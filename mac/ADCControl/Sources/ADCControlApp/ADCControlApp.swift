import ADCMonitorCore
import SwiftUI

@main
struct ADCControlApplication: App {
    @StateObject private var model = ADCViewModel()

    var body: some Scene {
        WindowGroup("ADC CRT Control") {
            ContentView(model: model)
                .frame(minWidth: 520, minHeight: 360)
        }
        .commands {
            CommandMenu("ADC Display") {
                Button("Power On") {
                    model.setPower(true)
                }
                .keyboardShortcut("p", modifiers: [.command, .option])

                Button("Power Off...") {
                    model.setPower(false)
                }
                .keyboardShortcut("p", modifiers: [.command, .option, .shift])

                Button("Cancel Power Off") {
                    model.cancelPowerOff()
                }
                .keyboardShortcut(".", modifiers: [.command])
            }
        }
    }
}

@MainActor
final class ADCViewModel: ObservableObject {
    @Published var hidStatus = "HID: checking..."
    @Published var ddcStatus = "DDC: checking..."
    @Published var values: [UInt8: Int] = [:]
    @Published var verticalSyncText = "V. Rate: -- Hz"
    @Published var powerIsOn = false
    @Published var status = ""
    @Published var countdown: Int?
    @Published var ddcReady = false
    @Published var selectedOEMResolutionID = ADCOEMResolution.all[0].id
    @Published var currentModeText = "Resolution: --"
    @Published var oemStatuses: [ADCOEMModeStatus] = []

    private let hid = MacHIDTransport()
    private let ddc = MacDDCPowerTransport()
    private let displayModesTransport = MacDisplayModeTransport()
    private var countdownTask: Task<Void, Never>?

    init() {
        Task {
            await refreshConnections()
            await refreshDisplayModes()
            await readAll()
        }
    }

    func refreshConnections() async {
        do {
            let summary = try hid.connect()
            hidStatus = summary.isADCStudioDisplay
                ? "HID: Studio Display connected"
                : "HID: unexpected device connected"
        } catch {
            hidStatus = "HID: \(error.localizedDescription)"
        }

        let displays = ddc.listDisplays()
        if let display = displays.first(where: { $0.isExternalADCStudioDisplay && $0.hasAVService }) {
            ddcReady = true
            let mirrorText = display.isMirrored ? " (mirrored)" : ""
            ddcStatus = "DDC: \(display.productName.isEmpty ? "Studio Display" : display.productName) ready\(mirrorText)"
        } else if displays.contains(where: { $0.isExternalADCStudioDisplay }) {
            ddcReady = false
            ddcStatus = "DDC: Studio Display found, no AV service"
        } else {
            ddcReady = false
            ddcStatus = "DDC: Studio Display not found"
        }
    }

    func readAll() async {
        status = "Reading values..."
        do {
            values = try hid.readAllControls()
            let vsync = try hid.readVerticalSyncHz()
            verticalSyncText = String(format: "V. Rate: %.1f Hz", vsync)
            powerIsOn = try hid.readPowerState().isOn
            status = ""
        } catch {
            status = error.localizedDescription
        }
    }

    func refreshDisplayModes() async {
        do {
            let statuses = try displayModesTransport.oemModeStatuses()
            oemStatuses = statuses
            let current = try displayModesTransport.currentMode()
            if let currentOEM = current.oemRecommendation {
                selectedOEMResolutionID = currentOEM.id
            } else if !statuses.contains(where: { $0.id == selectedOEMResolutionID }) {
                selectedOEMResolutionID = statuses.first(where: { canApplyOEMStatus($0) })?.id ?? ADCOEMResolution.all[0].id
            }
            currentModeText = "Resolution: \(current.label)"
        } catch {
            oemStatuses = []
            selectedOEMResolutionID = ADCOEMResolution.all[0].id
            currentModeText = "Resolution: unavailable"
        }
    }

    func applySelectedMode() {
        guard let selectedStatus = oemStatuses.first(where: { $0.id == selectedOEMResolutionID }) else {
            status = "No resolution selected."
            return
        }

        status = "Setting resolution..."
        Task {
            do {
                if let mode = readyMode(for: selectedStatus) {
                    try displayModesTransport.setMode(modeID: mode.modeID)
                } else if ADCEdidProfiles.studioDisplayOEMProfile(for: selectedStatus.recommendation) != nil {
                    try await applyOEMProfileMode(for: selectedStatus)
                } else {
                    throw ADCError.displayModeUnavailable(unavailableReason(for: selectedStatus))
                }
                try? await Task.sleep(for: .seconds(1))
                await refreshDisplayModes()
                await readAll()
                status = "Resolution updated."
            } catch {
                status = error.localizedDescription
            }
        }
    }

    func canApplyOEMStatus(_ status: ADCOEMModeStatus) -> Bool {
        readyMode(for: status) != nil || (
            ADCEdidProfiles.studioDisplayOEMProfile(for: status.recommendation) != nil &&
            ddcReady
        )
    }

    private func applyOEMProfileMode(for status: ADCOEMModeStatus) async throws {
        if let existingMode = preferredOEMMode(width: status.recommendation.width, height: status.recommendation.height) {
            try displayModesTransport.setMode(modeID: existingMode.modeID)
            return
        }

        guard let profile = ADCEdidProfiles.studioDisplayOEMProfile(for: status.recommendation) else {
            throw ADCError.displayModeUnavailable(unavailableReason(for: status))
        }

        self.status = "Applying \(status.recommendation.label) profile..."
        try ddc.setVirtualEDID(profile)
        try? await Task.sleep(for: .seconds(2))

        let statuses = try displayModesTransport.oemModeStatuses()
        guard let mode = statuses
            .first(where: { $0.recommendation.id == status.recommendation.id })?
            .preferredDesktopMode else {
            throw ADCError.displayModeUnavailable("\(status.recommendation.label) was not exposed after applying the profile.")
        }
        guard mode.matchesOEMTiming else {
            throw ADCError.displayModeUnavailable("macOS exposed \(mode.actualTimingLabel), not \(status.recommendation.label).")
        }
        try displayModesTransport.setMode(modeID: mode.modeID)
    }

    private func preferredOEMMode(width: Int, height: Int) -> ADCDisplayMode? {
        oemStatuses
            .first { $0.recommendation.width == width && $0.recommendation.height == height }?
            .preferredDesktopMode
            .flatMap { $0.matchesOEMTiming ? $0 : nil }
    }

    private func readyMode(for status: ADCOEMModeStatus) -> ADCDisplayMode? {
        status.preferredDesktopMode.flatMap { $0.matchesOEMTiming ? $0 : nil }
    }

    private func unavailableReason(for status: ADCOEMModeStatus) -> String {
        if let desktopMode = status.preferredDesktopMode {
            return "\(status.recommendation.label) is not available as an OEM timing. macOS currently offers \(desktopMode.actualTimingLabel) instead."
        }
        if let legacyMode = status.preferredLegacyMode {
            return "\(status.recommendation.label) is exposed only as legacy/non-desktop \(legacyMode.actualTimingLabel). macOS refuses to commit it as the live desktop mode."
        }
        return "\(status.recommendation.label) is not exposed by the current adapter/EDID path."
    }

    func set(_ control: ADCControl, to value: Int) {
        let clamped = control.clamped(value)
        values[control.code] = clamped
        status = "Setting 0x\(control.code.hex2) = \(clamped)..."
        Task {
            do {
                try hid.writeControl(control, value: clamped)
                status = ""
            } catch {
                status = error.localizedDescription
            }
        }
    }

    func degauss() {
        status = "Degaussing..."
        Task {
            do {
                try hid.degauss()
                status = ""
            } catch {
                status = error.localizedDescription
            }
        }
    }

    func save() {
        status = "Saving settings..."
        Task {
            do {
                try hid.saveSettings()
                status = "Settings saved."
            } catch {
                status = error.localizedDescription
            }
        }
    }

    func defaults() {
        status = "Restoring defaults..."
        Task {
            do {
                try hid.writeFactoryDefaults()
                values = Dictionary(uniqueKeysWithValues: ADCControls.all.map { ($0.code, $0.factoryDefault) })
                status = "Defaults restored."
            } catch {
                status = error.localizedDescription
            }
        }
    }

    func setPower(_ on: Bool) {
        if on {
            cancelPowerOff()
            powerIsOn = true
            status = "Powering on..."
            Task {
                do {
                    try setPowerTransport(on: true)
                    status = ""
                } catch {
                    status = error.localizedDescription
                }
            }
        } else {
            startPowerOffCountdown()
        }
    }

    func cancelPowerOff() {
        countdownTask?.cancel()
        countdownTask = nil
        countdown = nil
        powerIsOn = true
        status = ""
    }

    private func startPowerOffCountdown() {
        countdownTask?.cancel()
        powerIsOn = false
        countdown = 5
        status = "Powering off in 5s..."

        countdownTask = Task {
            for seconds in stride(from: 4, through: 0, by: -1) {
                try? await Task.sleep(for: .seconds(1))
                if Task.isCancelled {
                    return
                }
                countdown = seconds == 0 ? nil : seconds
                if seconds > 0 {
                    status = "Powering off in \(seconds)s..."
                }
            }

            do {
                status = "Powering off..."
                try setPowerTransport(on: false)
                status = ""
            } catch {
                powerIsOn = true
                status = error.localizedDescription
            }
        }
    }

    private func setPowerTransport(on: Bool) throws {
        do {
            try ddc.setPower(on: on)
        } catch {
            try hid.writePowerState(on: on)
        }
    }
}

struct ContentView: View {
    @ObservedObject var model: ADCViewModel
    @State private var visibleInfoID: String?

    var body: some View {
        GeometryReader { proxy in
            let layout = ADCUILayout(size: proxy.size)
            ScrollView {
                VStack(alignment: .leading, spacing: layout.sectionSpacing) {
                    header(layout)
                    Divider()
                    power(layout)
                    Divider()
                    controls(layout)
                    Divider()
                    actions(layout)
                    resolution(layout)
                    status
                }
                .padding(layout.padding)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func header(_ layout: ADCUILayout) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("ADC CRT Control")
                .font((layout.isCompact ? Font.headline : Font.title2).weight(.semibold))
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 14) {
                    connectionLabels
                    Spacer()
                    displaySummary
                }
                VStack(alignment: .leading, spacing: 3) {
                    connectionLabels
                    displaySummary
                }
            }
            .font(.callout)
            .foregroundStyle(.secondary)
        }
    }

    private var connectionLabels: some View {
        HStack(spacing: 10) {
            Label(model.hidStatus, systemImage: "cable.connector")
                .lineLimit(1)
            Label(model.ddcStatus, systemImage: "display")
                .lineLimit(1)
        }
    }

    private var displaySummary: some View {
        HStack(spacing: 10) {
            Text(model.currentModeText)
                .monospacedDigit()
                .lineLimit(1)
            Text(model.verticalSyncText)
                .monospacedDigit()
                .lineLimit(1)
        }
    }

    private func controls(_ layout: ADCUILayout) -> some View {
        Grid(alignment: .leading, horizontalSpacing: layout.controlSpacing, verticalSpacing: layout.controlRowSpacing) {
            ForEach(ADCControls.all) { control in
                let binding = Binding<Double>(
                    get: { Double(model.values[control.code, default: 0]) },
                    set: { model.set(control, to: Int($0.rounded())) }
                )
                let stepperBinding = Binding<Int>(
                    get: { model.values[control.code, default: 0] },
                    set: { model.set(control, to: $0) }
                )

                GridRow {
                    Text(control.label)
                        .frame(width: layout.labelWidth, alignment: .leading)
                    Slider(value: binding, in: 0...Double(control.maxValue), step: 1)
                        .frame(width: layout.sliderWidth)
                    Stepper(value: stepperBinding, in: 0...control.maxValue) {
                        Text("\(model.values[control.code, default: 0])")
                            .monospacedDigit()
                            .frame(width: 36, alignment: .trailing)
                    }
                    .frame(width: layout.stepperWidth)
                }
            }
        }
    }

    private func actions(_ layout: ADCUILayout) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                actionButtons
                Spacer()
            }
            VStack(alignment: .leading, spacing: 8) {
                actionButtons
            }
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 10) {
            Button {
                Task { await model.readAll() }
            } label: {
                Label("Read", systemImage: "arrow.clockwise")
            }

            Button {
                model.save()
            } label: {
                Label("Save", systemImage: "square.and.arrow.down")
            }

            Button {
                model.defaults()
            } label: {
                Label("Defaults", systemImage: "dial.low")
            }

            Button {
                model.degauss()
            } label: {
                Label("Degauss", systemImage: "waveform.path.ecg")
            }
        }
    }

    private func resolution(_ layout: ADCUILayout) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    Text("Resolution")
                        .frame(width: layout.labelWidth, alignment: .leading)
                    resolutionControls(layout)
                    Spacer()
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("Resolution")
                    resolutionControls(layout)
                }
            }

            if !model.oemStatuses.isEmpty {
                Grid(alignment: .leading, horizontalSpacing: layout.statusColumnSpacing, verticalSpacing: layout.statusRowSpacing) {
                    GridRow {
                        Text("")
                            .frame(width: layout.labelWidth)
                        Text("OEM Recommended")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Text("Status")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Text("")
                            .frame(width: 22)
                    }

                    ForEach(model.oemStatuses) { status in
                        GridRow {
                            Text("")
                                .frame(width: layout.labelWidth)
                            Text(status.recommendation.label)
                                .monospacedDigit()
                            Label(oemStatusText(status), systemImage: oemStatusIcon(status))
                                .foregroundStyle(oemStatusColor(status))
                            infoButton(for: status)
                        }
                        .font(.caption)
                    }
                }
            }
        }
    }

    private func resolutionControls(_ layout: ADCUILayout) -> some View {
        HStack(spacing: 10) {
            Picker("Resolution", selection: Binding<String>(
                get: { model.selectedOEMResolutionID },
                set: { model.selectedOEMResolutionID = $0 }
            )) {
                ForEach(ADCOEMResolution.all) { recommendation in
                    Text(recommendation.label)
                        .tag(recommendation.id)
                }
            }
            .labelsHidden()
            .frame(width: layout.pickerWidth)

            Button {
                model.applySelectedMode()
            } label: {
                Label("Apply", systemImage: "display.and.arrow.down")
            }
            .disabled(selectedOEMStatus.map { !model.canApplyOEMStatus($0) } ?? true)

            Button {
                Task { await model.refreshDisplayModes() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
    }

    private var selectedOEMStatus: ADCOEMModeStatus? {
        model.oemStatuses.first { $0.id == model.selectedOEMResolutionID }
    }

    private func oemStatus(for recommendation: ADCOEMResolution) -> ADCOEMModeStatus? {
        model.oemStatuses.first { $0.recommendation.id == recommendation.id }
    }

    private func oemStatusText(_ status: ADCOEMModeStatus) -> String {
        if let mode = status.preferredDesktopMode {
            if mode.matchesOEMTiming {
                return "Ready"
            }
            if canApplyViaProfile(status) {
                return "Profile available"
            }
            return "Fallback only"
        }
        if status.hasAnyMode {
            if canApplyViaProfile(status) {
                return "Profile available"
            }
            return "Blocked"
        }
        if canApplyViaProfile(status) {
            return "Profile available"
        }
        return "Not exposed"
    }

    private func oemStatusIcon(_ status: ADCOEMModeStatus) -> String {
        if status.preferredDesktopMode?.matchesOEMTiming == true {
            return "checkmark.circle.fill"
        }
        if canApplyViaProfile(status) {
            return "arrow.down.circle.fill"
        }
        if status.hasAnyMode {
            return "exclamationmark.circle.fill"
        }
        return "xmark.circle"
    }

    private func oemStatusColor(_ status: ADCOEMModeStatus) -> Color {
        if status.preferredDesktopMode?.matchesOEMTiming == true {
            return .green
        }
        if canApplyViaProfile(status) {
            return .orange
        }
        if status.hasAnyMode {
            return .orange
        }
        return .secondary
    }

    @ViewBuilder
    private func infoButton(for status: ADCOEMModeStatus) -> some View {
        if let info = oemInfoText(status) {
            Button {
                visibleInfoID = status.id
            } label: {
                Image(systemName: "info.circle")
            }
            .buttonStyle(.borderless)
            .foregroundStyle(.secondary)
            .frame(width: 22)
            .popover(isPresented: Binding(
                get: { visibleInfoID == status.id },
                set: { isPresented in
                    if !isPresented {
                        visibleInfoID = nil
                    }
                }
            )) {
                Text(info)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(14)
                    .frame(width: 320, alignment: .leading)
            }
        } else {
            Color.clear
                .frame(width: 22, height: 16)
        }
    }

    private func oemInfoText(_ status: ADCOEMModeStatus) -> String? {
        if status.preferredDesktopMode?.matchesOEMTiming == true {
            return nil
        }

        if canApplyViaProfile(status) {
            let fallback = status.preferredDesktopMode.map { " macOS is currently exposing \($0.actualTimingLabel) instead." } ?? ""
            return "This target uses a runtime EDID profile before switching to Apple's \(status.recommendation.label) timing.\(fallback) If the adapter blanks the link, unplug and replug the display chain."
        }

        if ADCEdidProfiles.studioDisplayOEMProfile(for: status.recommendation) != nil && !model.ddcReady {
            return "A runtime EDID profile exists for Apple's \(status.recommendation.label) timing, but DDC is not ready on the current display path, so the app cannot apply it right now."
        }

        if let mode = status.preferredDesktopMode {
            return "macOS exposes \(mode.actualTimingLabel), but that is not Apple's OEM \(status.recommendation.label) timing. The app does not treat fallback or scaled modes as valid OEM output."
        }

        if let mode = status.preferredLegacyMode {
            if status.recommendation.width == 640 && status.recommendation.height == 480 {
                return "macOS sees a 640x480 mode, but marks it legacy/non-desktop and refuses to commit it as the live desktop mode. That is why this app cannot safely switch to Apple's 640x480 @ 153 Hz target on this Mac path."
            }
            return "macOS exposes \(mode.actualTimingLabel), but only as a legacy/non-desktop mode, so the normal desktop mode switch cannot use it."
        }

        return "The current adapter/EDID path does not expose Apple's \(status.recommendation.label) timing to macOS."
    }

    private func canApplyViaProfile(_ status: ADCOEMModeStatus) -> Bool {
        ADCEdidProfiles.studioDisplayOEMProfile(for: status.recommendation) != nil &&
            status.preferredDesktopMode?.matchesOEMTiming != true &&
            model.ddcReady
    }

    private func power(_ layout: ADCUILayout) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                powerControls
                Spacer()
            }
            VStack(alignment: .leading, spacing: 8) {
                powerControls
            }
        }
    }

    private var powerControls: some View {
        HStack(spacing: 10) {
            Toggle("Power", isOn: Binding(
                get: { model.powerIsOn },
                set: { model.setPower($0) }
            ))
            .toggleStyle(.switch)

            Button {
                model.setPower(true)
            } label: {
                Label("Power On", systemImage: "power")
            }
            .keyboardShortcut("p", modifiers: [.command, .option])

            Button {
                model.setPower(false)
            } label: {
                Label("Power Off...", systemImage: "power.circle")
            }
            .keyboardShortcut("p", modifiers: [.command, .option, .shift])

            if let countdown = model.countdown {
                Text("Powering off in \(countdown)s")
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                Button("Cancel") {
                    model.cancelPowerOff()
                }
                .keyboardShortcut(".", modifiers: [.command])
            }
        }
    }

    private var status: some View {
        Text(model.status)
            .font(.callout)
            .foregroundStyle(model.status.localizedCaseInsensitiveContains("error") ? .red : .secondary)
            .frame(minHeight: 20, alignment: .leading)
    }
}

private struct ADCUILayout {
    let size: CGSize

    var isCompact: Bool {
        size.height < 640 || size.width < 700
    }

    var padding: CGFloat {
        isCompact ? 10 : 18
    }

    var sectionSpacing: CGFloat {
        isCompact ? 8 : 14
    }

    var controlSpacing: CGFloat {
        isCompact ? 8 : 12
    }

    var controlRowSpacing: CGFloat {
        isCompact ? 5 : 10
    }

    var statusRowSpacing: CGFloat {
        isCompact ? 3 : 5
    }

    var statusColumnSpacing: CGFloat {
        isCompact ? 10 : 14
    }

    var labelWidth: CGFloat {
        isCompact ? 96 : 116
    }

    var stepperWidth: CGFloat {
        isCompact ? 104 : 112
    }

    var sliderWidth: CGFloat {
        let available = size.width - padding * 2 - labelWidth - stepperWidth - controlSpacing * 2 - 28
        return min(300, max(170, available))
    }

    var pickerWidth: CGFloat {
        let available = size.width - padding * 2 - labelWidth - 230
        return min(360, max(230, available))
    }
}
