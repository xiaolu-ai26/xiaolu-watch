import XCTest
@testable import WatchMemoryCore

final class ChunkStoreTests: XCTestCase {
    private var directory: URL!
    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: directory) }
    private func recording(_ store: ChunkStore) throws -> Chunk {
        let id = UUID()
        let chunk = Chunk(id: id, sessionID: UUID(), sequence: 0, capturedAt: Date(timeIntervalSince1970: 1000), fileName: id.uuidString.lowercased() + ".caf")
        try store.insert(chunk); try Data("original audio".utf8).write(to: store.fileURL(for: chunk))
        return chunk
    }
    private func ready(_ store: ChunkStore) throws -> Chunk {
        let chunk = try recording(store)
        try store.finalize(chunk.id, duration: 60, sha256: String(repeating: "a", count: 64), byteCount: 14)
        return store.chunk(chunk.id)!
    }
    func testAtomicManifestAndRecoverKeepsInterruptedOriginal() throws {
        let store = try ChunkStore(directory: directory)
        let chunk = try recording(store)
        // A partially written temporary file must not replace the valid manifest.
        try Data("{broken".utf8).write(to: directory.appendingPathComponent("manifest.json.tmp"))
        let relaunched = try ChunkStore(directory: directory)
        let recovery = try relaunched.recoverInterrupted()
        XCTAssertEqual(recovery.map(\.id), [chunk.id])
        XCTAssertEqual(relaunched.chunk(chunk.id)?.state, .needsRecovery)
        XCTAssertEqual(try Data(contentsOf: relaunched.fileURL(for: chunk)), Data("original audio".utf8))
        XCTAssertThrowsError(try relaunched.clearReceivedAudio(chunk.id))
        try relaunched.finalize(chunk.id, duration: 0.01, sha256: String(repeating: "a", count: 64), byteCount: 14)
        XCTAssertEqual(try ChunkStore(directory: directory).chunk(chunk.id)?.state, .ready)
    }
    func testOnlyVerifiedDurablePhoneReceiptAllowsCleanup() throws {
        let store = try ChunkStore(directory: directory); let chunk = try ready(store)
        try store.markAttempt(chunk.id, uploading: false)
        try store.transferFinished(chunk.id, success: true)
        XCTAssertEqual(store.chunk(chunk.id)?.state, .awaitingReceipt)
        XCTAssertThrowsError(try store.clearReceivedAudio(chunk.id))
        XCTAssertThrowsError(try store.acknowledgePhone(chunk.id, sha256: String(repeating: "b", count: 64), byteCount: 14))
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.fileURL(for: chunk).path))
        let ackDate = Date(timeIntervalSince1970: 2000)
        try store.acknowledgePhone(chunk.id, sha256: chunk.sha256, byteCount: 14, now: ackDate)
        try store.acknowledgePhone(chunk.id, sha256: chunk.sha256, byteCount: 14, now: Date())
        // Late WC completion must not regress an already persisted application receipt.
        try store.transferFinished(chunk.id, success: false)
        XCTAssertEqual(store.chunk(chunk.id)?.state, .received)
        XCTAssertEqual(store.chunk(chunk.id)?.acknowledgedAt, ackDate)
        try store.clearReceivedAudio(chunk.id)
        XCTAssertEqual(try ChunkStore(directory: directory).chunk(chunk.id)?.state, .cleared)
        XCTAssertFalse(FileManager.default.fileExists(atPath: try store.fileURL(for: chunk).path))
    }
    func testHTTPErrorOrWrongHashNeverCompletesOrDeletesAudio() throws {
        let store = try ChunkStore(directory: directory); let chunk = try ready(store)
        try store.markAttempt(chunk.id, uploading: true)
        let receipt = ServerReceipt(stored: true, sha256: chunk.sha256, source_id: chunk.sha256, chunk_id: chunk.id.uuidString, session_id: chunk.sessionID.uuidString, sequence: chunk.sequence)
        XCTAssertThrowsError(try store.acknowledgeServer(chunk.id, receipt: receipt, statusCode: 500))
        let wrong = ServerReceipt(stored: true, sha256: String(repeating: "b", count: 64), source_id: chunk.sha256, chunk_id: chunk.id.uuidString, session_id: chunk.sessionID.uuidString, sequence: chunk.sequence)
        XCTAssertThrowsError(try store.acknowledgeServer(chunk.id, receipt: wrong, statusCode: 201))
        XCTAssertEqual(store.chunk(chunk.id)?.state, .uploading)
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.fileURL(for: chunk).path))
        try store.acknowledgeServer(chunk.id, receipt: receipt, statusCode: 201)
        try store.acknowledgeServer(chunk.id, receipt: receipt, statusCode: 200)
        XCTAssertEqual(try ChunkStore(directory: directory).chunk(chunk.id)?.state, .uploaded)
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.fileURL(for: chunk).path))
    }
    func testDuplicateIdentityAndSequenceCannotOverwriteEvidence() throws {
        let store = try ChunkStore(directory: directory); let chunk = try ready(store)
        try store.insert(chunk)
        var conflict = chunk; conflict.sha256 = String(repeating: "b", count: 64)
        XCTAssertThrowsError(try store.insert(conflict))
        var other = Chunk(sessionID: chunk.sessionID, sequence: chunk.sequence, capturedAt: chunk.capturedAt, fileName: UUID().uuidString.lowercased() + ".caf", state: .ready)
        other.sha256 = chunk.sha256; other.byteCount = chunk.byteCount; other.duration = chunk.duration
        XCTAssertThrowsError(try store.insert(other))
        XCTAssertEqual(store.all().count, 1)
    }
    func testCorruptManifestFailsClosedAndIsNotOverwritten() throws {
        let path = directory.appendingPathComponent("manifest.json")
        let original = Data("{not valid}".utf8); try original.write(to: path)
        XCTAssertThrowsError(try ChunkStore(directory: directory))
        XCTAssertEqual(try Data(contentsOf: path), original)
    }
    func testMetadataRoundtripAndPrivateHTTPPolicy() throws {
        let store = try ChunkStore(directory: directory); let chunk = try ready(store)
        let info = try JSONDecoder().decode(UploadMetadata.self, from: JSONEncoder().encode(chunk.uploadMetadata))
        XCTAssertEqual(try info.makeChunk().sha256, chunk.sha256)
        XCTAssertEqual(try info.makeChunk().capturedAt, chunk.capturedAt)
        // PUBLIC TEST FIXTURE SECRET (bytes 0...31). Never use it for a real pairing.
        let token = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        XCTAssertNoThrow(try PairingConfiguration(endpoint: "http://100.100.1.2:8915", pairing_secret: token).validatedURL())
        XCTAssertNoThrow(try PairingConfiguration(endpoint: "https://example.com", pairing_secret: token).validatedURL())
        XCTAssertThrowsError(try PairingConfiguration(endpoint: "http://example.com", pairing_secret: token).validatedURL())
        XCTAssertThrowsError(try PairingConfiguration(endpoint: "http://bad.192.168.1.2", pairing_secret: token).validatedURL())
        XCTAssertThrowsError(try PairingConfiguration(endpoint: "https://example.com", pairing_secret: token + "\r\nInjected:yes").validatedURL())
    }
}
