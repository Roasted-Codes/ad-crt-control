import Foundation

public struct ADCControl: Identifiable, Hashable, Sendable {
    public let code: UInt8
    public let label: String
    public let maxValue: Int
    public let factoryDefault: Int

    public var id: UInt8 { code }

    public func clamped(_ value: Int) -> Int {
        min(max(value, 0), maxValue)
    }
}

public enum ADCControls {
    public static let contrast = ADCControl(code: 0x12, label: "Contrast", maxValue: 96, factoryDefault: 95)

    public static let all: [ADCControl] = [
        contrast,
        ADCControl(code: 0x20, label: "H Phase", maxValue: 96, factoryDefault: 64),
        ADCControl(code: 0x22, label: "H Size", maxValue: 96, factoryDefault: 70),
        ADCControl(code: 0x30, label: "V Phase", maxValue: 96, factoryDefault: 68),
        ADCControl(code: 0x32, label: "V Size", maxValue: 96, factoryDefault: 67),
        ADCControl(code: 0x24, label: "H Pincushion", maxValue: 96, factoryDefault: 63),
        ADCControl(code: 0x42, label: "H Keystone", maxValue: 96, factoryDefault: 54),
        ADCControl(code: 0x40, label: "H Key Balance", maxValue: 96, factoryDefault: 77),
        ADCControl(code: 0x44, label: "Rotation", maxValue: 255, factoryDefault: 152),
        ADCControl(code: 0x28, label: "H Stat Conv", maxValue: 255, factoryDefault: 127),
        ADCControl(code: 0x38, label: "V Stat Conv", maxValue: 255, factoryDefault: 127),
    ]

    public static let byCode: [UInt8: ADCControl] = Dictionary(uniqueKeysWithValues: all.map { ($0.code, $0) })
}

public enum ADCUsage {
    public static let vendorID = 0x05AC
    public static let productID = 0x9213
    public static let displayVendorID = 0x0610
    public static let primaryUsagePage = 0x80
    public static let primaryUsage = 0x01

    public static let degauss: UInt8 = 0x01
    public static let save: UInt8 = 0xB0
    public static let power: UInt8 = 0xD6
    public static let verticalSync: UInt8 = 0xAE
    public static let button: UInt8 = 0xE4
}

public enum ADCPowerState: Int, Sendable {
    case on = 1
    case sleep = 2
    case off = 4
    case unknown = -1

    public init(rawHIDValue: Int?) {
        switch rawHIDValue {
        case 1: self = .on
        case 2: self = .sleep
        case 4: self = .off
        default: self = .unknown
        }
    }

    public var isOn: Bool {
        self == .on
    }
}

public enum ADCError: Error, LocalizedError, Sendable {
    case deviceNotFound
    case deviceOpenFailed(String)
    case reportReadFailed(code: UInt8, status: Int32)
    case reportWriteFailed(code: UInt8, status: Int32)
    case invalidControlCode(String)
    case invalidValue(String)
    case displayNotFound
    case ddcUnavailable(String)
    case ddcWriteFailed(String)
    case displayModeUnavailable(String)
    case displayModeSetFailed(String)

    public var errorDescription: String? {
        switch self {
        case .deviceNotFound:
            return "Apple Studio Display HID device 05ac:9213 was not found."
        case .deviceOpenFailed(let message):
            return "Could not open Apple Studio Display HID device: \(message)"
        case .reportReadFailed(let code, let status):
            return "Could not read HID feature report 0x\(code.hex2) (IOReturn \(status))."
        case .reportWriteFailed(let code, let status):
            return "Could not write HID feature report 0x\(code.hex2) (IOReturn \(status))."
        case .invalidControlCode(let value):
            return "Invalid control code: \(value)"
        case .invalidValue(let value):
            return "Invalid value: \(value)"
        case .displayNotFound:
            return "Apple Studio Display DDC display was not found."
        case .ddcUnavailable(let message):
            return "DDC is unavailable: \(message)"
        case .ddcWriteFailed(let message):
            return "DDC write failed: \(message)"
        case .displayModeUnavailable(let message):
            return "Display mode unavailable: \(message)"
        case .displayModeSetFailed(let message):
            return "Could not set display mode: \(message)"
        }
    }
}

public extension UInt8 {
    var hex2: String {
        String(format: "%02X", self)
    }
}

public extension Int {
    var hex2: String {
        String(format: "%02X", self)
    }
}
