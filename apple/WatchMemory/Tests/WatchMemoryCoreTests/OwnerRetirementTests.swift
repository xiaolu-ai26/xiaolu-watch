import XCTest
@testable import WatchMemoryCore

final class OwnerRetirementTests: XCTestCase {
    func testOnlyPurposeBoundRetirementStopsRetryAndPreservesLocalBytes() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try ChunkStore(directory: root)
        // PUBLIC deterministic test fixture, not captured speech or a real pairing secret.
        let audio = Data("PUBLIC-RETIREMENT-FIXTURE".utf8)
        var chunk = Chunk(sessionID: UUID(), sequence: 0, capturedAt: Date(timeIntervalSince1970: 1700000000), fileName: "temporary.caf", state: .ready)
        chunk.fileName = chunk.id.uuidString.lowercased() + ".caf"
        chunk.duration = 2; chunk.byteCount = Int64(audio.count); chunk.sha256 = SealedTransport.digest(audio)
        try audio.write(to: store.fileURL(for: chunk)); try store.insert(chunk)
        var other = Chunk(sessionID: chunk.sessionID, sequence: 1, capturedAt: chunk.capturedAt.addingTimeInterval(2), fileName: "temporary.caf", state: .ready)
        other.fileName = other.id.uuidString.lowercased() + ".caf"; other.duration = 2
        other.byteCount = Int64(audio.count); other.sha256 = chunk.sha256
        try audio.write(to: store.fileURL(for: other)); try store.insert(other)
        let config = PairingConfiguration(endpoint: "https://example.invalid", pairing_secret: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
        let (attempt, _) = try SealedTransport.seal(metadata: chunk.uploadMetadata, audio: audio, configuration: config)
        var json: [String: Any] = ["schema_version": 2, "protocol": SealedTransport.protocolName, "purpose": "owner_deletion", "retired": true,
            "source_id": chunk.sha256, "sha256": chunk.sha256, "chunk_id": chunk.id.uuidString.lowercased(),
            "session_id": chunk.sessionID.uuidString.lowercased(), "sequence": 0, "deletion_id": UUID().uuidString.lowercased()]
        func wire() throws -> Data { try JSONSerialization.data(withJSONObject: json) }
        let body = try wire(); let mac = SealedTransport.fixtureRetirementMAC(body: body, attempt: attempt, secret: config.pairing_secret)
        XCTAssertThrowsError(try SealedTransport.verifyRetirement(body, macBase64: nil, statusCode: 410, attempt: attempt, configuration: config))
        XCTAssertThrowsError(try SealedTransport.verifyRetirement(body, macBase64: mac, statusCode: 200, attempt: attempt, configuration: config))
        let storedMAC = SealedTransport.fixtureReceiptMAC(body: body, statusCode: 410, attempt: attempt, secret: config.pairing_secret)
        XCTAssertThrowsError(try SealedTransport.verifyRetirement(body, macBase64: storedMAC, statusCode: 410, attempt: attempt, configuration: config))
        let (differentAttempt, _) = try SealedTransport.seal(metadata: chunk.uploadMetadata, audio: audio, configuration: config)
        XCTAssertThrowsError(try SealedTransport.verifyRetirement(body, macBase64: mac, statusCode: 410, attempt: differentAttempt, configuration: config))
        json["purpose"] = "stored"
        let wrongBody = try wire(); let wrongMAC = SealedTransport.fixtureRetirementMAC(body: wrongBody, attempt: attempt, secret: config.pairing_secret)
        XCTAssertThrowsError(try SealedTransport.verifyRetirement(wrongBody, macBase64: wrongMAC, statusCode: 410, attempt: attempt, configuration: config))
        XCTAssertEqual(store.chunk(chunk.id)?.state, .ready)
        XCTAssertEqual(try Data(contentsOf: store.fileURL(for: chunk)), audio)
        let receipt = try SealedTransport.verifyRetirement(body, macBase64: mac, statusCode: 410, attempt: attempt, configuration: config)
        try store.retireOwnerDeleted(chunk.id, receipt: receipt, signedBody: body, mac: mac)
        XCTAssertFalse(store.retryable(uploading: true).contains(where: { $0.id == chunk.id }))
        XCTAssertTrue(store.retryable(uploading: true).contains(where: { $0.id == other.id }))
        XCTAssertEqual(try Data(contentsOf: store.fileURL(for: other)), audio)
        XCTAssertEqual(try ChunkStore(directory: root).chunk(chunk.id)?.state, .cleared)
        XCTAssertEqual(try Data(contentsOf: root.appendingPathComponent("owner-deletion-recovery/" + chunk.fileName)), audio)
        try store.markFailed(chunk.id, message: "late transport failure")
        try store.acknowledgePhone(chunk.id, sha256: chunk.sha256, byteCount: chunk.byteCount)
        XCTAssertEqual(store.chunk(chunk.id)?.state, .cleared)
    }
}
