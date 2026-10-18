import Foundation
import CryptoKit

/// Only the manifest is mutable. Unacknowledged audio is never removed by this store.
/// All transitions serialize with the atomic manifest write, including WC delegate calls.
public final class ChunkStore: @unchecked Sendable {
    private struct Manifest: Codable { var version = 1; var chunks: [Chunk] = []; var archivedCount = 0 }
    public let directory: URL
    private let manifestURL: URL
    private let lock = NSRecursiveLock()
    private var manifest: Manifest
    public init(directory: URL) throws {
        self.directory = directory; manifestURL = directory.appendingPathComponent("manifest.json")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: manifestURL.path) {
            manifest = try JSONDecoder().decode(Manifest.self, from: Data(contentsOf: manifestURL))
            guard manifest.version == 1 else { throw StoreError.invalidMetadata }
        } else { manifest = Manifest() }
        for chunk in manifest.chunks { _ = try fileURL(for: chunk) }
    }
    public func all() -> [Chunk] { lock.lock(); defer { lock.unlock() }; return manifest.chunks }
    public func chunk(_ id: UUID) -> Chunk? { all().first { $0.id == id } }
    public func fileURL(for chunk: Chunk) throws -> URL {
        guard !chunk.fileName.contains("/"), !chunk.fileName.contains("\\"), !chunk.fileName.hasPrefix("."),
              (chunk.fileName.hasSuffix(".caf") || chunk.fileName.hasSuffix(".m4a")) else { throw StoreError.unsafePath }
        if let recovery = chunk.recoveryFileName {
            guard recovery == chunk.id.uuidString.lowercased() + ".caf" else { throw StoreError.unsafePath }
        }
        return directory.appendingPathComponent(chunk.fileName)
    }
    private func transaction(_ change: (inout Manifest) throws -> Void) throws {
        lock.lock(); defer { lock.unlock() }
        var changed = manifest
        try change(&changed)
        let data = try JSONEncoder().encode(changed)
        // Atomic replacement preserves the previous valid manifest after interrupted writes.
        try data.write(to: manifestURL, options: .atomic)
        let handle = try FileHandle(forWritingTo: manifestURL)
        try handle.synchronize(); try handle.close()
        manifest = changed
    }
    public func insert(_ chunk: Chunk) throws {
        _ = try fileURL(for: chunk)
        try transaction { value in
            if let existing = value.chunks.first(where: { $0.id == chunk.id }) {
                guard existing.sessionID == chunk.sessionID, existing.sequence == chunk.sequence,
                      existing.sha256 == chunk.sha256, existing.byteCount == chunk.byteCount,
                      existing.capturedAt == chunk.capturedAt, existing.duration == chunk.duration,
                      existing.fileName == chunk.fileName else { throw StoreError.receiptMismatch }
                return
            }
            guard !value.chunks.contains(where: { $0.sessionID == chunk.sessionID && $0.sequence == chunk.sequence }) else {
                throw StoreError.receiptMismatch
            }
            guard value.chunks.count < 8192 else { throw StoreError.invalidState }
            value.chunks.append(chunk)
        }
    }
    public func finalize(_ id: UUID, duration: Double, sha256: String, byteCount: Int64, encodedFileName: String? = nil) throws {
        guard duration.isFinite, duration > 0, byteCount > 0, sha256.count == 64,
              sha256.allSatisfy({ "0123456789abcdef".contains($0) }) else { throw StoreError.invalidMetadata }
        try mutate(id) { chunk in
            guard [.recording, .needsRecovery].contains(chunk.state) else { throw StoreError.invalidState }
            if let name = encodedFileName {
                guard name == chunk.id.uuidString.lowercased() + ".m4a" else { throw StoreError.unsafePath }
                chunk.recoveryFileName = chunk.fileName; chunk.fileName = name
            }
            chunk.duration = duration; chunk.sha256 = sha256; chunk.byteCount = byteCount
            chunk.state = .ready; chunk.lastError = nil
        }
    }
    private func mutate(_ id: UUID, _ body: (inout Chunk) throws -> Void) throws {
        try transaction { value in
            guard let index = value.chunks.firstIndex(where: { $0.id == id }) else { throw StoreError.missingChunk }
            try body(&value.chunks[index])
        }
    }
    public func recoverInterrupted() throws -> [Chunk] {
        try transaction { value in
            for i in value.chunks.indices {
                if value.chunks[i].state == .recording { value.chunks[i].state = .needsRecovery }
                // File transfers/upload tasks are reconciled against system task lists by each app.
            }
        }
        return all().filter { $0.state == .needsRecovery }
    }
    public func markAttempt(_ id: UUID, uploading: Bool, now: Date = Date()) throws {
        try mutate(id) { chunk in
            guard chunk.isFinalized, ![.received, .uploaded, .cleared].contains(chunk.state) else { throw StoreError.invalidState }
            chunk.state = uploading ? .uploading : .transferring
            chunk.attempts += 1; chunk.lastAttemptAt = now; chunk.lastError = nil
        }
    }
    public func transferFinished(_ id: UUID, success: Bool, error: String? = nil) throws {
        try mutate(id) { chunk in
            guard ![.received, .cleared, .uploaded].contains(chunk.state) else { return }
            chunk.state = success ? .awaitingReceipt : .ready; chunk.lastError = error
        }
    }
    public func markFailed(_ id: UUID, message: String) throws {
        try mutate(id) { chunk in
            guard ![.received, .uploaded, .cleared].contains(chunk.state) else { return }
            chunk.state = chunk.isFinalized ? .ready : .needsRecovery; chunk.lastError = message
        }
    }
    public func acknowledgePhone(_ id: UUID, sha256: String, byteCount: Int64, now: Date = Date()) throws {
        try mutate(id) { chunk in
            guard chunk.isFinalized, chunk.sha256 == sha256, chunk.byteCount == byteCount else { throw StoreError.receiptMismatch }
            guard chunk.state != .cleared else { return }
            chunk.phoneAcknowledgedAt = chunk.phoneAcknowledgedAt ?? now
            guard ![.uploading, .uploaded].contains(chunk.state) else { return }
            chunk.state = .received; chunk.acknowledgedAt = chunk.acknowledgedAt ?? now; chunk.lastError = nil
        }
    }
    /// A legacy phone custody ACK is not a receiver ACK. Requeue only a verified
    /// retained original, preserving that historical phone receipt separately.
    public func requeuePhoneAcknowledgedForDirectUpload(_ id: UUID) throws {
        lock.lock(); defer { lock.unlock() }
        guard let item = chunk(id), item.state == .received else { return }
        let path = try fileURL(for: item)
        let file = try FileHandle(forReadingFrom: path); defer { try? file.close() }
        var sha = SHA256(); var size: Int64 = 0
        while let data = try file.read(upToCount: 256 * 1024), !data.isEmpty {
            size += Int64(data.count)
            guard size <= item.byteCount else { throw StoreError.receiptMismatch }
            sha.update(data: data)
        }
        guard size == item.byteCount, sha.finalize().map({ String(format: "%02x", $0) }).joined() == item.sha256 else { throw StoreError.receiptMismatch }
        try mutate(id) {
            $0.phoneAcknowledgedAt = $0.phoneAcknowledgedAt ?? $0.acknowledgedAt
            $0.acknowledgedAt = nil; $0.state = .ready; $0.attempts = 0; $0.lastAttemptAt = nil
        }
    }
    public func acknowledgeServer(_ id: UUID, receipt: ServerReceipt, statusCode: Int, now: Date = Date()) throws {
        try mutate(id) { chunk in
            guard receipt.validates(statusCode: statusCode, chunk: chunk) else { throw StoreError.receiptMismatch }
            guard chunk.state != .cleared else { return }
            if chunk.state != .uploaded { chunk.acknowledgedAt = now }
            chunk.state = .uploaded; chunk.lastError = nil
        }
    }
    /// Owner deletion is not a stored ACK. Retain local bytes in a recoverable namespace and stop retries.
    public func retireOwnerDeleted(_ id: UUID, receipt: OwnerRetirementReceipt, signedBody: Data, mac: String) throws {
        lock.lock(); defer { lock.unlock() }
        guard let chunk = chunk(id), receipt.validates(chunk), signedBody.count <= SealedTransport.maxResponseBytes,
              [id.uuidString.lowercased() + ".caf", id.uuidString.lowercased() + ".m4a"].contains(chunk.fileName) else { throw StoreError.receiptMismatch }
        let recovery = directory.appendingPathComponent("owner-deletion-recovery", isDirectory: true)
        try FileManager.default.createDirectory(at: recovery, withIntermediateDirectories: true)
        let proof = try JSONSerialization.data(withJSONObject: ["purpose": "owner_deletion", "body_base64": signedBody.base64EncodedString(), "mac_base64": mac])
        let proofURL = recovery.appendingPathComponent(id.uuidString.lowercased() + ".retired.json")
        try proof.write(to: proofURL, options: .atomic)
        let f = try FileHandle(forWritingTo: proofURL); try f.synchronize(); try f.close()
        // Persist terminal state first. If a move is interrupted, bytes remain locally recoverable and cannot reupload.
        try mutate(id) { $0.state = .cleared; $0.lastError = "已按确认的删除请求停止上传，本机保留可恢复原音" }
        let names = [chunk.fileName] + (chunk.recoveryFileName.map { [$0] } ?? [])
        for name in names {
            let source = directory.appendingPathComponent(name), target = recovery.appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: source.path) {
                if FileManager.default.fileExists(atPath: target.path) { continue } // Never overwrite recoverable bytes.
                try FileManager.default.moveItem(at: source, to: target)
            }
        }
    }
    public func clearReceivedAudio(_ id: UUID) throws {
        lock.lock(); defer { lock.unlock() }
        guard let chunk = chunk(id), chunk.state == .received else { throw StoreError.invalidState }
        let url = try fileURL(for: chunk)
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
        if let recovery = chunk.recoveryFileName {
            let raw = directory.appendingPathComponent(recovery)
            if FileManager.default.fileExists(atPath: raw.path) { try FileManager.default.removeItem(at: raw) }
        }
        try mutate(id) { $0.state = .cleared }
    }
    public func clearUploadedAudio(_ id: UUID) throws {
        lock.lock(); defer { lock.unlock() }
        guard let chunk = chunk(id), chunk.state == .uploaded else { throw StoreError.invalidState }
        let url = try fileURL(for: chunk)
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
        if let recovery = chunk.recoveryFileName {
            let raw = directory.appendingPathComponent(recovery)
            if FileManager.default.fileExists(atPath: raw.path) { try FileManager.default.removeItem(at: raw) }
        }
        try mutate(id) { $0.state = .cleared }
    }
    public var archivedChunkCount: Int { lock.lock(); defer { lock.unlock() }; return manifest.archivedCount }
    public func compactCleared() throws {
        lock.lock(); defer { lock.unlock() }
        let finished = manifest.chunks.filter { $0.state == .cleared }
        guard !finished.isEmpty else { return }
        // Archive only metadata with a verified receipt and already reclaimed local audio.
        // Append+sync BEFORE pruning active manifest; interruption may duplicate an archive
        // line, but can never drop a pending chunk or its provenance.
        let archive = directory.appendingPathComponent("receipts-archive.jsonl")
        if !FileManager.default.fileExists(atPath: archive.path) { FileManager.default.createFile(atPath: archive.path, contents: nil) }
        let handle = try FileHandle(forWritingTo: archive)
        defer { try? handle.close() }
        try handle.seekToEnd()
        for item in finished {
            var data = try JSONEncoder().encode(item); data.append(10); try handle.write(contentsOf: data)
        }
        try handle.synchronize()
        let ids = Set(finished.map(\.id))
        try transaction { value in value.chunks.removeAll { ids.contains($0.id) }; value.archivedCount += finished.count }
    }
    public func retryable(now: Date = Date(), uploading: Bool) -> [Chunk] {
        all().filter { chunk in
            guard chunk.isFinalized, ![.received, .uploaded, .cleared].contains(chunk.state) else { return false }
            guard let last = chunk.lastAttemptAt else { return true }
            // Explicit application receipts can be lost; retry idempotently after bounded backoff.
            let delay = min(900.0, 30.0 * pow(2, Double(min(chunk.attempts, 5))))
            return now.timeIntervalSince(last) >= delay
        }
    }
}
