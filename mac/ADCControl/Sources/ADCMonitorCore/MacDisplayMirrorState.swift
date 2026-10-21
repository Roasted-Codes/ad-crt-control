import CoreGraphics
import Foundation

struct MacDisplayMirrorState: Equatable {
    struct Entry: Equatable {
        let displayID: CGDirectDisplayID
        let mirrorsDisplayID: CGDirectDisplayID
    }

    let entries: [Entry]

    static func capture() -> MacDisplayMirrorState {
        var displayIDs = [CGDirectDisplayID](repeating: 0, count: 16)
        var displayCount: UInt32 = 0
        guard CGGetOnlineDisplayList(UInt32(displayIDs.count), &displayIDs, &displayCount) == .success else {
            return MacDisplayMirrorState(entries: [])
        }

        return MacDisplayMirrorState(
            entries: displayIDs.prefix(Int(displayCount)).map {
                Entry(displayID: $0, mirrorsDisplayID: CGDisplayMirrorsDisplay($0))
            }.sorted { $0.displayID < $1.displayID }
        )
    }

    func restoreIfChanged() throws {
        let current = MacDisplayMirrorState.capture()
        guard current != self else {
            return
        }

        let currentMirrors = Dictionary(uniqueKeysWithValues: current.entries.map { ($0.displayID, $0.mirrorsDisplayID) })
        let changedEntries = entries.filter { currentMirrors[$0.displayID] != nil && currentMirrors[$0.displayID] != $0.mirrorsDisplayID }
        guard !changedEntries.isEmpty else {
            return
        }

        var config: CGDisplayConfigRef?
        let begin = CGBeginDisplayConfiguration(&config)
        guard begin == .success, let config else {
            throw ADCError.displayModeSetFailed("mirror restore begin returned \(begin.rawValue).")
        }

        for entry in changedEntries {
            let result = CGConfigureDisplayMirrorOfDisplay(config, entry.displayID, entry.mirrorsDisplayID)
            guard result == .success else {
                CGCancelDisplayConfiguration(config)
                throw ADCError.displayModeSetFailed("mirror restore configure returned \(result.rawValue).")
            }
        }

        let complete = CGCompleteDisplayConfiguration(config, .forSession)
        guard complete == .success else {
            throw ADCError.displayModeSetFailed("mirror restore complete returned \(complete.rawValue).")
        }
    }
}
