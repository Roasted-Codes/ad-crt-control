import CoreGraphics
import Foundation

public struct ADCDisplayMode: Identifiable, Hashable, Sendable {
    public let modeID: Int32
    public let width: Int
    public let height: Int
    public let pixelWidth: Int
    public let pixelHeight: Int
    public let refreshRate: Double
    public let ioFlags: UInt32
    public let isUsableForDesktop: Bool

    public var id: Int32 { modeID }

    public var oemRecommendation: ADCOEMResolution? {
        ADCOEMResolution.match(width: width, height: height)
    }

    public var matchesOEMTiming: Bool {
        guard let oemRecommendation else {
            return false
        }
        return abs(refreshRate - oemRecommendation.refreshRate) <= 1.5
    }

    public var actualTimingLabel: String {
        let refresh = refreshRate > 0 ? String(format: "%.0f Hz", refreshRate) : "default Hz"
        if pixelWidth != width || pixelHeight != height {
            return "\(width)x\(height) (\(pixelWidth)x\(pixelHeight)) @ \(refresh)"
        }
        return "\(width)x\(height) @ \(refresh)"
    }

    public var label: String {
        matchesOEMTiming ? "\(actualTimingLabel) - OEM" : actualTimingLabel
    }
}

public struct ADCOEMResolution: Identifiable, Hashable, Sendable {
    public let width: Int
    public let height: Int
    public let refreshRate: Double

    public var id: String { "\(width)x\(height)@\(refreshRate)" }
    public var resolutionLabel: String { "\(width)x\(height)" }
    public var refreshLabel: String { String(format: "%.0f Hz", refreshRate) }
    public var label: String { "\(resolutionLabel) @ \(refreshLabel)" }

    public static let all: [ADCOEMResolution] = [
        ADCOEMResolution(width: 640, height: 480, refreshRate: 153),
        ADCOEMResolution(width: 800, height: 600, refreshRate: 125),
        ADCOEMResolution(width: 832, height: 624, refreshRate: 120),
        ADCOEMResolution(width: 1024, height: 768, refreshRate: 99),
        ADCOEMResolution(width: 1056, height: 792, refreshRate: 96),
        ADCOEMResolution(width: 1280, height: 1024, refreshRate: 75),
        ADCOEMResolution(width: 1600, height: 1200, refreshRate: 65),
    ]

    public static func match(width: Int, height: Int) -> ADCOEMResolution? {
        all.first { $0.width == width && $0.height == height }
    }
}

public struct ADCOEMModeStatus: Identifiable, Sendable {
    public let recommendation: ADCOEMResolution
    public let desktopModes: [ADCDisplayMode]
    public let legacyModes: [ADCDisplayMode]

    public var id: String { recommendation.id }
    public var isAvailableForDesktop: Bool { !desktopModes.isEmpty }
    public var hasAnyMode: Bool { isAvailableForDesktop || !legacyModes.isEmpty }

    public var preferredDesktopMode: ADCDisplayMode? {
        preferredMode(from: desktopModes)
    }

    public var preferredLegacyMode: ADCDisplayMode? {
        preferredMode(from: legacyModes)
    }

    private func preferredMode(from modes: [ADCDisplayMode]) -> ADCDisplayMode? {
        modes.sorted {
            let leftExactPixels = $0.pixelWidth == recommendation.width && $0.pixelHeight == recommendation.height
            let rightExactPixels = $1.pixelWidth == recommendation.width && $1.pixelHeight == recommendation.height
            if leftExactPixels != rightExactPixels { return leftExactPixels }
            let leftRefreshDistance = abs($0.refreshRate - recommendation.refreshRate)
            let rightRefreshDistance = abs($1.refreshRate - recommendation.refreshRate)
            if leftRefreshDistance != rightRefreshDistance { return leftRefreshDistance < rightRefreshDistance }
            return $0.modeID < $1.modeID
        }.first
    }
}

public enum ADCDisplayModeSetMethod: String, Sendable {
    case direct
    case configuration
    case captured
}

public final class MacDisplayModeTransport {
    private let displayProvider: MacDDCPowerTransport

    public init(displayProvider: MacDDCPowerTransport = MacDDCPowerTransport()) {
        self.displayProvider = displayProvider
    }

    public func currentMode(display: DDCDisplay? = nil) throws -> ADCDisplayMode {
        let target = try display ?? displayProvider.preferredADCDisplay()
        try validateTargetDisplay(target)
        guard let mode = CGDisplayCopyDisplayMode(target.displayID) else {
            throw ADCError.displayModeUnavailable("No current display mode is available for display \(target.index).")
        }
        return ADCDisplayMode(mode)
    }

    public func availableModes(display: DDCDisplay? = nil) throws -> [ADCDisplayMode] {
        try modes(display: display, includeNonDesktop: false)
    }

    public func allModes(display: DDCDisplay? = nil) throws -> [ADCDisplayMode] {
        try modes(display: display, includeNonDesktop: true)
    }

    public func oemModeStatuses(display: DDCDisplay? = nil) throws -> [ADCOEMModeStatus] {
        let modes = try allModes(display: display)
        return ADCOEMResolution.all.map { recommendation in
            let matchingModes = modes.filter {
                $0.width == recommendation.width && $0.height == recommendation.height
            }
            return ADCOEMModeStatus(
                recommendation: recommendation,
                desktopModes: matchingModes.filter(\.isUsableForDesktop).sorted(),
                legacyModes: matchingModes.filter { !$0.isUsableForDesktop }.sorted()
            )
        }
    }

    private func modes(display: DDCDisplay? = nil, includeNonDesktop: Bool) throws -> [ADCDisplayMode] {
        let target = try display ?? displayProvider.preferredADCDisplay()
        try validateTargetDisplay(target)
        let options = [
            kCGDisplayShowDuplicateLowResolutionModes as String: true,
        ] as CFDictionary

        guard let modes = CGDisplayCopyAllDisplayModes(target.displayID, options) as? [CGDisplayMode] else {
            throw ADCError.displayModeUnavailable("No display modes are available for display \(target.index).")
        }

        let mappedModes = modes.map(ADCDisplayMode.init)
        if includeNonDesktop {
            return mappedModes.sorted()
        }
        return mappedModes.filter(\.isUsableForDesktop).sorted()
    }

    public func setMode(modeID: Int32, display: DDCDisplay? = nil) throws {
        try setMode(modeID: modeID, display: display, includeNonDesktop: false, method: .direct)
    }

    public func setUnsafeMode(modeID: Int32, display: DDCDisplay? = nil) throws {
        try setMode(modeID: modeID, display: display, includeNonDesktop: true, method: .direct)
    }

    public func setUnsafeModeUsingConfiguration(modeID: Int32, display: DDCDisplay? = nil) throws {
        try setMode(modeID: modeID, display: display, includeNonDesktop: true, method: .configuration)
    }

    public func captureDisplay(display: DDCDisplay? = nil) throws -> DDCDisplay {
        let target = try display ?? displayProvider.preferredADCDisplay()
        let result = CGDisplayCapture(target.displayID)
        guard result == .success else {
            throw ADCError.displayModeSetFailed("display capture returned \(result.rawValue).")
        }
        return target
    }

    public func releaseDisplay(_ display: DDCDisplay) {
        CGDisplayRelease(display.displayID)
    }

    private func setMode(
        modeID: Int32,
        display: DDCDisplay? = nil,
        includeNonDesktop: Bool,
        method: ADCDisplayModeSetMethod
    ) throws {
        let target = try display ?? displayProvider.preferredADCDisplay()
        try validateTargetDisplay(target)
        let modes = try availableCGModes(display: target, includeNonDesktop: includeNonDesktop)
        guard let mode = modes.first(where: { $0.ioDisplayModeID == modeID }) else {
            throw ADCError.displayModeUnavailable("Display mode \(modeID) is not available for \(target.productName).")
        }

        let mirrorState = MacDisplayMirrorState.capture()
        let result: CGError
        switch method {
        case .direct:
            result = CGDisplaySetDisplayMode(target.displayID, mode, nil)
        case .configuration:
            result = configureDisplay(target.displayID, mode: mode)
        case .captured:
            let capture = CGDisplayCapture(target.displayID)
            guard capture == .success else {
                throw ADCError.displayModeSetFailed("display capture returned \(capture.rawValue).")
            }
            defer {
                CGDisplayRelease(target.displayID)
            }
            result = CGDisplaySetDisplayMode(target.displayID, mode, nil)
        }

        guard result == .success else {
            throw ADCError.displayModeSetFailed("\(method.rawValue) mode set returned \(result.rawValue).")
        }
        try mirrorState.restoreIfChanged()
    }

    private func configureDisplay(_ displayID: CGDirectDisplayID, mode: CGDisplayMode) -> CGError {
        var config: CGDisplayConfigRef?
        let begin = CGBeginDisplayConfiguration(&config)
        guard begin == .success, let config else {
            return begin
        }

        let configure = CGConfigureDisplayWithDisplayMode(config, displayID, mode, nil)
        guard configure == .success else {
            CGCancelDisplayConfiguration(config)
            return configure
        }

        return CGCompleteDisplayConfiguration(config, .forSession)
    }

    private func availableCGModes(display: DDCDisplay, includeNonDesktop: Bool) throws -> [CGDisplayMode] {
        try validateTargetDisplay(display)
        let options = [
            kCGDisplayShowDuplicateLowResolutionModes as String: true,
        ] as CFDictionary

        guard let modes = CGDisplayCopyAllDisplayModes(display.displayID, options) as? [CGDisplayMode] else {
            throw ADCError.displayModeUnavailable("No display modes are available for display \(display.index).")
        }
        if includeNonDesktop {
            return modes
        }
        return modes.filter { $0.isUsableForDesktopGUI() }
    }

    private func validateTargetDisplay(_ display: DDCDisplay) throws {
        guard display.isExternalADCStudioDisplay else {
            let displayName = display.productName.isEmpty ? "display \(display.index)" : display.productName
            throw ADCError.displayModeUnavailable("Refusing to use \(displayName); target must be the external Apple Studio Display 0x\(String(format: "%04X", ADCUsage.productID)).")
        }
    }

}

private extension ADCDisplayMode {
    init(_ mode: CGDisplayMode) {
        self.init(
            modeID: mode.ioDisplayModeID,
            width: mode.width,
            height: mode.height,
            pixelWidth: mode.pixelWidth,
            pixelHeight: mode.pixelHeight,
            refreshRate: mode.refreshRate,
            ioFlags: mode.ioFlags,
            isUsableForDesktop: mode.isUsableForDesktopGUI()
        )
    }
}

private extension Array where Element == ADCDisplayMode {
    func sorted() -> [ADCDisplayMode] {
        sorted {
            if $0.width != $1.width { return $0.width < $1.width }
            if $0.height != $1.height { return $0.height < $1.height }
            if $0.refreshRate != $1.refreshRate { return $0.refreshRate < $1.refreshRate }
            if $0.pixelWidth != $1.pixelWidth { return $0.pixelWidth < $1.pixelWidth }
            return $0.modeID < $1.modeID
        }
    }
}
