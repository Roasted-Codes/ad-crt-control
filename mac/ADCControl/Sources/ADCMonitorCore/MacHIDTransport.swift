import Foundation
import IOKit.hid

public struct HIDDeviceSummary: Sendable {
    public let vendorID: Int
    public let productID: Int
    public let product: String
    public let manufacturer: String
    public let primaryUsagePage: Int
    public let primaryUsage: Int
    public let maxFeatureReportSize: Int

    public var isADCStudioDisplay: Bool {
        vendorID == ADCUsage.vendorID
            && productID == ADCUsage.productID
            && primaryUsagePage == ADCUsage.primaryUsagePage
            && primaryUsage == ADCUsage.primaryUsage
    }
}

public struct HIDFeatureReport: Sendable {
    public let code: UInt8
    public let rawBytes: [UInt8]

    public init(code: UInt8, rawBytes: [UInt8]) {
        self.code = code
        self.rawBytes = rawBytes
    }

    public var value: Int? {
        guard !rawBytes.isEmpty else { return nil }
        let payload: ArraySlice<UInt8>
        if rawBytes.first == code, rawBytes.count > 1 {
            payload = rawBytes.dropFirst()
        } else {
            payload = rawBytes[...]
        }

        guard let first = payload.first else { return nil }
        if code == ADCUsage.verticalSync, payload.count >= 2 {
            let second = payload[payload.index(after: payload.startIndex)]
            return Int(first) | (Int(second) << 8)
        }
        return Int(first)
    }

    public var bytesDescription: String {
        rawBytes.map { "0x\($0.hex2)" }.joined(separator: " ")
    }
}

public final class MacHIDTransport {
    private var manager: IOHIDManager?
    private var device: IOHIDDevice?

    public init() {}

    deinit {
        close()
    }

    public static func listDevices() -> [HIDDeviceSummary] {
        var iterator: io_iterator_t = 0
        let status = IOServiceGetMatchingServices(
            kIOMainPortDefault,
            IOServiceMatching("IOHIDInterface"),
            &iterator
        )
        guard status == KERN_SUCCESS else {
            return []
        }
        defer {
            IOObjectRelease(iterator)
        }

        var summaries: [HIDDeviceSummary] = []
        while true {
            let service = IOIteratorNext(iterator)
            if service == MACH_PORT_NULL {
                break
            }
            defer {
                IOObjectRelease(service)
            }
            summaries.append(summary(for: service))
        }

        return summaries.sorted {
            ($0.vendorID, $0.productID, $0.product, $0.primaryUsagePage, $0.primaryUsage)
                < ($1.vendorID, $1.productID, $1.product, $1.primaryUsagePage, $1.primaryUsage)
        }
    }

    @discardableResult
    public func connect() throws -> HIDDeviceSummary {
        if let device {
            return Self.summary(for: device)
        }

        let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
        let matching: [String: Any] = [
            kIOHIDVendorIDKey: ADCUsage.vendorID,
            kIOHIDProductIDKey: ADCUsage.productID,
            kIOHIDPrimaryUsagePageKey: ADCUsage.primaryUsagePage,
            kIOHIDPrimaryUsageKey: ADCUsage.primaryUsage,
        ]
        IOHIDManagerSetDeviceMatching(manager, matching as CFDictionary)
        IOHIDManagerScheduleWithRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)

        let managerOpen = IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone))
        guard managerOpen == kIOReturnSuccess else {
            IOHIDManagerUnscheduleFromRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
            throw ADCError.deviceOpenFailed(String(managerOpen))
        }
        CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.10, false)

        guard let devices = IOHIDManagerCopyDevices(manager) as? Set<IOHIDDevice>,
              let selected = devices.first
        else {
            IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            IOHIDManagerUnscheduleFromRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
            throw ADCError.deviceNotFound
        }

        let deviceOpen = IOHIDDeviceOpen(selected, IOOptionBits(kIOHIDOptionsTypeNone))
        guard deviceOpen == kIOReturnSuccess else {
            IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            IOHIDManagerUnscheduleFromRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
            throw ADCError.deviceOpenFailed(String(deviceOpen))
        }

        self.manager = manager
        self.device = selected
        return Self.summary(for: selected)
    }

    public func close() {
        if let device {
            IOHIDDeviceClose(device, IOOptionBits(kIOHIDOptionsTypeNone))
        }
        if let manager {
            IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            IOHIDManagerUnscheduleFromRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
        }
        device = nil
        manager = nil
    }

    public func readFeature(code: UInt8) throws -> HIDFeatureReport {
        let device = try openDevice()
        var buffer = [UInt8](repeating: 0, count: 132)
        var length = buffer.count

        let status = buffer.withUnsafeMutableBufferPointer { pointer in
            IOHIDDeviceGetReport(
                device,
                kIOHIDReportTypeFeature,
                CFIndex(code),
                pointer.baseAddress!,
                &length
            )
        }

        guard status == kIOReturnSuccess else {
            throw ADCError.reportReadFailed(code: code, status: status)
        }

        return HIDFeatureReport(code: code, rawBytes: Array(buffer.prefix(length)))
    }

    public func readControl(_ control: ADCControl) throws -> Int {
        guard let value = try readFeature(code: control.code).value else {
            throw ADCError.reportReadFailed(code: control.code, status: -1)
        }
        return value
    }

    public func readAllControls() throws -> [UInt8: Int] {
        var values: [UInt8: Int] = [:]
        for control in ADCControls.all {
            values[control.code] = try readControl(control)
        }
        return values
    }

    public func readVerticalSyncHz() throws -> Double {
        guard let value = try readFeature(code: ADCUsage.verticalSync).value else {
            throw ADCError.reportReadFailed(code: ADCUsage.verticalSync, status: -1)
        }
        return Double(value) / 100.0
    }

    public func readPowerState() throws -> ADCPowerState {
        let value = try readFeature(code: ADCUsage.power).value
        return ADCPowerState(rawHIDValue: value)
    }

    public func writePowerState(on: Bool) throws {
        try writeFeature(code: ADCUsage.power, payload: [on ? 1 : 4])
    }

    public func writeControl(_ control: ADCControl, value: Int) throws {
        try writeFeature(code: control.code, payload: [UInt8(control.clamped(value)), 0])
    }

    public func degauss() throws {
        try writeFeature(code: ADCUsage.degauss, payload: [1])
    }

    public func saveSettings() throws {
        try writeFeature(code: ADCUsage.save, payload: [1])
    }

    public func writeFactoryDefaults() throws {
        for control in ADCControls.all {
            try writeControl(control, value: control.factoryDefault)
        }
    }

    public func writeFeature(code: UInt8, payload: [UInt8]) throws {
        let device = try openDevice()

        var candidates: [[UInt8]] = []
        candidates.append([code] + payload)

        var trimmed = ([code] + payload)
        while trimmed.count > 2, trimmed.last == 0 {
            trimmed.removeLast()
        }
        if trimmed != candidates[0] {
            candidates.append(trimmed)
        }

        var lastStatus: IOReturn = kIOReturnError
        for candidate in candidates {
            var bytes = candidate
            let byteCount = bytes.count
            let status = bytes.withUnsafeMutableBufferPointer { pointer in
                IOHIDDeviceSetReport(
                    device,
                    kIOHIDReportTypeFeature,
                    CFIndex(code),
                    pointer.baseAddress!,
                    byteCount
                )
            }
            if status == kIOReturnSuccess {
                return
            }
            lastStatus = status
        }

        throw ADCError.reportWriteFailed(code: code, status: lastStatus)
    }

    private func openDevice() throws -> IOHIDDevice {
        if let device {
            return device
        }
        try connect()
        guard let device else {
            throw ADCError.deviceNotFound
        }
        return device
    }

    private static func summary(for device: IOHIDDevice) -> HIDDeviceSummary {
        HIDDeviceSummary(
            vendorID: intProperty(device, kIOHIDVendorIDKey),
            productID: intProperty(device, kIOHIDProductIDKey),
            product: stringProperty(device, kIOHIDProductKey),
            manufacturer: stringProperty(device, kIOHIDManufacturerKey),
            primaryUsagePage: intProperty(device, kIOHIDPrimaryUsagePageKey),
            primaryUsage: intProperty(device, kIOHIDPrimaryUsageKey),
            maxFeatureReportSize: intProperty(device, kIOHIDMaxFeatureReportSizeKey)
        )
    }

    private static func summary(for service: io_service_t) -> HIDDeviceSummary {
        HIDDeviceSummary(
            vendorID: intProperty(service, "VendorID"),
            productID: intProperty(service, "ProductID"),
            product: stringProperty(service, "Product"),
            manufacturer: stringProperty(service, "Manufacturer"),
            primaryUsagePage: intProperty(service, "PrimaryUsagePage"),
            primaryUsage: intProperty(service, "PrimaryUsage"),
            maxFeatureReportSize: intProperty(service, "MaxFeatureReportSize")
        )
    }

    private static func intProperty(_ device: IOHIDDevice, _ key: String) -> Int {
        if let value = IOHIDDeviceGetProperty(device, key as CFString) as? NSNumber {
            return value.intValue
        }
        if let value = IOHIDDeviceGetProperty(device, key as CFString) as? Int {
            return value
        }
        return 0
    }

    private static func stringProperty(_ device: IOHIDDevice, _ key: String) -> String {
        IOHIDDeviceGetProperty(device, key as CFString) as? String ?? ""
    }

    private static func intProperty(_ service: io_service_t, _ key: String) -> Int {
        registryProperty(service, key: key) as? Int ?? (registryProperty(service, key: key) as? NSNumber)?.intValue ?? 0
    }

    private static func stringProperty(_ service: io_service_t, _ key: String) -> String {
        registryProperty(service, key: key) as? String ?? ""
    }

    private static func registryProperty(_ service: io_service_t, key: String) -> Any? {
        guard let value = IORegistryEntryCreateCFProperty(
            service,
            key as CFString,
            kCFAllocatorDefault,
            0
        ) else {
            return nil
        }
        return value.takeRetainedValue()
    }
}
