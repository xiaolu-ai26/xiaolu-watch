import Foundation
import CryptoKit
import Combine
import UIKit
import WatchConnectivity
import WatchMemoryCore

final class PhoneCoordinator: NSObject, ObservableObject, WCSessionDelegate, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = PhoneCoordinator()
    static let backgroundID = "xiaolu.watch.uploads.v2"
    @Published private(set) var ready = false
    @Published private(set) var verifiedConnection = false
    @Published private(set) var watchStatus = "正在连接手表"
    @Published private(set) var uploadStatus = "先连接家中的处理电脑"
    @Published private(set) var pending = 0
    @Published private(set) var completed = 0
    @Published private(set) var lastError: String?
    @Published private(set) var phoneFallbackEnabled = UserDefaults.standard.bool(forKey: "phoneAudioFallbackEnabled")
    private let store: ChunkStore?
    private let attempts: UploadAttemptStore?
    private let queue = DispatchQueue(label: "xiaolu.watch.phone", qos: .utility)
    private let responseLock = NSLock()
    private var responses: [Int: Data] = [:]
    private lazy var scheduling = BackgroundUploadScheduler(queue: queue) { [weak self] done in
        guard let self else { done(); return }
        self.performSchedulingPass(completion: done)
    }
    private var timer: Timer?
    private lazy var uploads: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.backgroundID)
        config.sessionSendsLaunchEvents = true
        config.waitsForConnectivity = true
        config.isDiscretionary = false
        config.timeoutIntervalForRequest = 300
        config.timeoutIntervalForResource = 24 * 3600
        config.allowsCellularAccess = false // preserve mobile data by default; LAN/Tailscale on Wi-Fi
        config.httpMaximumConnectionsPerHost = 2
        let delegateQueue = OperationQueue(); delegateQueue.maxConcurrentOperationCount = 1
        return URLSession(configuration: config, delegate: self, delegateQueue: delegateQueue)
    }()
    private override init() {
        var openedStore: ChunkStore?
        var openedAttempts: UploadAttemptStore?
        do {
            let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("WatchMemory", isDirectory: true)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: root.path)
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            var mutable = root; try mutable.setResourceValues(values)
            openedStore = try ChunkStore(directory: root)
            openedAttempts = try UploadAttemptStore(directory: root.appendingPathComponent("upload-attempts", isDirectory: true))
        } catch { openedStore = nil; openedAttempts = nil }
        store = openedStore; attempts = openedAttempts
        super.init()
        _ = scheduling // initialize once before any WC/background delegate can request a pass
        if WCSession.isSupported() { WCSession.default.delegate = self; WCSession.default.activate() }
        // An older development build may have queued plaintext v1 tasks. Cancel them;
        // never migrate a v1 configuration or permit those requests in production.
        let legacyConfig = URLSessionConfiguration.background(withIdentifier: "xiaolu.watch.uploads.v1")
        let legacy = URLSession(configuration: legacyConfig)
        legacy.getAllTasks { tasks in tasks.forEach { $0.cancel() }; legacy.invalidateAndCancel() }
        _ = uploads // recreate background session promptly after a system relaunch
        DispatchQueue.main.async {
            if let config = try? KeychainConfiguration.read() {
                self.ready = true
                self.verifiedConnection = UserDefaults.standard.string(forKey: "verifiedPairingKeyID-v2") == SealedTransport.pairingKeyID(config.pairing_secret)
            }
            if self.store == nil { self.lastError = "录音资料暂时无法打开，请勿卸载应用。" }
            self.refresh()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.scheduleUploads() }
        scheduleUploads()
    }
    func setBackgroundCompletion(_ completion: @escaping () -> Void) { scheduling.registerBackgroundCompletion(completion) }
    func setPhoneFallback(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: "phoneAudioFallbackEnabled")
        DispatchQueue.main.async { self.phoneFallbackEnabled = enabled }
        publishWatchConfiguration(); scheduleUploads()
    }
    func publishWatchConfiguration() {
        queue.async {
            PairingDiagnostics.record("phone_publish_requested", ["activation": String(WCSession.default.activationState.rawValue)])
            do {
                guard let config = try KeychainConfiguration.read() else { PairingDiagnostics.record("phone_config_missing"); return }
                let endpoint = try config.validatedURL()
                PairingDiagnostics.record("phone_config_available", ["scheme": endpoint.scheme ?? "", "host": endpoint.host ?? ""])
                guard WCSession.default.activationState == .activated, endpoint.scheme == "https" else { return }
                let data = try JSONEncoder().encode(config)
                let context: [String: Any] = ["kind": "receiver_config_v2", "configuration": data,
                    "phone_fallback": UserDefaults.standard.bool(forKey: "phoneAudioFallbackEnabled")]
                try WCSession.default.updateApplicationContext(context)
                PairingDiagnostics.record("phone_wc_reachability", ["reachable": String(WCSession.default.isReachable)])
                if WCSession.default.isReachable {
                    WCSession.default.sendMessage(context, replyHandler: nil) { error in PairingDiagnostics.failure("phone_live_config_error", error) }
                    PairingDiagnostics.record("phone_live_config_sent")
                }
                PairingDiagnostics.record("phone_context_sent", ["scheme": "https", "host": try config.validatedURL().host ?? "", "fallback": String(UserDefaults.standard.bool(forKey: "phoneAudioFallbackEnabled"))])
            } catch { PairingDiagnostics.failure("phone_context_error", error); DispatchQueue.main.async { self.watchStatus = "连接信息已保存在手机，等待下发手表" } }
        }
    }
    func configure(_ text: String, completion: @escaping (Bool) -> Void) {
        queue.async {
            do {
                guard text.utf8.count <= 8192 else { throw StoreError.invalidMetadata }
                let config = try JSONDecoder().decode(PairingConfiguration.self, from: Data(text.utf8))
                guard try config.validatedURL().scheme == "https" else { throw StoreError.invalidMetadata }
                try KeychainConfiguration.save(config)
                PairingDiagnostics.record("phone_config_saved", ["scheme": "https", "host": try config.validatedURL().host ?? ""])
                UserDefaults.standard.removeObject(forKey: "verifiedPairingKeyID-v2")
                // Configuration is local only. No Bearer/secret health request. A valid
                // authenticated receipt for an actual sealed upload verifies the receiver.
                DispatchQueue.main.async {
                    self.ready = true; self.verifiedConnection = false; self.lastError = nil
                    self.uploadStatus = "已保存，收到录音后会验证连接"; completion(true)
                }
                self.publishWatchConfiguration(); self.scheduleUploads()
            } catch { PairingDiagnostics.failure("phone_config_error", error); DispatchQueue.main.async { self.lastError = "这份连接信息无法使用，请重新扫描新的连接二维码"; completion(false) } }
        }
    }
    func scheduleUploads(completion: (() -> Void)? = nil) { scheduling.request(completion: completion) }
    private func showSchedulingBlocker(_ text: String) {
        DispatchQueue.main.async { self.lastError = text }
    }
    // Called only on queue by the coalescing scheduler. No timer is required for
    // future retries: even a not-yet-due retry gets a durable system upload task.
    private func performSchedulingPass(completion: @escaping () -> Void) {
        guard UserDefaults.standard.bool(forKey: "phoneAudioFallbackEnabled") else {
            uploads.getAllTasks { tasks in
                tasks.forEach { $0.cancel() }
                self.refresh(); completion()
            }
            return
        }
        guard let store = self.store, let attempts = self.attempts else {
            showSchedulingBlocker("录音资料暂时无法打开，原音已保留")
            completion(); return
        }
        do {
            guard let config = try KeychainConfiguration.read() else {
                showSchedulingBlocker("请先保存电脑连接信息，录音仍保存在手机")
                self.refresh(); completion(); return
            }
            let base = try config.validatedURL()
                self.uploads.getAllTasks { tasks in
                    self.queue.async {
                        defer { self.refresh(); completion() }
                        var activeEvents = Set<UUID>(); var activeAttempts = Set<UUID>()
                        for task in tasks {
                            guard [.running, .suspended].contains(task.state),
                                  let name = task.taskDescription, let id = UUID(uuidString: name),
                                  name == id.uuidString.lowercased(), let request = task.originalRequest,
                                  let url = request.url, let method = request.httpMethod,
                                  let challenge = request.value(forHTTPHeaderField: "X-Watch-Challenge"),
                                  let attempt = try? attempts.validateBoundTask(id, taskID: task.taskIdentifier,
                                      originalURL: url, httpMethod: method, challengeHeader: challenge,
                                      configuration: config, checkCiphertext: task.state == .suspended) else {
                                task.cancel(); continue
                            }
                            activeAttempts.insert(id)
                            if let event = UUID(uuidString: attempt.expectedMetadata.chunk_id) { activeEvents.insert(event) }
                            // Crash window: bind(taskID) was durable, but resume() never ran.
                            // Identity and ciphertext were checked above before resuming.
                            if task.state == .suspended { task.resume() }
                        }
                        try? attempts.reconcileAndCompact(active: activeAttempts)
                        let slots = max(0, 2 - activeAttempts.count)
                        let plans = BackgroundUploadPlanning.plan(store.all(), activeEvents: activeEvents, limit: slots)
                        for plan in plans {
                            let chunk = plan.chunk
                            var preparingID: UUID?
                            do {
                                let file = try store.fileURL(for: chunk)
                                let size = (try FileManager.default.attributesOfItem(atPath: file.path)[.size] as? NSNumber)?.int64Value ?? 0
                                guard size == chunk.byteCount, size <= SealedTransport.maxAudioBytes else { throw TransportError.oversized }
                                let source = try FileHandle(forReadingFrom: file)
                                let audio: Data
                                do { audio = try source.read(upToCount: SealedTransport.maxAudioBytes + 1) ?? Data(); try source.close() }
                                catch { try? source.close(); throw error }
                                let (attempt, cipher) = try SealedTransport.seal(metadata: chunk.uploadMetadata, audio: audio, configuration: config)
                                preparingID = attempt.id
                                try attempts.prepare(attempt, ciphertext: cipher) // sync cipher+context BEFORE task creation
                                var request = URLRequest(url: base.appendingPathComponent(String(attempt.path.dropFirst())))
                                request.httpMethod = "PUT"
                                request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
                                request.setValue(String(attempt.cipherByteCount), forHTTPHeaderField: "Content-Length")
                                request.setValue(attempt.challenge, forHTTPHeaderField: "X-Watch-Challenge")
                                try store.markAttempt(chunk.id, uploading: true, now: plan.earliestBeginDate)
                                let task = self.uploads.uploadTask(with: request, fromFile: try attempts.cipherURL(attempt))
                                task.taskDescription = attempt.id.uuidString.lowercased()
                                task.earliestBeginDate = plan.earliestBeginDate
                                do { try attempts.bind(attempt.id, taskID: task.taskIdentifier) }
                                catch { task.cancel(); throw error }
                                task.resume()
                            } catch {
                                if let id = preparingID { try? attempts.finish(id, succeeded: false) }
                                try? store.markFailed(chunk.id, message: "这段录音暂未送达，原音已保留")
                                self.showSchedulingBlocker("暂时无法安排自动同步，原音已保留；请检查手机空间和连接信息")
                            }
                        }
                        let free = (try? store.directory.resourceValues(forKeys: [.volumeAvailableCapacityKey]).volumeAvailableCapacity) ?? 0
                        for item in store.all() where item.state == .uploaded {
                            if let ack = item.acknowledgedAt, Date().timeIntervalSince(ack) > 86400 || free < 512 * 1024 * 1024 {
                                try? store.clearUploadedAudio(item.id)
                            }
                        }
                        try? store.compactCleared()
                        self.refresh()
                    }
                }
        } catch {
            showSchedulingBlocker("连接信息暂时无法读取，请解锁后重新扫描连接二维码")
            self.refresh(); completion()
        }
    }
    private func refresh() {
        let chunks = store?.all() ?? []
        DispatchQueue.main.async {
            self.pending = chunks.filter { ![.uploaded, .cleared].contains($0.state) }.count
            self.completed = chunks.filter { [.uploaded, .cleared].contains($0.state) }.count + (self.store?.archivedChunkCount ?? 0)
            if self.ready {
                if !self.phoneFallbackEnabled { self.uploadStatus = "手表直接上传；手机只保存连接设置" }
                else if !self.verifiedConnection { self.uploadStatus = "连接信息已保存，等待首次录音验证" }
                else { self.uploadStatus = self.pending == 0 ? "录音已送达，等待电脑整理" : "\(self.pending) 段已保存在手机，正在同步" }
            }
        }
    }
    private func accept(_ temporary: URL, metadata: [String: Any]?) {
        guard let store, let encoded = metadata?["metadata"] as? String, let bytes = Data(base64Encoded: encoded), bytes.count <= 8192 else { return }
        let staging = store.directory.appendingPathComponent("incoming-" + UUID().uuidString.lowercased() + ".part")
        // WC's temporary URL is valid ONLY until this delegate returns. Copy synchronously.
        do {
            try FileManager.default.copyItem(at: temporary, to: staging)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: staging.path)
        } catch { DispatchQueue.main.async { self.lastError = "这段录音未能保存，手表原音会继续保留" }; return }
        queue.async {
            do {
                let info = try JSONDecoder().decode(UploadMetadata.self, from: bytes)
                let chunk = try info.makeChunk()
                let size = (try FileManager.default.attributesOfItem(atPath: staging.path)[.size] as? NSNumber)?.int64Value ?? 0
                guard size == chunk.byteCount, try Self.hashFile(staging) == chunk.sha256 else { throw StoreError.receiptMismatch }
                let destination = try store.fileURL(for: chunk)
                if FileManager.default.fileExists(atPath: destination.path) {
                    guard try Self.hashFile(destination) == chunk.sha256 else { throw StoreError.receiptMismatch }
                    try FileManager.default.removeItem(at: staging) // verified duplicate only
                } else { try FileManager.default.moveItem(at: staging, to: destination) }
                let durable = try FileHandle(forWritingTo: destination); try durable.synchronize(); try durable.close()
                try store.insert(chunk) // synchronized manifest BEFORE application-level receipt
                WCSession.default.transferUserInfo(["kind": "chunk_persisted", "chunk_id": chunk.id.uuidString.lowercased(),
                    "sha256": chunk.sha256, "byte_count": NSNumber(value: chunk.byteCount)])
                self.refresh(); self.scheduleUploads()
            } catch {
                // Preserve any unverified file for diagnosis; never acknowledge failed persistence.
                DispatchQueue.main.async { self.lastError = "有一段录音需要重新同步，手表原音保留" }
            }
        }
    }
    private static func hashFile(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        var sha = SHA256()
        while let data = try handle.read(upToCount: 256 * 1024), !data.isEmpty { sha.update(data: data) }
        return sha.finalize().map { String(format: "%02x", $0) }.joined()
    }
    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        PairingDiagnostics.record("phone_wc_activated", ["state": String(activationState.rawValue), "paired": String(session.isPaired), "installed": String(session.isWatchAppInstalled)])
        if let error { PairingDiagnostics.failure("phone_wc_activation_error", error) }
        DispatchQueue.main.async { self.watchStatus = session.isPaired ? (session.isWatchAppInstalled ? "手表已配对" : "请在手表上安装小鹿录音") : "请先配对 Apple Watch" }
        publishWatchConfiguration(); scheduleUploads()
    }
    func sessionWatchStateDidChange(_ session: WCSession) { publishWatchConfiguration() }
    func sessionReachabilityDidChange(_ session: WCSession) { if session.isReachable { publishWatchConfiguration() } }
    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        queue.async {
            guard userInfo["kind"] as? String == "receiver_config_stored_v2",
                  let c = try? KeychainConfiguration.read(),
                  userInfo["key_id"] as? String == SealedTransport.pairingKeyID(c.pairing_secret),
                  userInfo["phone_fallback"] as? Bool == UserDefaults.standard.bool(forKey: "phoneAudioFallbackEnabled"),
                  userInfo["endpoint_digest"] as? String == SealedTransport.digest(Data(c.endpoint.utf8)) else { return }
            PairingDiagnostics.record("phone_watch_config_ack")
            DispatchQueue.main.async { self.watchStatus = "处理电脑连接已保存在手表" }
        }
    }
    func sessionDidBecomeInactive(_ session: WCSession) {}
    func sessionDidDeactivate(_ session: WCSession) { session.activate() }
    func session(_ session: WCSession, didReceive file: WCSessionFile) { accept(file.fileURL, metadata: file.metadata) }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        responseLock.lock(); defer { responseLock.unlock() }
        let old = responses[dataTask.taskIdentifier] ?? Data()
        if old.count + data.count <= 65536 { responses[dataTask.taskIdentifier] = old + data }
        else { dataTask.cancel() }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        responseLock.lock(); let data = responses.removeValue(forKey: task.taskIdentifier) ?? Data(); responseLock.unlock()
        queue.async {
            guard let name = task.taskDescription, let id = UUID(uuidString: name),
                  let attempts = self.attempts, let attempt = try? attempts.load(id), let store = self.store,
                  let eventID = UUID(uuidString: attempt.expectedMetadata.chunk_id) else { return }
            do {
                guard attempt.systemTaskID == task.taskIdentifier, error == nil,
                      let response = task.response as? HTTPURLResponse,
                      let config = try KeychainConfiguration.read() else { throw TransportError.incompleteAttempt }
                if response.statusCode == 410 {
                    let mac = response.value(forHTTPHeaderField: "X-Watch-Retirement-MAC")
                    let retired = try SealedTransport.verifyRetirement(data, macBase64: mac, statusCode: response.statusCode, attempt: attempt, configuration: config)
                    try store.retireOwnerDeleted(eventID, receipt: retired, signedBody: data, mac: mac!)
                    try? attempts.finish(id, succeeded: true)
                    self.refresh(); return
                }
                let receipt = try SealedTransport.verifyReceipt(data,
                    macBase64: response.value(forHTTPHeaderField: "X-Watch-Receipt-MAC"),
                    statusCode: response.statusCode, attempt: attempt, configuration: config)
                // No original custody transition occurs until MAC+schema+event are verified.
                try store.acknowledgeServer(eventID, receipt: receipt, statusCode: response.statusCode)
                try? attempts.finish(id, succeeded: true)
                UserDefaults.standard.set(attempt.pairingKeyID, forKey: "verifiedPairingKeyID-v2")
                DispatchQueue.main.async { self.verifiedConnection = true; self.lastError = nil }
            } catch {
                try? attempts.finish(id, succeeded: false)
                try? store.markFailed(eventID, message: "未收到已验证的电脑确认，原音仍保留")
                if UserDefaults.standard.bool(forKey: "phoneAudioFallbackEnabled") {
                    DispatchQueue.main.async { self.lastError = "连接尚未验证，录音仍保存在手机；请检查电脑和网络" }
                }
            }
            self.refresh(); self.scheduleUploads()
        }
    }
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        // Replaces the old queue-drain-only callback: getAllTasks is asynchronous.
        // Persisted deferred retries must be bound/resumed before iOS can suspend us.
        scheduling.didFinishBackgroundEvents()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // Background URLSession may follow redirects without calling this delegate.
        // This is defense in depth only: v2 sends AES-GCM ciphertext and no secret,
        // and an authenticated challenge-bound MAC is required for every receipt.
        completionHandler(nil)
    }
}
