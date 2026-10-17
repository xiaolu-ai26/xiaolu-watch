import Foundation

public struct RecordingIncident: Codable, Sendable {
    public let at: Date
    public let event: String
    public let outcome: String
    public let fields: [String: String]
}
public struct RecordingCheckpoint: Codable, Sendable {
    public let cleanEnd: Bool
    public let sessionID: UUID?
    public let updatedAt: Date
    public let lastPCMAt: Date?
    public let lastKnownEvent: String
}

/// Small durable metadata journals. No audio text, route/device names or credentials.
public final class RecordingDiagnostics: @unchecked Sendable {
    private let directory: URL
    private let lock = NSRecursiveLock()
    private static let events: Set<String> = ["system_interruption", "automatic_resume", "manual_resume", "activation_failed", "no_resume_hint", "media_reset", "previous_session_unfinished", "files_pending_recovery", "pcm_stall_suspected", "pcm_flow_resumed", "capture_error"]
    private static let keys: Set<String> = ["reasonCode", "sourceCode", "domain", "code", "recoveryCount", "lastKnownEvent", "unknownExitReason", "lastPCMAt"]
    public init(directory: URL) throws {
        self.directory = directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    private func save<T: Encodable>(_ value: T, _ name: String) throws {
        let data = try JSONEncoder().encode(value)
        guard data.count <= 65536 else { throw StoreError.invalidMetadata }
        let url = directory.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        let f = try FileHandle(forWritingTo: url); defer { try? f.close() }; try f.synchronize()
    }
    public func checkpoint() throws -> RecordingCheckpoint? {
        lock.lock(); defer { lock.unlock() }
        let url = directory.appendingPathComponent("recording-lifecycle.json")
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        let data = try handle.read(upToCount: 4097) ?? Data()
        guard data.count <= 4096 else { throw StoreError.invalidMetadata }
        return try JSONDecoder().decode(RecordingCheckpoint.self, from: data)
    }
    public func update(cleanEnd: Bool, sessionID: UUID?, lastPCMAt: Date?, event: String, now: Date = Date()) throws {
        lock.lock(); defer { lock.unlock() }
        guard event.utf8.count <= 64, event.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "_") }) else { throw StoreError.invalidMetadata }
        try save(RecordingCheckpoint(cleanEnd: cleanEnd, sessionID: sessionID, updatedAt: now, lastPCMAt: lastPCMAt, lastKnownEvent: event), "recording-lifecycle.json")
    }
    public func incidents() throws -> [RecordingIncident] {
        lock.lock(); defer { lock.unlock() }
        let url = directory.appendingPathComponent("recording-incidents.json")
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        let data = try handle.read(upToCount: 65537) ?? Data()
        guard data.count <= 65536 else { throw StoreError.invalidMetadata }
        return try JSONDecoder().decode([RecordingIncident].self, from: data)
    }
    private static func validField(_ key: String, _ value: String) -> Bool {
        guard value.utf8.count <= 128 else { return false }
        switch key {
        case "domain": return !value.isEmpty && value.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || "._-".contains($0)) }
        case "code", "reasonCode", "sourceCode", "recoveryCount": return Int64(value) != nil
        case "lastKnownEvent": return !value.isEmpty && value.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "_") }
        case "unknownExitReason": return value == "unknown"
        case "lastPCMAt": return Double(value).map { $0.isFinite && $0 >= 0 } ?? false
        default: return false
        }
    }
    public func incident(_ event: String, outcome: String, fields: [String: String] = [:], now: Date = Date()) throws {
        lock.lock(); defer { lock.unlock() }
        guard Self.events.contains(event), ["waiting", "recovered", "manual", "unknown"].contains(outcome),
              fields.count <= 8, Set(fields.keys).isSubset(of: Self.keys), fields.allSatisfy({ Self.validField($0.key, $0.value) }) else { throw StoreError.invalidMetadata }
        var all = try incidents(); all.append(RecordingIncident(at: now, event: event, outcome: outcome, fields: fields))
        try save(Array(all.suffix(32)), "recording-incidents.json")
    }
}
