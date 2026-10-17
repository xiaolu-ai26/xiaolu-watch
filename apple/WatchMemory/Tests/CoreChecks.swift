import Foundation
import CryptoKit
import WatchMemoryCore

/// SDK-independent regression checks. XCTest remains available in the Xcode toolchain.
@main
struct CoreChecks {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try ChunkStore(directory: root)
        let id = UUID(); let session = UUID(); let hash = String(repeating: "a", count: 64)
        let original = Data("original audio".utf8)
        let chunk = Chunk(id: id, sessionID: session, sequence: 0, capturedAt: Date(timeIntervalSince1970: 1000), fileName: id.uuidString.lowercased() + ".caf")
        try store.insert(chunk); let url = try store.fileURL(for: chunk); try original.write(to: url)
        try Data("{interrupted}".utf8).write(to: root.appendingPathComponent("manifest.json.tmp"))
        let recovered = try ChunkStore(directory: root)
        try check(try recovered.recoverInterrupted().map(\.id) == [id])
        try check(try Data(contentsOf: url) == original)
        try rejects { try recovered.clearReceivedAudio(id) }
        let encoded = id.uuidString.lowercased() + ".m4a"
        try Data("encoded copy".utf8).write(to: root.appendingPathComponent(encoded))
        try recovered.finalize(id, duration: 60, sha256: hash, byteCount: 12, encodedFileName: encoded)
        try recovered.markAttempt(id, uploading: false)
        try recovered.transferFinished(id, success: true)
        try rejects { try recovered.clearReceivedAudio(id) }
        try rejects { try recovered.acknowledgePhone(id, sha256: String(repeating: "b", count: 64), byteCount: 12) }
        precondition(FileManager.default.fileExists(atPath: url.path))
        let ackDate = Date(timeIntervalSince1970: 2000)
        try recovered.acknowledgePhone(id, sha256: hash, byteCount: 12, now: ackDate)
        try recovered.acknowledgePhone(id, sha256: hash, byteCount: 12)
        try recovered.transferFinished(id, success: false)
        precondition(recovered.chunk(id)?.state == .received)
        precondition(recovered.chunk(id)?.acknowledgedAt == ackDate)
        try recovered.clearReceivedAudio(id)
        precondition(!FileManager.default.fileExists(atPath: url.path))
        precondition(!FileManager.default.fileExists(atPath: root.appendingPathComponent(encoded).path))
        try recovered.compactCleared()
        precondition(recovered.all().isEmpty && recovered.archivedChunkCount == 1)
        let archiveText = try String(contentsOf: root.appendingPathComponent("receipts-archive.jsonl"))
        precondition(archiveText.contains(id.uuidString))
        print("PASS atomic interrupted-manifest recovery / only verified ACK clears original+encoded / late transport callback / compact provenance")

        let phoneRoot = root.appendingPathComponent("phone")
        let phone = try ChunkStore(directory: phoneRoot)
        var incoming = Chunk(id: id, sessionID: session, sequence: 0, capturedAt: chunk.capturedAt, fileName: encoded, state: .ready)
        incoming.duration = 60; incoming.sha256 = hash; incoming.byteCount = 12
        try phone.insert(incoming); let phoneFile = try phone.fileURL(for: incoming); try Data("encoded copy".utf8).write(to: phoneFile)
        try phone.markAttempt(id, uploading: true)
        let receipt = ServerReceipt(stored: true, sha256: hash, source_id: hash, chunk_id: id.uuidString, session_id: session.uuidString, sequence: 0)
        try rejects { try phone.acknowledgeServer(id, receipt: receipt, statusCode: 500) }
        let otherEvent = ServerReceipt(stored: true, sha256: hash, source_id: hash, chunk_id: UUID().uuidString, session_id: session.uuidString, sequence: 1)
        try rejects { try phone.acknowledgeServer(id, receipt: otherEvent, statusCode: 201) }
        precondition(phone.chunk(id)?.state == .uploading && FileManager.default.fileExists(atPath: phoneFile.path))
        var conflict = Chunk(id: id, sessionID: session, sequence: 0, capturedAt: Date(timeIntervalSince1970: 1001), fileName: encoded, state: .ready)
        conflict.duration = incoming.duration; conflict.sha256 = incoming.sha256; conflict.byteCount = incoming.byteCount
        try rejects { try phone.insert(conflict) }
        conflict = incoming; conflict.duration = 50
        try rejects { try phone.insert(conflict) }
        try phone.acknowledgeServer(id, receipt: receipt, statusCode: 201)
        try phone.acknowledgeServer(id, receipt: receipt, statusCode: 200)
        precondition(phone.chunk(id)?.state == .uploaded)
        precondition(FileManager.default.fileExists(atPath: phoneFile.path))
        let info = try JSONDecoder().decode(UploadMetadata.self, from: JSONEncoder().encode(incoming.uploadMetadata))
        try check(try info.makeChunk().fileName == encoded)
        print("PASS HTTP non2xx / wrong-event receipt / duplicate ACK / immutable metadata conflict / AAC metadata roundtrip")

        let directRoot = root.appendingPathComponent("watch-direct")
        var direct = try ChunkStore(directory: directRoot)
        let directID = UUID(); let rawName = directID.uuidString.lowercased() + ".caf"
        let encodedName = directID.uuidString.lowercased() + ".m4a"
        let payload = Data("real retained encoded fixture".utf8)
        let digest = SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        let directChunk = Chunk(id: directID, sessionID: session, sequence: 1, capturedAt: chunk.capturedAt, fileName: rawName)
        try direct.insert(directChunk)
        try original.write(to: directRoot.appendingPathComponent(rawName))
        try payload.write(to: directRoot.appendingPathComponent(encodedName))
        try direct.finalize(directID, duration: 35.5, sha256: digest, byteCount: Int64(payload.count), encodedFileName: encodedName)
        try direct.acknowledgePhone(directID, sha256: digest, byteCount: Int64(payload.count), now: ackDate)
        // Reopen the real version-1 layout produced by the already installed build.
        let manifestPath = directRoot.appendingPathComponent("manifest.json")
        var legacyManifest = try JSONSerialization.jsonObject(with: Data(contentsOf: manifestPath)) as! [String: Any]
        var legacyChunks = legacyManifest["chunks"] as! [[String: Any]]
        legacyChunks[0].removeValue(forKey: "phoneAcknowledgedAt"); legacyManifest["chunks"] = legacyChunks
        try JSONSerialization.data(withJSONObject: legacyManifest).write(to: manifestPath, options: .atomic)
        direct = try ChunkStore(directory: directRoot)
        try Data("tampered".utf8).write(to: directRoot.appendingPathComponent(encodedName))
        try rejects { try direct.requeuePhoneAcknowledgedForDirectUpload(directID) }
        precondition(direct.chunk(directID)?.state == .received)
        precondition(FileManager.default.fileExists(atPath: directRoot.appendingPathComponent(rawName).path))
        try payload.write(to: directRoot.appendingPathComponent(encodedName))
        try direct.requeuePhoneAcknowledgedForDirectUpload(directID)
        precondition(direct.chunk(directID)?.state == .ready)
        precondition(direct.chunk(directID)?.phoneAcknowledgedAt == ackDate && direct.chunk(directID)?.acknowledgedAt == nil)
        try direct.markAttempt(directID, uploading: true)
        try direct.acknowledgePhone(directID, sha256: digest, byteCount: Int64(payload.count))
        precondition(direct.chunk(directID)?.state == .uploading)
        let directReceipt = ServerReceipt(stored: true, sha256: digest, source_id: digest, chunk_id: directID.uuidString, session_id: session.uuidString, sequence: 1)
        try rejects { try direct.acknowledgeServer(directID, receipt: directReceipt, statusCode: 503) }
        try rejects { try direct.clearUploadedAudio(directID) }
        let serverDate = Date(timeIntervalSince1970: 3000)
        try direct.acknowledgeServer(directID, receipt: directReceipt, statusCode: 200, now: serverDate)
        try direct.acknowledgePhone(directID, sha256: digest, byteCount: Int64(payload.count))
        precondition(direct.chunk(directID)?.state == .uploaded && direct.chunk(directID)?.acknowledgedAt == serverDate)
        try direct.clearUploadedAudio(directID)
        precondition(!FileManager.default.fileExists(atPath: directRoot.appendingPathComponent(rawName).path))
        precondition(!FileManager.default.fileExists(atPath: directRoot.appendingPathComponent(encodedName).path))
        try direct.acknowledgeServer(directID, receipt: directReceipt, statusCode: 200)
        precondition(direct.chunk(directID)?.state == .cleared)
        print("PASS legacy Phone custody SHA-verified migration / tamper retains originals / late Phone ACK cannot regress direct upload / only server ACK clears encoded+CAF")

        // PUBLIC TEST FIXTURE SECRET (bytes 0...31). Never use it for a real pairing.
        let token = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        _ = try PairingConfiguration(endpoint: "http://100.100.1.2:18437", pairing_secret: token).validatedURL()
        try rejects { _ = try PairingConfiguration(endpoint: "http://example.com", pairing_secret: token).validatedURL() }
        try rejects { _ = try PairingConfiguration(endpoint: "http://evil.192.168.1.2", pairing_secret: token).validatedURL() }
        try rejects { _ = try PairingConfiguration(endpoint: "https://example.com", pairing_secret: token + "\r\nheader").validatedURL() }
        let corrupt = root.appendingPathComponent("bad"); try FileManager.default.createDirectory(at: corrupt, withIntermediateDirectories: true)
        let bad = Data("{bad}".utf8); try bad.write(to: corrupt.appendingPathComponent("manifest.json"))
        try rejects { _ = try ChunkStore(directory: corrupt) }
        try check(try Data(contentsOf: corrupt.appendingPathComponent("manifest.json")) == bad)
        let claimBase = try PairingClaimPolicy.baseURL("HTTPS://EXAMPLE.COM:443/")
        try check(claimBase.host == "example.com")
        try check(PairingClaimPolicy.code("ABCD-EFGH-IJKL-MN23") == "ABCDEFGHIJKLMN23")
        for bad in ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1", "https://example.com#x"] { try rejects { _ = try PairingClaimPolicy.baseURL(bad) } }
        for bad in ["SHORT", "0123456789012345", "ABCDEFGHIJKLMNOPQ"] { try rejects { _ = try PairingClaimPolicy.code(bad) } }
        let claimConfig = PairingConfiguration(endpoint: "https://example.com", pairing_secret: Data(repeating: 7, count: 32).base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: ""))
        let claimData = try JSONEncoder().encode(claimConfig)
        try check(PairingClaimPolicy.validateResponse(claimData, requestedBase: claimBase) == claimConfig)
        try rejects { _ = try PairingClaimPolicy.validateResponse(claimData, requestedBase: URL(string: "https://other.example")!) }
        try rejects { _ = try PairingClaimPolicy.validateResponse(claimData, requestedBase: URL(string: "https://example.com:8443")!) }
        try rejects { _ = try PairingClaimPolicy.validateResponse(Data(repeating: 0, count: 8193), requestedBase: claimBase) }
        let importFile = root.appendingPathComponent("onboarding.json")
        let importData = try JSONSerialization.data(withJSONObject: ["endpoint": "https://example.com", "code": "ABCD-EFGH-IJKL-MN23"])
        try importData.write(to: importFile)
        try check(ReceiverBootstrapImport.readRegularFile(importFile)?.code == "ABCDEFGHIJKLMN23")
        let link = root.appendingPathComponent("bootstrap-link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: importFile)
        try rejects { _ = try ReceiverBootstrapImport.readRegularFile(link) }
        try rejects { _ = try ReceiverBootstrapImport.readRegularFile(root) }
        try Data(repeating: 0, count: 8193).write(to: importFile)
        try rejects { _ = try ReceiverBootstrapImport.readRegularFile(importFile) }
        try JSONSerialization.data(withJSONObject: ["endpoint": "http://example.com", "code": "ABCDEFGHIJKLMNOP"]).write(to: importFile)
        try rejects { _ = try ReceiverBootstrapImport.readRegularFile(importFile) }
        try JSONSerialization.data(withJSONObject: ["endpoint": "https://example.com", "code": "ABCDEFGHIJKLMNOP", "pairing_secret": "forbidden"]).write(to: importFile)
        try rejects { _ = try ReceiverBootstrapImport.readRegularFile(importFile) }
        try FileManager.default.removeItem(at: importFile)
        try check(ReceiverBootstrapImport.readRegularFile(importFile) == nil)
        print("PASS bootstrap file rejects symlink, directory, oversize, HTTP and secret fields")
        let runtimeID = UUID()
        try check(AudioUploadEligibility.session(engineRunning: true, wanted: true, paused: false, id: runtimeID) == runtimeID)
        try check(AudioUploadEligibility.session(engineRunning: false, wanted: true, paused: false, id: runtimeID) == nil)
        try check(AudioUploadEligibility.session(engineRunning: true, wanted: false, paused: false, id: runtimeID) == nil)
        try check(AudioUploadEligibility.session(engineRunning: true, wanted: true, paused: true, id: runtimeID) == nil)
        var fastChunk = Chunk(sessionID: runtimeID, sequence: 0, capturedAt: Date(), fileName: UUID().uuidString.lowercased() + ".caf", state: .ready)
        fastChunk.duration = 60; fastChunk.byteCount = 100; fastChunk.sha256 = String(repeating: "a", count: 64)
        try check(AudioUploadEligibility.canStartFast(chunk: fastChunk, runtimeSession: runtimeID, fastCount: 0, backgroundCount: 2))
        try check(!AudioUploadEligibility.canStartFast(chunk: fastChunk, runtimeSession: UUID(), fastCount: 0, backgroundCount: 0))
        try check(!AudioUploadEligibility.canStartFast(chunk: fastChunk, runtimeSession: runtimeID, fastCount: 1, backgroundCount: 0))
        try check(!AudioUploadEligibility.canStartFast(chunk: fastChunk, runtimeSession: runtimeID, fastCount: 0, backgroundCount: 3))
        try check(!AudioUploadEligibility.canStartFast(chunk: fastChunk, runtimeSession: runtimeID, fastCount: 0, backgroundCount: 0, minimumSequence: 1))
        print("PASS real audio runtime gate / new session only / one fast slot / handoff background cap")
        var recordingIntent = RecordingIntent()
        try check(!recordingIntent.wanted && !recordingIntent.canRestartAutomatically)
        recordingIntent.start(); recordingIntent.activationSucceeded(); recordingIntent.interruptionBegan()
        try check(recordingIntent.wanted && recordingIntent.interruptionActive && !recordingIntent.canRestartAutomatically)
        try check(!recordingIntent.mayActivate(manual: false))
        // Missing ended event still has an explicit user-initiated API probe.
        recordingIntent.requestManualResume(); try check(recordingIntent.mayActivate(manual: true))
        recordingIntent.activationFailed(); try check(recordingIntent.wanted && recordingIntent.manualResumeRequired)
        try check(!recordingIntent.canRestartAutomatically)
        recordingIntent.requestManualResume(); recordingIntent.activationSucceeded()
        try check(recordingIntent.canRestartAutomatically && !recordingIntent.interruptionActive)
        try check(!recordingIntent.interruptionEnded(shouldResume: true, engineRunning: true))
        recordingIntent.interruptionBegan()
        try check(!recordingIntent.interruptionEnded(shouldResume: false, engineRunning: false))
        try check(recordingIntent.manualResumeRequired && !recordingIntent.canRestartAutomatically)
        recordingIntent.requestManualResume(); recordingIntent.activationSucceeded(); recordingIntent.pause(); recordingIntent.interruptionBegan()
        try check(!recordingIntent.interruptionEnded(shouldResume: true, engineRunning: false))
        try check(recordingIntent.userPaused && !recordingIntent.canRestartAutomatically)
        recordingIntent.stop(); try check(!recordingIntent.interruptionEnded(shouldResume: true, engineRunning: false))
        try check(!recordingIntent.wanted)
        var auto = RecordingIntent(); auto.start(); auto.interruptionBegan()
        try check(auto.interruptionEnded(shouldResume: true, engineRunning: false))
        auto.activationSucceeded(); try check(auto.canRestartAutomatically)
        let diagnosticRoot = root.appendingPathComponent("diagnostics")
        let diagnostics = try RecordingDiagnostics(directory: diagnosticRoot)
        let diagSession = UUID(), pcmAt = Date(timeIntervalSince1970: 1792230000)
        try diagnostics.update(cleanEnd: false, sessionID: diagSession, lastPCMAt: pcmAt, event: "heartbeat")
        let reopenDiagnostics = try RecordingDiagnostics(directory: diagnosticRoot)
        try check(reopenDiagnostics.checkpoint()?.cleanEnd == false)
        try check(reopenDiagnostics.checkpoint()?.lastPCMAt == pcmAt)
        let coldRaw = diagnosticRoot.appendingPathComponent("retained-source.caf")
        let coldAudio = Data("PUBLIC SYNTHETIC RECOVERY AUDIO".utf8); try coldAudio.write(to: coldRaw)
        let coldIntent = RecordingIntent(); try check(!coldIntent.wanted && !coldIntent.mayActivate(manual: false))
        _ = try reopenDiagnostics.checkpoint(); try check(Data(contentsOf: coldRaw) == coldAudio)
        for i in 0..<80 { try diagnostics.incident("system_interruption", outcome: "waiting", fields: ["reasonCode": String(i)]) }
        try check(diagnostics.incidents().count == 32)
        try check(diagnostics.incidents().first?.fields["reasonCode"] == "48")
        try rejects { try diagnostics.incident("system_interruption", outcome: "waiting", fields: ["pairing_secret": "must-not-log"]) }
        try rejects { try diagnostics.incident("recorded speech must-not-log", outcome: "waiting") }
        try rejects { try diagnostics.incident("capture_error", outcome: "manual", fields: ["domain": "https://must-not-log"]) }
        try rejects { try diagnostics.incident("system_interruption", outcome: "waiting", fields: ["transcript": "must-not-log"]) }
        try rejects { try diagnostics.incident("capture_error", outcome: "manual", fields: ["domain": String(repeating: "x", count: 129)]) }
        let diagBytes = try Data(contentsOf: diagnosticRoot.appendingPathComponent("recording-incidents.json"))
        try check(diagBytes.count <= 65536 && !String(decoding: diagBytes, as: UTF8.self).contains("must-not-log"))
        try diagnostics.update(cleanEnd: true, sessionID: diagSession, lastPCMAt: pcmAt, event: "user_stop_complete")
        try check(reopenDiagnostics.checkpoint()?.cleanEnd == true)
        print("PASS interruption intent/explicit retry/user priority/cold idle + bounded metadata-only incident checkpoint")
        print("PASS independent pairing rejects malformed input, cross-origin and oversized responses")
        print("PASS private transport policy / corrupt manifests fail closed; ALL CORE CHECKS PASSED")
    }
    static func check(_ value: @autoclosure () throws -> Bool) throws { let result = try value(); precondition(result) }
    static func rejects(_ operation: () throws -> Void) throws {
        do { try operation() } catch { return }
        throw NSError(domain: "CoreChecks.ExpectedFailure", code: 1)
    }
}
