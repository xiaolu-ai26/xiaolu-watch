import Foundation
import WatchMemoryCore
import Darwin

struct PhoneContextFlags: Codable { let healthEnabled: Bool; let locationEnabled: Bool; let shareDerivedWithAi: Bool; let controlRevision: Int }
struct PhoneContextState: Codable { let health: String; let location: String }
struct PhoneContextBody: Encodable {
    let version = 3; let requestId: String; let observedAt: String; let timezone: String
    let functionFlags: PhoneContextFlags; let collectionState: PhoneContextState
    let health: [PhoneHealthSample]; let healthDeleted: [String]; let places: [PhonePlaceSample]
}
struct PhoneContextPacket: Codable {
    let id: String; let principalID: String; let kind: String; let payload: Data
    let checkpointKind: String?; let checkpointData: Data?
    var attempts: Int; var nextAt: Date
}
struct PhonePendingDeletion: Codable {
    let principalID: String; let plan: PhoneDeletionPlan; let requestID: String
}

enum PhoneContextFiles {
    static func directory() throws -> URL {
        let url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("WatchMemory/phone-context-v3", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: url.path)
        var u = url; var values = URLResourceValues(); values.isExcludedFromBackup = true; try u.setResourceValues(values)
        return url
    }
    static func write(_ data: Data, to url: URL) throws {
        try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        let f = try FileHandle(forWritingTo: url); defer { try? f.close() }; try f.synchronize()
    }
    static func packets() throws -> [PhoneContextPacket] {
        let root = try directory()
        let files = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]).filter { $0.pathExtension == "packet" }
        guard files.count <= 256 else { throw StoreError.invalidMetadata }
        return try files.map { u in
            let v = try u.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
            guard v.isRegularFile == true, v.isSymbolicLink != true, (v.fileSize ?? Int.max) <= 2 * 1024 * 1024 else { throw StoreError.unsafePath }
            let p = try JSONDecoder().decode(PhoneContextPacket.self, from: Data(contentsOf: u))
            guard PhoneV3Policy.uuid(p.id), u.lastPathComponent == p.id + ".packet", p.payload.count <= 256 * 1024 else { throw StoreError.invalidMetadata }
            return p
        }.sorted { $0.nextAt < $1.nextAt }
    }
    static func save(_ packet: PhoneContextPacket) throws {
        guard PhoneV3Policy.uuid(packet.id) else { throw StoreError.invalidMetadata }
        let root = try directory(); let path = root.appendingPathComponent(packet.id + ".packet")
        if !FileManager.default.fileExists(atPath: path.path) {
            let all = try packets(); guard all.count < 256, all.reduce(0, { $0 + $1.payload.count }) + packet.payload.count <= 20 * 1024 * 1024 else { throw StoreError.invalidState }
        }
        try write(JSONEncoder().encode(packet), to: path)
    }
    static func remove(_ packet: PhoneContextPacket) throws {
        let root = try directory()
        for suffix in [".packet", ".payload"] {
            let p = root.appendingPathComponent(packet.id + suffix)
            if FileManager.default.fileExists(atPath: p.path) { try FileManager.default.removeItem(at: p) }
        }
    }
}
