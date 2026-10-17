import Foundation

/// Local task ownership only. Never serialized into the sealed wire metadata.
public struct UploadSessionIdentity: Codable, Equatable, Hashable, Sendable {
    public let kind: String
    public let scope: String
    public static let background = UploadSessionIdentity(kind: "background", scope: "persistent")
    public static func audio(_ process: UUID) -> Self { Self(kind: "activeAudio", scope: process.uuidString.lowercased()) }
    public var isValid: Bool {
        self == .background || (kind == "activeAudio" && UUID(uuidString: scope)?.uuidString.lowercased() == scope)
    }
}
public enum AudioUploadEligibility {
    public static func session(engineRunning: Bool, wanted: Bool, paused: Bool, id: UUID) -> UUID? {
        engineRunning && wanted && !paused ? id : nil
    }
    public static func canStartFast(chunk: Chunk, runtimeSession: UUID?, fastCount: Int, backgroundCount: Int, minimumSequence: Int = 0) -> Bool {
        chunk.isFinalized && chunk.sessionID == runtimeSession && chunk.sequence >= minimumSequence && fastCount == 0 && backgroundCount <= 2
    }
}
public struct UploadResponseKey: Hashable, Sendable {
    public let session: UploadSessionIdentity
    public let taskID: Int
    public init(session: UploadSessionIdentity, taskID: Int) { self.session = session; self.taskID = taskID }
}
public final class UploadResponseBuffers: @unchecked Sendable {
    private let lock = NSLock()
    private var buffers: [UploadResponseKey: Data] = [:]
    public init() {}
    public func append(_ bytes: Data, for key: UploadResponseKey) -> Bool {
        lock.lock(); defer { lock.unlock() }
        let previous = buffers[key] ?? Data()
        guard key.session.isValid, buffers[key] != nil || buffers.count < 16,
              previous.count + bytes.count <= SealedTransport.maxResponseBytes else { return false }
        buffers[key] = previous + bytes; return true
    }
    public func take(_ key: UploadResponseKey) -> Data {
        lock.lock(); defer { lock.unlock() }; return buffers.removeValue(forKey: key) ?? Data()
    }
}

/// Serial-owner snapshot used to keep late failures from overwriting a replacement task.
public struct UploadOwnershipLedger: Sendable {
    private var owners: [UUID: Set<UUID>] = [:]
    public init() {}
    public mutating func clear() { owners.removeAll() }
    public mutating func add(event: UUID, attempt: UUID) { owners[event, default: []].insert(attempt) }
    public mutating func remove(event: UUID, attempt: UUID) {
        owners[event]?.remove(attempt)
        if owners[event]?.isEmpty == true { owners.removeValue(forKey: event) }
    }
    public func hasOtherOwner(event: UUID, excluding attempt: UUID) -> Bool {
        !(owners[event, default: []].subtracting([attempt])).isEmpty
    }
}
