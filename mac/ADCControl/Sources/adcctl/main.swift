import ADCMonitorCore
import Foundation

@main
struct ADCCTL {
    static func main() {
        do {
            try run(Array(CommandLine.arguments.dropFirst()))
        } catch {
            fputs("error: \(error.localizedDescription)\n", stderr)
            Foundation.exit(1)
        }
    }

    private static func run(_ args: [String]) throws {
        guard let command = args.first else {
            printUsage()
            return
        }

        switch command {
        case "hid":
            try runHID(Array(args.dropFirst()))
        case "ddc":
            try runDDC(Array(args.dropFirst()))
        case "power":
            try runPower(Array(args.dropFirst()))
        case "display", "resolution":
            try runDisplay(Array(args.dropFirst()))
        case "self-test":
            try runSelfTest()
        case "help", "-h", "--help":
            printUsage()
        default:
            printUsage()
            throw ADCError.invalidControlCode(command)
        }
    }

    private static func runHID(_ args: [String]) throws {
        guard let subcommand = args.first else {
            printHIDUsage()
            return
        }

        switch subcommand {
        case "list":
            let devices = MacHIDTransport.listDevices()
            if devices.isEmpty {
                print("No HID devices visible.")
                return
            }
            for device in devices {
                let marker = device.isADCStudioDisplay ? "*" : " "
                print("\(marker) \(String(format: "%04X", device.vendorID)):\(String(format: "%04X", device.productID)) \(device.product)")
                print("    manufacturer: \(device.manufacturer.isEmpty ? "(none)" : device.manufacturer)")
                print("    usage: page 0x\(device.primaryUsagePage.hex2), usage 0x\(device.primaryUsage.hex2)")
                print("    max feature report: \(device.maxFeatureReportSize)")
                if device.isADCStudioDisplay {
                    print("    controls: \(ADCControls.all.map { "0x\($0.code.hex2)" }.joined(separator: ", ")), 0x\(ADCUsage.verticalSync.hex2), 0x\(ADCUsage.power.hex2), 0x\(ADCUsage.save.hex2), 0x\(ADCUsage.degauss.hex2)")
                }
            }
        case "read":
            guard args.count >= 2 else {
                printHIDUsage()
                return
            }
            let code = try parseCode(args[1])
            let report = try MacHIDTransport().readFeature(code: code)
            let valueText = report.value.map(String.init) ?? "(nil)"
            print("0x\(code.hex2) = \(valueText)")
            print("raw: \(report.bytesDescription)")
            if code == ADCUsage.verticalSync, let value = report.value {
                print(String(format: "vsync: %.2f Hz", Double(value) / 100.0))
            }
        case "set":
            guard args.count >= 3 else {
                printHIDUsage()
                return
            }
            let code = try parseCode(args[1])
            let value = try parseValue(args[2])
            let transport = MacHIDTransport()
            if let control = ADCControls.byCode[code] {
                try transport.writeControl(control, value: value)
            } else {
                try transport.writeFeature(code: code, payload: [UInt8(clamping: value)])
            }
            print("wrote 0x\(code.hex2) = \(value)")
        default:
            printHIDUsage()
            throw ADCError.invalidControlCode(subcommand)
        }
    }

    private static func runDDC(_ args: [String]) throws {
        guard let subcommand = args.first else {
            printDDCUsage()
            return
        }

        let transport = MacDDCPowerTransport()
        switch subcommand {
        case "list":
            let displays = transport.listDisplays()
            if displays.isEmpty {
                print("No DDC-capable online displays visible.")
                return
            }
            for display in displays {
                let marker = display.isExternalADCStudioDisplay ? "*" : " "
                let location = display.isBuiltIn ? "built-in" : "external"
                print("\(marker) [\(display.index)] \(display.productName.isEmpty ? "(unnamed)" : display.productName) (\(display.uuid))")
                print("    vendor: \(display.vendor) (0x\(String(format: "%04X", display.vendor)))")
                print("    model: \(display.model) (0x\(String(format: "%04X", display.model)))")
                print("    location: \(location)")
                print("    mirrored: \(display.isMirrored ? "yes" : "no")")
                print("    serial: \(display.serial)")
                print("    edid: \(display.edidUUID.isEmpty ? "(none)" : display.edidUUID)")
                print("    av service: \(display.hasAVService ? "yes" : "no")")
            }
            let packet = DDCPacket.power(on: true)
            print("power-on packet: \(packet.fullPiStyleBytes.map { "0x\($0.hex2)" }.joined(separator: " "))")
            print("power-off packet: \(DDCPacket.power(on: false).fullPiStyleBytes.map { "0x\($0.hex2)" }.joined(separator: " "))")
        case "edid":
            let edid = try transport.copyEDID()
            print(edid.map { String(format: "%02X", $0) }.joined())
        case "edid-i2c":
            let edid = try transport.readEDIDOverI2C()
            print(edid.map { String(format: "%02X", $0) }.joined())
        case "virtual-edid":
            guard args.count >= 2 else {
                printDDCUsage()
                return
            }
            let url = URL(fileURLWithPath: args[1])
            let edid = try Data(contentsOf: url)
            try validateEDID(edid)
            try transport.setVirtualEDID(edid)
            print("virtual EDID enabled from \(url.path)")
        case "reset-edid":
            try transport.resetVirtualEDID()
            print("virtual EDID disabled")
        default:
            printDDCUsage()
            throw ADCError.invalidControlCode(subcommand)
        }
    }

    private static func runPower(_ args: [String]) throws {
        guard let subcommand = args.first else {
            printPowerUsage()
            return
        }

        switch subcommand {
        case "read":
            let state = try MacHIDTransport().readPowerState()
            print("power: \(state) (\(state.rawValue))")
        case "on":
            try setPowerWithFallback(on: true)
            print("power on sent")
        case "off":
            try setPowerWithFallback(on: false)
            print("power off sent")
        case "on-hid":
            try MacHIDTransport().writePowerState(on: true)
            print("power on sent via HID")
        case "off-hid":
            try MacHIDTransport().writePowerState(on: false)
            print("power off sent via HID")
        default:
            printPowerUsage()
            throw ADCError.invalidControlCode(subcommand)
        }
    }

    private static func setPowerWithFallback(on: Bool) throws {
        do {
            try MacDDCPowerTransport().setPower(on: on)
        } catch {
            try MacHIDTransport().writePowerState(on: on)
        }
    }

    private static func runDisplay(_ args: [String]) throws {
        guard let subcommand = args.first else {
            printDisplayUsage()
            return
        }

        let transport = MacDisplayModeTransport()
        switch subcommand {
        case "current":
            let mode = try transport.currentMode()
            print("current: \(mode.modeID) \(mode.label)")
        case "modes":
            let current = try? transport.currentMode()
            let modes = try transport.availableModes()
            for mode in modes {
                let marker = mode.modeID == current?.modeID ? "*" : " "
                print("\(marker) \(mode.modeID): \(mode.label)")
            }
        case "oem":
            try printOEMStatus(using: transport)
        case "test-oem":
            try testOEMDesktopModes(using: transport)
        case "test-legacy-640":
            let seconds = args.count >= 2 ? (Double(args[1]) ?? 5) : 5
            try testLegacy640(using: transport, holdSeconds: min(max(seconds, 1), 30), method: .direct)
        case "test-legacy-640-config":
            let seconds = args.count >= 2 ? (Double(args[1]) ?? 5) : 5
            try testLegacy640(using: transport, holdSeconds: min(max(seconds, 1), 30), method: .configuration)
        case "test-legacy-640-capture":
            let seconds = args.count >= 2 ? (Double(args[1]) ?? 5) : 5
            try testLegacy640(using: transport, holdSeconds: min(max(seconds, 1), 30), method: .captured)
        case "set":
            guard args.count >= 2, let modeID = Int32(args[1]) else {
                printDisplayUsage()
                return
            }
            try transport.setMode(modeID: modeID)
            print("display mode set to \(modeID)")
        case "set-unsafe":
            guard args.count >= 2, let modeID = Int32(args[1]) else {
                printDisplayUsage()
                return
            }
            try transport.setUnsafeMode(modeID: modeID)
            print("unsafe display mode set to \(modeID)")
        default:
            printDisplayUsage()
            throw ADCError.invalidControlCode(subcommand)
        }
    }

    private static func printOEMStatus(using transport: MacDisplayModeTransport) throws {
        let statuses = try transport.oemModeStatuses()
        for status in statuses {
            if let mode = status.preferredDesktopMode {
                let timing = mode.matchesOEMTiming ? "ready" : "fallback"
                print("[desktop] \(status.recommendation.label) -> \(timing), mode \(mode.modeID), macOS offers \(mode.actualTimingLabel)")
            } else if let mode = status.preferredLegacyMode {
                print("[legacy]  \(status.recommendation.label) -> blocked, mode \(mode.modeID), macOS offers \(mode.actualTimingLabel), not desktop-usable")
            } else {
                print("[missing] \(status.recommendation.label) -> blocked, not exposed by macOS")
            }
        }
    }

    private static func testOEMDesktopModes(using transport: MacDisplayModeTransport) throws {
        let original = try transport.currentMode()
        let statuses = try transport.oemModeStatuses()
        print("original: \(original.modeID) \(original.label)")

        defer {
            try? transport.setMode(modeID: original.modeID)
        }

        for status in statuses {
            guard let mode = status.preferredDesktopMode else {
                if status.legacyModes.isEmpty {
                    print("[skip] \(status.recommendation.label): not exposed by macOS")
                } else {
                    print("[skip] \(status.recommendation.label): only exposed as non-desktop mode")
                }
                continue
            }

            do {
                try transport.setMode(modeID: mode.modeID)
                Thread.sleep(forTimeInterval: 0.8)
                let applied = try transport.currentMode()
                let works = applied.width == mode.width && applied.height == mode.height
                let result = works ? "works" : "changed unexpectedly"
                print("[\(result)] \(status.recommendation.label): requested \(mode.modeID) \(mode.label), applied \(applied.modeID) \(applied.label)")
            } catch {
                print("[failed] \(status.recommendation.label): \(error.localizedDescription)")
            }
        }
    }

    private static func testLegacy640(
        using transport: MacDisplayModeTransport,
        holdSeconds: Double,
        method: ADCDisplayModeSetMethod
    ) throws {
        let original = try transport.currentMode()
        let statuses = try transport.oemModeStatuses()
        guard let mode = statuses
            .first(where: { $0.recommendation.width == 640 && $0.recommendation.height == 480 })?
            .preferredLegacyMode
        else {
            throw ADCError.displayModeUnavailable("No legacy 640x480 mode is exposed by macOS.")
        }

        print("original: \(original.modeID) \(original.label)")
        print("trying unsafe \(method.rawValue): \(mode.modeID) \(mode.label) for \(String(format: "%.0f", holdSeconds))s")

        let capturedDisplay = method == .captured ? try transport.captureDisplay() : nil

        defer {
            do {
                try transport.setUnsafeMode(modeID: original.modeID, display: capturedDisplay)
                print("restored: \(original.modeID) \(original.label)")
            } catch {
                fputs("restore failed: \(error.localizedDescription)\n", stderr)
            }
            if let capturedDisplay {
                transport.releaseDisplay(capturedDisplay)
            }
        }

        switch method {
        case .direct:
            try transport.setUnsafeMode(modeID: mode.modeID)
        case .configuration:
            try transport.setUnsafeModeUsingConfiguration(modeID: mode.modeID)
        case .captured:
            try transport.setUnsafeMode(modeID: mode.modeID, display: capturedDisplay)
        }
        Thread.sleep(forTimeInterval: holdSeconds)
        let applied = try transport.currentMode()
        let works = applied.width == mode.width && applied.height == mode.height
        let result = works ? "accepted" : "changed unexpectedly"
        print("[\(result)] requested \(mode.modeID) \(mode.label), applied \(applied.modeID) \(applied.label)")
    }

    private static func runSelfTest() throws {
        guard ADCControls.all.count == 11,
              ADCControls.byCode[0x12]?.factoryDefault == 95,
              ADCControls.byCode[0x20]?.factoryDefault == 64,
              ADCControls.byCode[0x44]?.maxValue == 255
        else {
            throw ADCError.invalidValue("control constants do not match the Pi backend")
        }

        guard DDCPacket.power(on: true).fullPiStyleBytes == [0x51, 0x84, 0x03, 0xD6, 0x00, 0x01, 0x6F],
              DDCPacket.power(on: false).fullPiStyleBytes == [0x51, 0x84, 0x03, 0xD6, 0x00, 0x04, 0x6A]
        else {
            throw ADCError.invalidValue("DDC power packets do not match the Pi backend")
        }

        guard HIDFeatureReport(code: 0x12, rawBytes: [95, 0]).value == 95,
              HIDFeatureReport(code: 0x12, rawBytes: [0x12, 95, 0]).value == 95,
              HIDFeatureReport(code: 0xAE, rawBytes: [0x70, 0x17]).value == 6000,
              HIDFeatureReport(code: 0xAE, rawBytes: [0xAE, 0x70, 0x17]).value == 6000
        else {
            throw ADCError.invalidValue("HID feature report parsing failed")
        }

        guard ADCOEMResolution.all.map(\.resolutionLabel) == [
            "640x480", "800x600", "832x624", "1024x768", "1056x792", "1280x1024", "1600x1200",
        ] else {
            throw ADCError.invalidValue("OEM resolution table does not match Apple Studio Display documentation")
        }

        try validateEDID(ADCEdidProfiles.studioDisplayAllOEM)

        print("self-test passed")
    }

    private static func parseCode(_ text: String) throws -> UInt8 {
        let trimmed = text.lowercased().replacingOccurrences(of: "0x", with: "")
        guard let value = UInt8(trimmed, radix: 16) else {
            throw ADCError.invalidControlCode(text)
        }
        return value
    }

    private static func parseValue(_ text: String) throws -> Int {
        guard let value = Int(text) else {
            throw ADCError.invalidValue(text)
        }
        return value
    }

    private static func validateEDID(_ data: Data) throws {
        guard data.count == 128 || data.count == 256 else {
            throw ADCError.invalidValue("EDID must be 128 or 256 bytes, got \(data.count)")
        }
        let expectedExtensionCount = Int(data[126])
        guard data.count == 128 * (expectedExtensionCount + 1) else {
            throw ADCError.invalidValue("EDID extension count is \(expectedExtensionCount), but file has \(data.count / 128) block(s)")
        }
        for blockStart in stride(from: 0, to: data.count, by: 128) {
            let checksum = data[blockStart..<blockStart + 128].reduce(UInt8(0), &+)
            guard checksum == 0 else {
                throw ADCError.invalidValue("EDID block \(blockStart / 128) checksum is invalid")
            }
        }
    }

    private static func printUsage() {
        print("""
        adcctl commands:
          adcctl hid list
          adcctl hid read 0x12
          adcctl hid set 0x12 95
          adcctl ddc list
          adcctl ddc edid
          adcctl ddc edid-i2c
          adcctl ddc virtual-edid <edid.bin>
          adcctl ddc reset-edid
          adcctl display current
          adcctl display modes
          adcctl display oem
          adcctl display test-oem
          adcctl display test-legacy-640 [seconds]
          adcctl display test-legacy-640-config [seconds]
          adcctl display test-legacy-640-capture [seconds]
          adcctl display set <mode-id>
          adcctl display set-unsafe <mode-id>
          adcctl power read
          adcctl power on
          adcctl power off
          adcctl power on-hid
          adcctl power off-hid
          adcctl self-test
        """)
    }

    private static func printHIDUsage() {
        print("usage: adcctl hid list | read <hex-code> | set <hex-code> <value>")
    }

    private static func printDDCUsage() {
        print("usage: adcctl ddc list | edid | edid-i2c | virtual-edid <edid.bin> | reset-edid")
    }

    private static func printPowerUsage() {
        print("usage: adcctl power read | on | off | on-hid | off-hid")
    }

    private static func printDisplayUsage() {
        print("usage: adcctl display current | modes | oem | test-oem | test-legacy-640 [seconds] | test-legacy-640-config [seconds] | test-legacy-640-capture [seconds] | set <mode-id> | set-unsafe <mode-id>")
    }
}
