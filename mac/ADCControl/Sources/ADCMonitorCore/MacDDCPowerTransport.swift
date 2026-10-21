import CoreGraphics
import Foundation
import IOKit

private typealias IOAVServiceRef = CFTypeRef

@_silgen_name("IOAVServiceCreate")
private func IOAVServiceCreate(_ allocator: CFAllocator?) -> IOAVServiceRef?

@_silgen_name("IOAVServiceCreateWithService")
private func IOAVServiceCreateWithService(_ allocator: CFAllocator?, _ service: io_service_t) -> IOAVServiceRef?

@_silgen_name("IOAVServiceWriteI2C")
private func IOAVServiceWriteI2C(
    _ service: IOAVServiceRef,
    _ chipAddress: UInt32,
    _ dataAddress: UInt32,
    _ inputBuffer: UnsafeRawPointer,
    _ inputBufferSize: UInt32
) -> IOReturn

@_silgen_name("IOAVServiceReadI2C")
private func IOAVServiceReadI2C(
    _ service: IOAVServiceRef,
    _ chipAddress: UInt32,
    _ offset: UInt32,
    _ outputBuffer: UnsafeMutableRawPointer,
    _ outputBufferSize: UInt32
) -> IOReturn

@_silgen_name("IOAVServiceCopyEDID")
private func IOAVServiceCopyEDID(_ service: IOAVServiceRef, _ edid: UnsafeMutablePointer<CFData?>) -> IOReturn

@_silgen_name("IOAVServiceSetVirtualEDIDMode")
private func IOAVServiceSetVirtualEDIDMode(
    _ service: IOAVServiceRef,
    _ mode: UInt32,
    _ edid: CFData?
) -> IOReturn

@_silgen_name("CoreDisplay_DisplayCreateInfoDictionary")
private func CoreDisplay_DisplayCreateInfoDictionary(_ display: CGDirectDisplayID) -> CFDictionary?

public struct DDCDisplay: Identifiable, Sendable {
    public let index: Int
    public let displayID: CGDirectDisplayID
    public let uuid: String
    public let edidUUID: String
    public let productName: String
    public let manufacturer: String
    public let serial: UInt32
    public let model: UInt32
    public let vendor: UInt32
    public let ioLocation: String
    public let hasAVService: Bool
    public let isBuiltIn: Bool
    public let isInMirrorSet: Bool
    public let isInHardwareMirrorSet: Bool
    public let mirrorsDisplayID: CGDirectDisplayID

    public var id: String { uuid.isEmpty ? String(displayID) : uuid }

    public var isADCStudioDisplay: Bool {
        vendor == ADCUsage.displayVendorID &&
            (model == ADCUsage.productID || productName.localizedCaseInsensitiveContains("StudioDisplay"))
    }

    public var isExternalADCStudioDisplay: Bool {
        isADCStudioDisplay && !isBuiltIn
    }

    public var isMirrored: Bool {
        isInMirrorSet || isInHardwareMirrorSet || mirrorsDisplayID != CGDirectDisplayID(kCGNullDirectDisplay)
    }
}

public struct DDCPacket: Sendable, Equatable {
    public static let chipAddress: UInt32 = 0x37
    public static let inputAddress: UInt8 = 0x51
    public static let outputAddress: UInt8 = 0x6E

    public let vcpCode: UInt8
    public let value: UInt16
    public let bytes: [UInt8]

    public static func setVCP(_ code: UInt8, value: UInt16) -> DDCPacket {
        let high = UInt8((value >> 8) & 0xFF)
        let low = UInt8(value & 0xFF)
        let checksum = outputAddress ^ inputAddress ^ 0x84 ^ 0x03 ^ code ^ high ^ low
        return DDCPacket(
            vcpCode: code,
            value: value,
            bytes: [0x84, 0x03, code, high, low, checksum]
        )
    }

    public static func power(on: Bool) -> DDCPacket {
        setVCP(ADCUsage.power, value: on ? 1 : 4)
    }

    public var fullPiStyleBytes: [UInt8] {
        [Self.inputAddress] + bytes
    }
}

public final class MacDDCPowerTransport {
    private static let ddcWaitMicroseconds: useconds_t = 10_000
    private static let ddcIterations = 2

    public init() {}

    public func listDisplays() -> [DDCDisplay] {
        var displayIDs = [CGDirectDisplayID](repeating: 0, count: 8)
        var displayCount: UInt32 = 0
        guard CGGetOnlineDisplayList(UInt32(displayIDs.count), &displayIDs, &displayCount) == .success else {
            return []
        }

        var displays: [DDCDisplay] = []
        for displayID in displayIDs.prefix(Int(displayCount)) {
            guard let display = displayInfo(for: displayID, index: displays.count + 1) else {
                continue
            }
            displays.append(display)
        }
        return displays
    }

    public func preferredADCDisplay() throws -> DDCDisplay {
        let displays = listDisplays()
        if let display = displays.first(where: { $0.isExternalADCStudioDisplay && $0.hasAVService }) {
            return display
        }
        if let display = displays.first(where: { $0.isExternalADCStudioDisplay }) {
            return display
        }
        throw ADCError.displayNotFound
    }

    public func setPower(on: Bool, display: DDCDisplay? = nil) throws {
        let target = try display ?? preferredADCDisplay()
        try validateADCDisplay(target)
        guard let avService = avService(for: target) else {
            throw ADCError.ddcUnavailable("No external DCPAVServiceProxy found for display \(target.index).")
        }

        let packet = DDCPacket.power(on: on).bytes
        var lastStatus: IOReturn = kIOReturnSuccess
        for _ in 0..<Self.ddcIterations {
            usleep(Self.ddcWaitMicroseconds)
            lastStatus = packet.withUnsafeBytes { pointer in
                IOAVServiceWriteI2C(
                    avService,
                    DDCPacket.chipAddress,
                    UInt32(DDCPacket.inputAddress),
                    pointer.baseAddress!,
                    UInt32(packet.count)
                )
            }
            if lastStatus != kIOReturnSuccess {
                throw ADCError.ddcWriteFailed("IOReturn \(lastStatus)")
            }
        }
    }

    public func copyEDID(display: DDCDisplay? = nil) throws -> Data {
        let target = try display ?? preferredADCDisplay()
        try validateADCDisplay(target)
        guard let avService = avService(for: target) else {
            throw ADCError.ddcUnavailable("No external DCPAVServiceProxy found for display \(target.index).")
        }
        var edid: CFData?
        let result = IOAVServiceCopyEDID(avService, &edid)
        guard result == kIOReturnSuccess else {
            throw ADCError.ddcUnavailable("IOAVServiceCopyEDID returned \(result).")
        }
        guard let edid else {
            throw ADCError.ddcUnavailable("No EDID data returned for display \(target.index).")
        }
        return edid as Data
    }

    public func readEDIDOverI2C(display: DDCDisplay? = nil) throws -> Data {
        let target = try display ?? preferredADCDisplay()
        try validateADCDisplay(target)
        guard let avService = avService(for: target) else {
            throw ADCError.ddcUnavailable("No external DCPAVServiceProxy found for display \(target.index).")
        }

        var bytes = [UInt8](repeating: 0, count: 128)
        let result = bytes.withUnsafeMutableBytes { buffer in
            IOAVServiceReadI2C(
                avService,
                0x50,
                0,
                buffer.baseAddress!,
                UInt32(buffer.count)
            )
        }
        guard result == kIOReturnSuccess else {
            throw ADCError.ddcUnavailable("IOAVServiceReadI2C returned \(result).")
        }
        return Data(bytes)
    }

    public func setVirtualEDID(_ edid: Data, display: DDCDisplay? = nil) throws {
        let target = try display ?? preferredADCDisplay()
        try validateADCDisplay(target)
        guard let avService = avService(for: target) else {
            throw ADCError.ddcUnavailable("No external DCPAVServiceProxy found for display \(target.index).")
        }

        let mirrorState = MacDisplayMirrorState.capture()
        let result = IOAVServiceSetVirtualEDIDMode(avService, 1, edid as CFData)
        guard result == kIOReturnSuccess else {
            throw ADCError.ddcUnavailable("IOAVServiceSetVirtualEDIDMode returned \(result).")
        }
        try mirrorState.restoreIfChanged()
    }

    public func resetVirtualEDID(display: DDCDisplay? = nil) throws {
        let target = try display ?? preferredADCDisplay()
        try validateADCDisplay(target)
        guard let avService = avService(for: target) else {
            throw ADCError.ddcUnavailable("No external DCPAVServiceProxy found for display \(target.index).")
        }

        let mirrorState = MacDisplayMirrorState.capture()
        let result = IOAVServiceSetVirtualEDIDMode(avService, 0, nil)
        guard result == kIOReturnSuccess else {
            throw ADCError.ddcUnavailable("IOAVServiceSetVirtualEDIDMode reset returned \(result).")
        }
        try mirrorState.restoreIfChanged()
    }

    private func displayInfo(for displayID: CGDirectDisplayID, index: Int) -> DDCDisplay? {
        guard let info = CoreDisplay_DisplayCreateInfoDictionary(displayID) as NSDictionary? else {
            return nil
        }

        let uuid = info["kCGDisplayUUID"] as? String ?? ""
        let ioLocation = info["IODisplayLocation"] as? String ?? ""
        guard !uuid.isEmpty, !ioLocation.isEmpty else {
            return nil
        }

        let adapter = IORegistryEntryCopyFromPath(kIOMainPortDefault, ioLocation as CFString)
        defer {
            if adapter != MACH_PORT_NULL {
                IOObjectRelease(adapter)
            }
        }

        let attributes = registryProperty(adapter, key: "DisplayAttributes") as? NSDictionary
        let productAttributes = attributes?["ProductAttributes"] as? NSDictionary

        return DDCDisplay(
            index: index,
            displayID: displayID,
            uuid: uuid,
            edidUUID: registryProperty(adapter, key: "EDID UUID") as? String ?? "",
            productName: productAttributes?["ProductName"] as? String ?? "",
            manufacturer: productAttributes?["ManufacturerID"] as? String ?? "",
            serial: CGDisplaySerialNumber(displayID),
            model: CGDisplayModelNumber(displayID),
            vendor: CGDisplayVendorNumber(displayID),
            ioLocation: ioLocation,
            hasAVService: avService(forAdapterPath: ioLocation) != nil,
            isBuiltIn: CGDisplayIsBuiltin(displayID) != 0,
            isInMirrorSet: CGDisplayIsInMirrorSet(displayID) != 0,
            isInHardwareMirrorSet: CGDisplayIsInHWMirrorSet(displayID) != 0,
            mirrorsDisplayID: CGDisplayMirrorsDisplay(displayID)
        )
    }

    private func validateADCDisplay(_ display: DDCDisplay) throws {
        guard display.isExternalADCStudioDisplay else {
            let displayName = display.productName.isEmpty ? "display \(display.index)" : display.productName
            throw ADCError.ddcUnavailable("Refusing to use \(displayName); target must be the external Apple Studio Display 0x\(String(format: "%04X", ADCUsage.productID)).")
        }
    }

    private func avService(for display: DDCDisplay) -> IOAVServiceRef? {
        avService(forAdapterPath: display.ioLocation)
    }

    private func avService(forAdapterPath adapterPath: String) -> IOAVServiceRef? {
        let root = IORegistryGetRootEntry(kIOMainPortDefault)
        guard root != MACH_PORT_NULL else {
            return nil
        }
        defer {
            IOObjectRelease(root)
        }

        var iterator: io_iterator_t = 0
        let iteratorStatus = IORegistryEntryCreateIterator(
            root,
            kIOServicePlane,
            IOOptionBits(kIORegistryIterateRecursively),
            &iterator
        )
        guard iteratorStatus == KERN_SUCCESS else {
            return nil
        }
        defer {
            IOObjectRelease(iterator)
        }

        var foundDisplayPath = false
        while true {
            let service = IOIteratorNext(iterator)
            if service == MACH_PORT_NULL {
                break
            }
            defer {
                IOObjectRelease(service)
            }

            if !foundDisplayPath {
                foundDisplayPath = registryPath(service) == adapterPath
                continue
            }

            if registryName(service) == "DCPAVServiceProxy",
               registryProperty(service, key: "Location") as? String == "External",
               let avService = IOAVServiceCreateWithService(kCFAllocatorDefault, service)
            {
                return avService
            }
        }

        return nil
    }

    private func registryPath(_ service: io_service_t) -> String {
        var path = [CChar](repeating: 0, count: 4096)
        let status = path.withUnsafeMutableBufferPointer {
            IORegistryEntryGetPath(service, kIOServicePlane, $0.baseAddress)
        }
        guard status == KERN_SUCCESS else {
            return ""
        }
        return string(from: path)
    }

    private func registryName(_ service: io_service_t) -> String {
        var name = [CChar](repeating: 0, count: 128)
        let status = name.withUnsafeMutableBufferPointer {
            IORegistryEntryGetName(service, $0.baseAddress)
        }
        guard status == KERN_SUCCESS else {
            return ""
        }
        return string(from: name)
    }

    private func registryProperty(_ service: io_service_t, key: String) -> Any? {
        guard service != MACH_PORT_NULL,
              let value = IORegistryEntrySearchCFProperty(
                service,
                kIOServicePlane,
                key as CFString,
                kCFAllocatorDefault,
                IOOptionBits(kIORegistryIterateRecursively)
              )
        else {
            return nil
        }
        return value
    }

    private func string(from cStringBuffer: [CChar]) -> String {
        let end = cStringBuffer.firstIndex(of: 0) ?? cStringBuffer.count
        return String(decoding: cStringBuffer[..<end].map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }
}
