import Foundation
import WatchConnectivity
import WatchMemoryCore

final class WatchTransfer: NSObject, WCSessionDelegate {
    private let store: ChunkStore
    private let updated: () -> Void
    private let transportQueue = DispatchQueue(label: "xiaolu.watch.watchtransfer")
    init(store: ChunkStore, updated: @escaping () -> Void) {
        self.store = store; self.updated = updated; super.init()
        if WCSession.isSupported() { WCSession.default.delegate = self; WCSession.default.activate() }
    }
    func sendPending() {
        WatchDirectUploader.shared.scheduleUploads()
        guard UserDefaults.standard.bool(forKey: "watchPhoneFallback") else { updated(); return }
        transportQueue.async { [weak self] in self?.enqueue() }
    }
    private func enqueue() {
        let session = WCSession.default
        guard session.activationState == .activated else { return }
        let outstanding = Set(session.outstandingFileTransfers.compactMap { $0.file.metadata?["chunk_id"] as? String })
        for chunk in store.retryable(uploading: false).prefix(4) {
            guard !outstanding.contains(chunk.id.uuidString.lowercased()) else { continue }
            do {
                let path = try store.fileURL(for: chunk)
                guard FileManager.default.fileExists(atPath: path.path) else { throw StoreError.missingChunk }
                let encoded = try JSONEncoder().encode(chunk.uploadMetadata).base64EncodedString()
                try store.markAttempt(chunk.id, uploading: false)
                session.transferFile(path, metadata: ["chunk_id": chunk.id.uuidString.lowercased(), "metadata": encoded])
                // A queued or WC-delivered file is NOT an application-level persistence receipt.
            } catch { try? store.markFailed(chunk.id, message: "等待与手机重新连接") }
        }
        // Phone custody never permits collection in direct mode. The direct
        // uploader alone collects only authenticated receiver-acknowledged audio.
        try? store.compactCleared()
        updated()
    }
    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        PairingDiagnostics.record("watch_wc_activated", ["state": String(activationState.rawValue), "context_count": String(session.receivedApplicationContext.count)])
        if let error { PairingDiagnostics.failure("watch_wc_activation_error", error) }
        acceptConfiguration(session.receivedApplicationContext); sendPending()
    }
    func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) { acceptConfiguration(applicationContext) }
    func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        PairingDiagnostics.record("watch_live_config_received")
        acceptConfiguration(message)
    }
    private func acceptConfiguration(_ context: [String: Any]) {
        PairingDiagnostics.record("watch_context_received", ["count": String(context.count)])
        guard context["kind"] as? String == "receiver_config_v2", let data = context["configuration"] as? Data else { return }
        WatchDirectUploader.shared.receiveConfiguration(data, phoneFallback: context["phone_fallback"] as? Bool ?? false) { config in
            guard let config else { return }
            PairingDiagnostics.record("watch_config_ack_queued")
            WCSession.default.transferUserInfo(["kind": "receiver_config_stored_v2",
                "key_id": SealedTransport.pairingKeyID(config.pairing_secret),
                "endpoint_digest": SealedTransport.digest(Data(config.endpoint.utf8)),
                "phone_fallback": context["phone_fallback"] as? Bool ?? false])
        }
    }
    func session(_ session: WCSession, didFinish fileTransfer: WCSessionFileTransfer, error: Error?) {
        guard let text = fileTransfer.file.metadata?["chunk_id"] as? String, let id = UUID(uuidString: text) else { return }
        try? store.transferFinished(id, success: error == nil, error: error == nil ? nil : "尚未同步，手表原音保留")
        try? store.compactCleared()
        updated()
    }
    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        guard userInfo["kind"] as? String == "chunk_persisted", let text = userInfo["chunk_id"] as? String,
              let id = UUID(uuidString: text), let sha = userInfo["sha256"] as? String,
              let bytes = userInfo["byte_count"] as? NSNumber else { return }
        do { try store.acknowledgePhone(id, sha256: sha, byteCount: bytes.int64Value); updated() }
        catch { /* Do not delete audio or accept an unverified receipt. */ }
    }
}
