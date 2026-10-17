import Foundation

/// Derived ciphertext and attempt context are persisted before URLSession may send.
/// Audio custody remains exclusively with ChunkStore; this class never deletes audio.
public final class UploadAttemptStore: @unchecked Sendable {
    public let directory: URL
    private let lock = NSRecursiveLock()
    public init(directory: URL) throws {
        self.directory = directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    public func cipherURL(_ attempt: UploadAttempt) throws -> URL {
        guard attempt.cipherFileName == attempt.id.uuidString.lowercased() + ".sealed",
              attempt.challenge == attempt.id.uuidString.lowercased() else { throw TransportError.invalidChallenge }
        return directory.appendingPathComponent(attempt.cipherFileName)
    }
    private func recordURL(_ id: UUID) -> URL { directory.appendingPathComponent(id.uuidString.lowercased() + ".json") }
    private func durableWrite(_ data: Data, to url: URL) throws {
        try data.write(to: url, options: .atomic)
        let file = try FileHandle(forWritingTo: url); defer { try? file.close() }; try file.synchronize()
    }
    public func prepare(_ attempt: UploadAttempt, ciphertext: Data) throws {
        lock.lock(); defer { lock.unlock() }
        guard ciphertext.count == attempt.cipherByteCount,
              SealedTransport.digest(ciphertext) == attempt.cipherSHA256,
              !FileManager.default.fileExists(atPath: recordURL(attempt.id).path) else { throw TransportError.incompleteAttempt }
        try durableWrite(ciphertext, to: cipherURL(attempt))
        try durableWrite(JSONEncoder().encode(attempt), to: recordURL(attempt.id))
    }
    public func load(_ id: UUID) throws -> UploadAttempt {
        lock.lock(); defer { lock.unlock() }
        let value = try JSONDecoder().decode(UploadAttempt.self, from: Data(contentsOf: recordURL(id)))
        guard value.id == id else { throw TransportError.incompleteAttempt }
        _ = try cipherURL(value)
        return value
    }
    public func bind(_ id: UUID, taskID: Int, session: UploadSessionIdentity = .background) throws {
        lock.lock(); defer { lock.unlock() }
        var attempt = try load(id)
        guard attempt.state == "prepared", session.isValid else { throw TransportError.incompleteAttempt }
        attempt.systemTaskID = taskID; attempt.sessionIdentity = session; attempt.state = "scheduled"
        try durableWrite(JSONEncoder().encode(attempt), to: recordURL(id))
    }
    /// Validate the durable bind before recovering a suspended background task.
    /// A task created before bind completed is intentionally rejected and retried as a new attempt.
    public func validateBoundTask(_ id: UUID, taskID: Int, originalURL: URL, httpMethod: String,
                                  challengeHeader: String, configuration: PairingConfiguration,
                                  checkCiphertext: Bool, session: UploadSessionIdentity = .background) throws -> UploadAttempt {
        lock.lock(); defer { lock.unlock() }
        let attempt = try load(id)
        let base = try configuration.validatedURL()
        _ = try attempt.expectedMetadata.makeChunk()
        guard session.isValid, (attempt.sessionIdentity ?? .background) == session,
              attempt.state == "scheduled", attempt.systemTaskID == taskID,
              attempt.path == "/v2/chunks/" + attempt.expectedMetadata.chunk_id,
              attempt.pairingKeyID == SealedTransport.pairingKeyID(configuration.pairing_secret),
              originalURL == base.appendingPathComponent(String(attempt.path.dropFirst())),
              httpMethod == "PUT", challengeHeader == attempt.challenge else { throw TransportError.incompleteAttempt }
        if checkCiphertext {
            let path = try cipherURL(attempt)
            let handle = try FileHandle(forReadingFrom: path); defer { try? handle.close() }
            let data = try handle.read(upToCount: SealedTransport.maxWireBytes + 1) ?? Data()
            guard data.count <= SealedTransport.maxWireBytes, data.count == attempt.cipherByteCount,
                  SealedTransport.digest(data) == attempt.cipherSHA256 else { throw TransportError.incompleteAttempt }
        }
        return attempt
    }
    /// A default session from a dead process cannot survive. Retain original custody
    /// and fail only its derived context; the caller schedules a new background nonce.
    public func abandonOtherAudioScopes(current: UploadSessionIdentity) throws -> [UUID] {
        lock.lock(); defer { lock.unlock() }
        var events: [UUID] = []
        for file in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) where file.pathExtension == "json" {
            guard let id = UUID(uuidString: file.deletingPathExtension().lastPathComponent),
                  let a = try? load(id), a.state == "scheduled",
                  let identity = a.sessionIdentity, identity.kind == "activeAudio", identity != current,
                  let event = UUID(uuidString: a.expectedMetadata.chunk_id) else { continue }
            try finish(id, succeeded: false); events.append(event)
        }
        return events
    }
    public func finish(_ id: UUID, succeeded: Bool) throws {
        lock.lock(); defer { lock.unlock() }
        var attempt = try load(id); attempt.state = succeeded ? "verified" : "failed"
        try durableWrite(JSONEncoder().encode(attempt), to: recordURL(id))
        let sealed = try cipherURL(attempt)
        if FileManager.default.fileExists(atPath: sealed.path) { try FileManager.default.removeItem(at: sealed) }
    }
    public func reconcileAndCompact(active: Set<UUID>, now: Date = Date()) throws {
        lock.lock(); defer { lock.unlock() }
        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        // prepare() syncs ciphertext before JSON. A crash between those writes leaves
        // a derived .sealed file with no valid context. Never scan ChunkStore/audio dirs.
        for file in files where file.pathExtension == "sealed" {
            let name = file.deletingPathExtension().lastPathComponent
            guard let id = UUID(uuidString: name), name == id.uuidString.lowercased(), !active.contains(id) else { continue }
            let values = try file.resourceValues(forKeys: [.contentModificationDateKey, .isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true,
                  let modified = values.contentModificationDate, now.timeIntervalSince(modified) > 600 else { continue }
            let context = try? load(id)
            if context == nil { try FileManager.default.removeItem(at: file) }
        }
        for file in files where file.pathExtension == "json" {
            guard let id = UUID(uuidString: file.deletingPathExtension().lastPathComponent), !active.contains(id),
                  let attempt = try? load(id) else { continue }
            if now.timeIntervalSince(attempt.createdAt) > 600 && ["prepared", "scheduled"].contains(attempt.state) {
                try finish(id, succeeded: false) // abandoned derived attempt; original audio remains
            }
            if now.timeIntervalSince(attempt.createdAt) > 86400 && ["verified", "failed"].contains(attempt.state) {
                try FileManager.default.removeItem(at: file)
            }
        }
    }
}
