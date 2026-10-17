import Foundation
import CryptoKit
import WatchMemoryCore

struct Fixture: Codable {
    let warning: String
    let configuration: PairingConfiguration
    let attempt: UploadAttempt
    let sealed_base64: String
    let expected_audio_base64: String
    let receipt_body_base64: String
    let mac_base64: String
    let status_code: Int
}

@main
struct WireV2Checks {
    static func fixture() throws -> Fixture {
        // Deliberately public deterministic bytes. NEVER use this fixture secret in production.
        let configuration = PairingConfiguration(endpoint: "http://127.0.0.1:18437",
            pairing_secret: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
        let id = UUID(uuidString: "00000000-0000-4000-8000-000000000001")!
        let session = UUID(uuidString: "00000000-0000-4000-8000-000000000002")!
        let audio = Data("PUBLIC SYNTHETIC AUDIO FIXTURE; not a real recording".utf8)
        var chunk = Chunk(id: id, sessionID: session, sequence: 9, capturedAt: Date(timeIntervalSince1970: 1792108800),
            fileName: id.uuidString.lowercased() + ".caf", state: .ready)
        chunk.duration = 1.25; chunk.sha256 = SealedTransport.digest(audio); chunk.byteCount = Int64(audio.count)
        let (attempt, cipher) = try SealedTransport.seal(metadata: chunk.uploadMetadata, audio: audio,
            configuration: configuration, attemptID: UUID(uuidString: "11111111-1111-4111-8111-111111111111")!, nonceBytes: Data(0..<12))
        let receipt = ServerReceipt(stored: true, sha256: chunk.sha256, source_id: chunk.sha256,
            chunk_id: id.uuidString.lowercased(), session_id: session.uuidString.lowercased(), sequence: 9)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let body = try encoder.encode(receipt)
        return Fixture(warning: "PUBLIC TEST SECRET/PLAINTEXT ONLY; DO NOT PAIR A PRODUCTION APP WITH THIS FILE",
            configuration: configuration, attempt: attempt, sealed_base64: cipher.base64EncodedString(),
            expected_audio_base64: audio.base64EncodedString(), receipt_body_base64: body.base64EncodedString(),
            mac_base64: SealedTransport.fixtureReceiptMAC(body: body, statusCode: 201, attempt: attempt, secret: configuration.pairing_secret), status_code: 201)
    }
    static func check(_ value: @autoclosure () throws -> Bool) throws { let result = try value(); precondition(result) }
    static func rejects(_ operation: () throws -> Void) throws {
        do { try operation() } catch { return }
        throw NSError(domain: "WireV2Checks.ExpectedFailure", code: 1)
    }
    static func verifyPythonVector(_ path: String) throws {
        let object = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as! [String: Any]
        guard (object["fixture_scope"] as? String)?.hasPrefix("Public deterministic") == true,
              let secret = object["pairing_secret"] as? String, let path = object["path"] as? String,
              let challenge = object["challenge"] as? String, let wireText = object["wire_base64"] as? String,
              let wire = Data(base64Encoded: wireText), let rawMetadata = object["metadata_utf8"] as? String,
              let audioText = object["audio_base64"] as? String, let audio = Data(base64Encoded: audioText),
              let rawReceipt = object["raw_receipt_utf8"] as? String, let status = object["http_status"] as? Int,
              let macText = object["receipt_mac_base64"] as? String, let mac = Data(base64Encoded: macText) else { throw TransportError.invalidEvent }
        func key(_ info: String) -> SymmetricKey {
            HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: Data(secret.utf8)), salt: Data("watch-memory-v2".utf8), info: Data(info.utf8), outputByteCount: 32)
        }
        let aad = try SealedTransport.aad(path: path, challenge: challenge)
        let plain = try AES.GCM.open(AES.GCM.SealedBox(combined: wire), using: key("upload-encryption"), authenticating: aad)
        guard plain.count >= 4 else { throw TransportError.invalidEvent }
        let count = plain.prefix(4).reduce(0) { ($0 << 8) | Int($1) }
        guard count > 0, count <= SealedTransport.maxMetadataBytes, count + 4 <= plain.count else { throw TransportError.invalidEvent }
        precondition(Data(plain[4..<(4 + count)]) == Data(rawMetadata.utf8))
        precondition(Data(plain[(4 + count)...]) == audio)
        var signed = Data("watch-memory-receipt-v2\n\(challenge)\n\(SealedTransport.digest(wire))\n\(status)\n".utf8)
        signed.append(Data(rawReceipt.utf8))
        precondition(HMAC<SHA256>.isValidAuthenticationCode(mac, authenticating: signed, using: key("receipt-authentication")))
        print("PASS independent receiver Python AESGCM ciphertext and raw receipt MAC verified by Swift CryptoKit (public crypto-only vector)")
    }
    static func sessionOwnershipChecks(_ f: Fixture) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = try UploadAttemptStore(directory: root.appendingPathComponent("attempts"))
        let cipher = Data(base64Encoded: f.sealed_base64)!
        try journal.prepare(f.attempt, ciphertext: cipher); try journal.bind(f.attempt.id, taskID: 1)
        let recordURL = journal.directory.appendingPathComponent(f.attempt.id.uuidString.lowercased() + ".json")
        var legacy = try JSONSerialization.jsonObject(with: Data(contentsOf: recordURL)) as! [String: Any]
        legacy.removeValue(forKey: "sessionIdentity") // installed old-version record
        try JSONSerialization.data(withJSONObject: legacy).write(to: recordURL, options: .atomic)
        let url = try f.configuration.validatedURL().appendingPathComponent(String(f.attempt.path.dropFirst()))
        _ = try journal.validateBoundTask(f.attempt.id, taskID: 1, originalURL: url, httpMethod: "PUT", challengeHeader: f.attempt.challenge, configuration: f.configuration, checkCiphertext: true)
        let oldScope = UploadSessionIdentity.audio(UUID()), newScope = UploadSessionIdentity.audio(UUID())
        try rejects { _ = try journal.validateBoundTask(f.attempt.id, taskID: 1, originalURL: url, httpMethod: "PUT", challengeHeader: f.attempt.challenge, configuration: f.configuration, checkCiphertext: true, session: oldScope) }
        let audio = Data(base64Encoded: f.expected_audio_base64)!
        let (fast, fastCipher) = try SealedTransport.seal(metadata: f.attempt.expectedMetadata, audio: audio, configuration: f.configuration)
        try journal.prepare(fast, ciphertext: fastCipher); try journal.bind(fast.id, taskID: 1, session: oldScope)
        _ = try journal.validateBoundTask(fast.id, taskID: 1, originalURL: url, httpMethod: "PUT", challengeHeader: fast.challenge, configuration: f.configuration, checkCiphertext: true, session: oldScope)
        for scope in [UploadSessionIdentity.background, newScope] {
            try rejects { _ = try journal.validateBoundTask(fast.id, taskID: 1, originalURL: url, httpMethod: "PUT", challengeHeader: fast.challenge, configuration: f.configuration, checkCiphertext: false, session: scope) }
        }
        let response = UploadResponseBuffers()
        let bgKey = UploadResponseKey(session: .background, taskID: 1), fastKey = UploadResponseKey(session: oldScope, taskID: 1)
        try check(response.append(Data("bg".utf8), for: bgKey)); try check(response.append(Data("fast".utf8), for: fastKey))
        try check(response.take(bgKey) == Data("bg".utf8)); try check(response.take(fastKey) == Data("fast".utf8))
        try check(!response.append(Data(repeating: 0, count: SealedTransport.maxResponseBytes + 1), for: bgKey))
        let custody = try ChunkStore(directory: root.appendingPathComponent("raw"))
        let chunk = try f.attempt.expectedMetadata.makeChunk()
        try audio.write(to: custody.fileURL(for: chunk)); try custody.insert(chunk); try custody.markAttempt(chunk.id, uploading: true)
        // A failed handoff binding cannot release the old valid owner or the raw audio.
        let (unbound, unboundCipher) = try SealedTransport.seal(metadata: f.attempt.expectedMetadata, audio: audio, configuration: f.configuration)
        try journal.prepare(unbound, ciphertext: unboundCipher)
        let invalidScope = try JSONDecoder().decode(UploadSessionIdentity.self, from: Data("{\"kind\":\"activeAudio\",\"scope\":\"invalid\"}".utf8))
        try rejects { try journal.bind(unbound.id, taskID: 2, session: invalidScope) }
        try check(try journal.load(unbound.id).state == "prepared")
        try check(try journal.load(fast.id).state == "scheduled")
        try check(FileManager.default.fileExists(atPath: custody.fileURL(for: chunk).path))
        // Cold start loses only old default-session ciphertext, never the existing background owner or raw audio.
        let lost = try journal.abandonOtherAudioScopes(current: newScope)
        try check(lost == [try f.attempt.expectedMetadata.makeChunk().id])
        try check(try journal.load(fast.id).state == "failed")
        try check(FileManager.default.fileExists(atPath: custody.fileURL(for: chunk).path))
        try check(try journal.load(f.attempt.id).state == "scheduled")
        try check(FileManager.default.fileExists(atPath: try journal.cipherURL(f.attempt).path))
        var owners = UploadOwnershipLedger(); owners.add(event: chunk.id, attempt: fast.id); owners.add(event: chunk.id, attempt: f.attempt.id)
        // A late failed fast callback sees a distinct durable background owner.
        if !owners.hasOtherOwner(event: chunk.id, excluding: fast.id) { try custody.markFailed(chunk.id, message: "late fast failure") }
        try check(custody.chunk(chunk.id)?.state == .uploading)
        try check(FileManager.default.fileExists(atPath: custody.fileURL(for: chunk).path))
        let body = Data(base64Encoded: f.receipt_body_base64)!
        let receipt = try SealedTransport.verifyReceipt(body, macBase64: f.mac_base64, statusCode: f.status_code, attempt: f.attempt, configuration: f.configuration)
        try custody.acknowledgeServer(chunk.id, receipt: receipt, statusCode: f.status_code)
        try custody.markFailed(chunk.id, message: "late other-session failure")
        try check(custody.chunk(chunk.id)?.state == .uploaded)
        try custody.clearUploadedAudio(chunk.id); try custody.markFailed(chunk.id, message: "late failure after collection")
        try check(custody.chunk(chunk.id)?.state == .cleared)
        try custody.acknowledgeServer(chunk.id, receipt: receipt, statusCode: f.status_code)
        try check(custody.chunk(chunk.id)?.state == .cleared)
        print("PASS legacy background / same task1 cross-session isolation / bounded response / cold default loss / handoff late failure and ACK collection guards")
    }
    static func recoveryFixtures(_ f: Fixture) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = try UploadAttemptStore(directory: root.appendingPathComponent("attempts"))
        let audio = Data(base64Encoded: f.expected_audio_base64)!
        let (freshBound, cipher) = try SealedTransport.seal(metadata: f.attempt.expectedMetadata, audio: audio, configuration: f.configuration)
        var historical = try JSONSerialization.jsonObject(with: JSONEncoder().encode(freshBound)) as! [String: Any]
        historical["createdAt"] = Date().addingTimeInterval(-1200).timeIntervalSinceReferenceDate
        let bound = try JSONDecoder().decode(UploadAttempt.self, from: JSONSerialization.data(withJSONObject: historical))
        try journal.prepare(bound, ciphertext: cipher)
        let expectedURL = try f.configuration.validatedURL().appendingPathComponent(String(bound.path.dropFirst()))
        func verify(_ source: UploadAttemptStore, taskID: Int = 42, url: URL? = nil, challenge: String? = nil) throws -> UploadAttempt {
            try source.validateBoundTask(bound.id, taskID: taskID, originalURL: url ?? expectedURL,
                httpMethod: "PUT", challengeHeader: challenge ?? bound.challenge, configuration: f.configuration, checkCiphertext: true)
        }
        try rejects { _ = try verify(journal) } // created/suspended but bind never committed: cancel, new attempt
        try journal.bind(bound.id, taskID: 42)
        let relaunched = try UploadAttemptStore(directory: journal.directory)
        let restored = try verify(relaunched)
        precondition(restored.systemTaskID == 42 && restored.state == "scheduled")
        try rejects { _ = try verify(relaunched, taskID: 43) }
        try rejects { _ = try verify(relaunched, url: URL(string: "http://127.0.0.1:18438" + bound.path)!) }
        try rejects { _ = try verify(relaunched, challenge: UUID().uuidString.lowercased()) }
        let movedReceiver = PairingConfiguration(endpoint: "https://receiver.example.invalid", pairing_secret: f.configuration.pairing_secret)
        try rejects { _ = try relaunched.validateBoundTask(bound.id, taskID: 42, originalURL: expectedURL,
            httpMethod: "PUT", challengeHeader: bound.challenge, configuration: movedReceiver, checkCiphertext: false) }
        let wrongKey = PairingConfiguration(endpoint: f.configuration.endpoint, pairing_secret: "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI")
        try rejects { _ = try relaunched.validateBoundTask(bound.id, taskID: 42, originalURL: expectedURL,
            httpMethod: "PUT", challengeHeader: bound.challenge, configuration: wrongKey, checkCiphertext: true) }
        var corrupted = cipher; corrupted[corrupted.startIndex] ^= 1
        try corrupted.write(to: journal.cipherURL(bound))
        try rejects { _ = try verify(relaunched) }
        try cipher.write(to: journal.cipherURL(bound))
        // Simulate prepare() dying after ciphertext fsync, before its JSON fsync.
        let oldID = UUID(); let oldSealed = journal.directory.appendingPathComponent(oldID.uuidString.lowercased() + ".sealed")
        try cipher.write(to: oldSealed)
        let oldDate = Date().addingTimeInterval(-1200)
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: oldSealed.path)
        let invalidID = UUID(); let invalidSealed = journal.directory.appendingPathComponent(invalidID.uuidString.lowercased() + ".sealed")
        try cipher.write(to: invalidSealed)
        try Data("{invalid}".utf8).write(to: journal.directory.appendingPathComponent(invalidID.uuidString.lowercased() + ".json"))
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: invalidSealed.path)
        let freshID = UUID(); let freshSealed = journal.directory.appendingPathComponent(freshID.uuidString.lowercased() + ".sealed")
        try cipher.write(to: freshSealed)
        let original = root.appendingPathComponent("original.caf"); try audio.write(to: original)
        let unexpectedOriginal = journal.directory.appendingPathComponent("keep.caf"); try audio.write(to: unexpectedOriginal)
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: journal.cipherURL(bound).path)
        try relaunched.reconcileAndCompact(active: [bound.id])
        precondition(!FileManager.default.fileExists(atPath: oldSealed.path))
        precondition(!FileManager.default.fileExists(atPath: invalidSealed.path))
        precondition(FileManager.default.fileExists(atPath: freshSealed.path))
        let protectedCipherPath = try journal.cipherURL(bound).path
        precondition(FileManager.default.fileExists(atPath: protectedCipherPath))
        try check(try Data(contentsOf: original) == audio)
        try check(try Data(contentsOf: unexpectedOriginal) == audio)
        print("PASS interrupted bind-before-resume recovery identity / wrong task-key-URL-challenge-cipher rejection / aged orphan sealed cleanup / active and recent cipher preservation / original audio untouched")
    }
    static func main() throws {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.first == "verify-python-vector", args.count == 2 { try verifyPythonVector(args[1]); return }
        if args.first == "verify-fixture", args.count == 2 {
            let input = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: args[1])))
            let body = Data(base64Encoded: input.receipt_body_base64)!
            _ = try SealedTransport.verifyReceipt(body, macBase64: input.mac_base64, statusCode: input.status_code,
                attempt: input.attempt, configuration: input.configuration)
            print("PASS authenticated Python-to-Swift receipt interoperability")
            return
        }
        let f = try fixture()
        let body = Data(base64Encoded: f.receipt_body_base64)!
        let cipher = Data(base64Encoded: f.sealed_base64)!
        _ = try SealedTransport.verifyReceipt(body, macBase64: f.mac_base64, statusCode: 201, attempt: f.attempt, configuration: f.configuration)
        try rejects { _ = try SealedTransport.verifyReceipt(body, macBase64: nil, statusCode: 201, attempt: f.attempt, configuration: f.configuration) }
        var changed = body; changed[changed.startIndex] ^= 1
        try rejects { _ = try SealedTransport.verifyReceipt(changed, macBase64: f.mac_base64, statusCode: 201, attempt: f.attempt, configuration: f.configuration) }
        try rejects { _ = try SealedTransport.verifyReceipt(body, macBase64: f.mac_base64, statusCode: 200, attempt: f.attempt, configuration: f.configuration) }
        let bad = PairingConfiguration(endpoint: f.configuration.endpoint, pairing_secret: "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI")
        try rejects { _ = try SealedTransport.verifyReceipt(body, macBase64: f.mac_base64, statusCode: 201, attempt: f.attempt, configuration: bad) }
        let audio = Data(base64Encoded: f.expected_audio_base64)!
        let (fresh, freshCipher) = try SealedTransport.seal(metadata: f.attempt.expectedMetadata, audio: audio, configuration: f.configuration)
        precondition(fresh.challenge != f.attempt.challenge && freshCipher != cipher)
        try rejects { _ = try SealedTransport.verifyReceipt(body, macBase64: f.mac_base64, statusCode: 201, attempt: fresh, configuration: f.configuration) }
        // A correctly MAC'd but wrong-event/schema JSON must still fail provenance validation.
        var object = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        object["chunk_id"] = UUID().uuidString.lowercased()
        let wrongEvent = try JSONSerialization.data(withJSONObject: object)
        let wrongMAC = SealedTransport.fixtureReceiptMAC(body: wrongEvent, statusCode: 201, attempt: f.attempt, secret: f.configuration.pairing_secret)
        try rejects { _ = try SealedTransport.verifyReceipt(wrongEvent, macBase64: wrongMAC, statusCode: 201, attempt: f.attempt, configuration: f.configuration) }
        object = try JSONSerialization.jsonObject(with: body) as! [String: Any]; object["schema_version"] = 1
        let oldSchema = try JSONSerialization.data(withJSONObject: object)
        let oldMAC = SealedTransport.fixtureReceiptMAC(body: oldSchema, statusCode: 201, attempt: f.attempt, secret: f.configuration.pairing_secret)
        try rejects { _ = try SealedTransport.verifyReceipt(oldSchema, macBase64: oldMAC, statusCode: 201, attempt: f.attempt, configuration: f.configuration) }
        try rejects { _ = try SealedTransport.aad(path: f.attempt.path, challenge: "not-a-uuid") }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = try UploadAttemptStore(directory: root)
        try journal.prepare(f.attempt, ciphertext: cipher)
        let reloaded = try UploadAttemptStore(directory: root)
        try check(try reloaded.load(f.attempt.id) == f.attempt)
        try reloaded.bind(f.attempt.id, taskID: 17)
        try check(try reloaded.load(f.attempt.id).systemTaskID == 17)
        try reloaded.finish(f.attempt.id, succeeded: false)
        let finishedPath = try reloaded.cipherURL(f.attempt).path
        precondition(!FileManager.default.fileExists(atPath: finishedPath))
        try rejects { _ = try PairingConfiguration(version: 1, endpoint: f.configuration.endpoint,
            pairing_secret: f.configuration.pairing_secret, protocolName: "watch-memory-chunks-v1").validatedURL() }
        try recoveryFixtures(f)
        try sessionOwnershipChecks(f)
        print("PASS AES-GCM random nonce / challenge-bound constant-time MAC / altered body-status-key-oldACK / wrong event-schema / durable attempt journal / production rejects v1")
        if args.first == "emit-fixture", args.count == 2 {
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(f).write(to: URL(fileURLWithPath: args[1]), options: .atomic)
            print("PASS public synthetic deterministic fixture written")
        } else if !args.isEmpty { throw TransportError.invalidEvent }
    }
}
