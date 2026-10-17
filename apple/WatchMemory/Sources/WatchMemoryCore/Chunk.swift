import Foundation

public enum ChunkState: String, Codable, Sendable {
    case recording, needsRecovery, ready, transferring, awaitingReceipt, received, uploading, uploaded, failed, cleared
}

public struct Chunk: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let sessionID: UUID
    public let sequence: Int
    public let capturedAt: Date
    public var duration: Double
    public var sha256: String
    public var byteCount: Int64
    public var fileName: String
    public var recoveryFileName: String?
    public var state: ChunkState
    public var attempts: Int
    public var lastAttemptAt: Date?
    public var acknowledgedAt: Date?
    public var phoneAcknowledgedAt: Date?
    public var lastError: String?
    public init(id: UUID = UUID(), sessionID: UUID, sequence: Int, capturedAt: Date,
                fileName: String, state: ChunkState = .recording) {
        self.id = id; self.sessionID = sessionID; self.sequence = sequence; self.capturedAt = capturedAt
        self.fileName = fileName; self.state = state; duration = 0; sha256 = ""; byteCount = 0; attempts = 0
    }
    public var uploadMetadata: UploadMetadata {
        UploadMetadata(chunk: self)
    }
    public var isFinalized: Bool { !sha256.isEmpty && byteCount > 0 && duration > 0 }
}

public struct UploadMetadata: Codable, Equatable, Sendable {
    public let schema_version: Int
    public let chunk_id: String
    public let session_id: String
    public let sequence: Int
    public let capture_started_at: String
    public let duration_s: Double
    public let sha256: String
    public let byte_count: Int64
    public let original_name: String
    public let mime: String
    public let sample_rate_hz: Int
    public let channels: Int
    public let source_app: String
    public let device: String
    public let is_test: Bool
    public init(chunk: Chunk) {
        schema_version = 1; chunk_id = chunk.id.uuidString.lowercased(); session_id = chunk.sessionID.uuidString.lowercased()
        sequence = chunk.sequence; capture_started_at = ISO8601DateFormatter().string(from: chunk.capturedAt)
        duration_s = chunk.duration; sha256 = chunk.sha256; byte_count = chunk.byteCount
        original_name = chunk.fileName; mime = chunk.fileName.hasSuffix(".m4a") ? "audio/mp4" : "audio/x-caf"; sample_rate_hz = 16000; channels = 1
        source_app = "watch-memory"; device = "Apple Watch"; is_test = false
    }
    public func makeChunk() throws -> Chunk {
        guard schema_version == 1, let id = UUID(uuidString: chunk_id), let session = UUID(uuidString: session_id),
              sequence >= 0, let date = ISO8601DateFormatter().date(from: capture_started_at),
              duration_s.isFinite, duration_s > 0, duration_s <= 180,
              byte_count > 0, byte_count <= 16 * 1024 * 1024,
              sha256.count == 64, sha256.allSatisfy({ "0123456789abcdef".contains($0) }),
              sample_rate_hz == 16000, channels == 1,
              ((original_name == id.uuidString.lowercased() + ".caf" && mime == "audio/x-caf") ||
               (original_name == id.uuidString.lowercased() + ".m4a" && mime == "audio/mp4")),
              source_app == "watch-memory", device == "Apple Watch" else { throw StoreError.invalidMetadata }
        var chunk = Chunk(id: id, sessionID: session, sequence: sequence, capturedAt: date,
                          fileName: original_name, state: .ready)
        chunk.duration = duration_s; chunk.byteCount = byte_count; chunk.sha256 = sha256
        return chunk
    }
}

public enum StoreError: Error { case invalidMetadata, missingChunk, invalidState, receiptMismatch, unsafePath }

public struct ServerReceipt: Codable, Sendable {
    public let schema_version: Int
    public let `protocol`: String
    public let stored: Bool
    public let sha256: String
    public let source_id: String
    public let chunk_id: String
    public let session_id: String
    public let sequence: Int
    public let status: String
    public init(stored: Bool, sha256: String, source_id: String, chunk_id: String, session_id: String,
                sequence: Int, status: String = "accepted", schema_version: Int = 2, protocolName: String = "watch-memory-chunks-v2") {
        self.schema_version = schema_version; self.protocol = protocolName
        self.stored = stored; self.sha256 = sha256; self.source_id = source_id
        self.chunk_id = chunk_id; self.session_id = session_id; self.sequence = sequence; self.status = status
    }
    public func validates(statusCode: Int, chunk: Chunk) -> Bool {
        (200...299).contains(statusCode) && schema_version == 2 && self.protocol == "watch-memory-chunks-v2" && stored && sha256 == chunk.sha256 && source_id == chunk.sha256 &&
        UUID(uuidString: chunk_id) == chunk.id && UUID(uuidString: session_id) == chunk.sessionID && sequence == chunk.sequence &&
        ["accepted", "already_accepted"].contains(status)
    }
}
