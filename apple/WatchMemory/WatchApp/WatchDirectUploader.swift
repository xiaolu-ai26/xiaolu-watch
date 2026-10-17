import Foundation
import Combine
import WatchKit
import WatchMemoryCore

/// The watch owns both original audio and its durable system upload tasks.
/// No iPhone audio custody is necessary in the default direct mode.
final class WatchDirectUploader: NSObject, ObservableObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = WatchDirectUploader()
    static let backgroundID = "xiaolu.watch.watch.uploads.v2"
    static let cellularKey = "watchAllowsCellularUpload"
    let store: ChunkStore?
    private let attempts: UploadAttemptStore?
    @Published private(set) var status = "请先用手机设置处理电脑"
    @Published private(set) var pending = 0
    @Published private(set) var completed = 0
    @Published private(set) var allowsCellular = UserDefaults.standard.bool(forKey: cellularKey)
    @Published private(set) var phoneFallback = UserDefaults.standard.bool(forKey: "watchPhoneFallback")
    private let queue = DispatchQueue(label: "watchmemory.watch.direct", qos: .utility)
    private let responseBuffers = UploadResponseBuffers()
    private let finishingLock = NSLock()
    private var finishingAttemptIDs = Set<UUID>()
    private let audioScope = UploadSessionIdentity.audio(UUID())
    private var audioRuntimeSession: UUID?
    private var audioMinimumSequence = 0
    private var supersededFast = Set<UUID>()
    private var currentOwners = UploadOwnershipLedger()
    private var handoffFailures: [UUID: Int] = [:]
    private var handoffRetryAt: [UUID: Date] = [:]
    private var retryWork: DispatchWorkItem?
    private var retryDate: Date?
    private var recoveredLostAudioScopes = false
    private struct OwnedTask {
        let task: URLSessionTask
        let scope: UploadSessionIdentity
        let attempt: UploadAttempt
        let event: UUID
    }
    private lazy var fastUploads: URLSession = {
        let c = URLSessionConfiguration.default
        c.waitsForConnectivity = false; c.allowsCellularAccess = true
        c.timeoutIntervalForRequest = 30; c.timeoutIntervalForResource = 90
        c.httpMaximumConnectionsPerHost = 1
        c.urlCache = nil; c.httpCookieStorage = nil; c.urlCredentialStorage = nil
        let delegates = OperationQueue(); delegates.maxConcurrentOperationCount = 1
        return URLSession(configuration: c, delegate: self, delegateQueue: delegates)
    }()
    private lazy var scheduling = BackgroundUploadScheduler(queue: queue) { [weak self] done in
        guard let self else { done(); return }; self.performSchedulingPass(completion: done)
    }
    private lazy var uploads: URLSession = {
        let c = URLSessionConfiguration.background(withIdentifier: Self.backgroundID)
        c.sessionSendsLaunchEvents = true; c.waitsForConnectivity = true; c.isDiscretionary = false
        c.allowsCellularAccess = true // each durable request carries the explicit user preference
        c.timeoutIntervalForRequest = 300; c.timeoutIntervalForResource = 86400
        c.httpMaximumConnectionsPerHost = 2
        let delegates = OperationQueue(); delegates.maxConcurrentOperationCount = 1
        return URLSession(configuration: c, delegate: self, delegateQueue: delegates)
    }()
    private override init() {
        var opened: ChunkStore?; var outbox: UploadAttemptStore?
        do {
            let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("WatchMemory", isDirectory: true)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: root.path)
            var url = root; var flags = URLResourceValues(); flags.isExcludedFromBackup = true; try url.setResourceValues(flags)
            opened = try ChunkStore(directory: root)
            outbox = try UploadAttemptStore(directory: root.appendingPathComponent("direct-attempts", isDirectory: true))
        } catch { opened = nil; outbox = nil }
        store = opened; attempts = outbox
        super.init(); _ = scheduling; _ = uploads; _ = fastUploads
        scheduleUploads()
    }
    func receiveConfiguration(_ data: Data, phoneFallback: Bool, completion: @escaping (PairingConfiguration?) -> Void) {
        queue.async {
            do {
                guard data.count <= 8192 else { throw StoreError.invalidMetadata }
                let c = try JSONDecoder().decode(PairingConfiguration.self, from: data)
                guard try c.validatedURL().scheme == "https" else { throw StoreError.invalidMetadata }
                try KeychainConfiguration.save(c)
                PairingDiagnostics.record("watch_config_saved", ["scheme": "https", "host": try c.validatedURL().host ?? ""])
                UserDefaults.standard.set(phoneFallback, forKey: "watchPhoneFallback")
                DispatchQueue.main.async { self.phoneFallback = phoneFallback }
                completion(c); self.scheduleUploads()
            } catch { PairingDiagnostics.failure("watch_config_error", error); self.showSchedulingBlocker("请在手机保存有效的 HTTPS 连接"); completion(nil) }
        }
    }
    func setCellular(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: Self.cellularKey)
        DispatchQueue.main.async { self.allowsCellular = value }
        uploads.getAllTasks { tasks in tasks.forEach { $0.cancel() }; self.scheduleUploads() }
        fastUploads.getAllTasks { tasks in tasks.forEach { $0.cancel() }; self.scheduleUploads() }
    }
    func setAudioRuntime(_ session: UUID?, minimumSequence: Int = 0) {
        queue.async {
            let floor = session == nil ? 0 : max(0, minimumSequence)
            guard self.audioRuntimeSession != session || self.audioMinimumSequence != floor else { return }
            self.audioRuntimeSession = session; self.audioMinimumSequence = floor
            if session == nil { self.retryWork?.cancel(); self.retryWork = nil; self.retryDate = nil }
            PairingDiagnostics.record("watch_audio_upload_runtime", ["active": String(session != nil)])
            self.scheduleUploads()
        }
    }
    func scheduleUploads(completion: (() -> Void)? = nil) { scheduling.request(completion: completion) }
    func handle(_ task: WKURLSessionRefreshBackgroundTask) {
        guard task.sessionIdentifier == Self.backgroundID else { task.setTaskCompletedWithSnapshot(false); return }
        _ = uploads
        scheduling.registerBackgroundCompletion { task.setTaskCompletedWithSnapshot(false) }
        scheduleUploads()
    }
    private func showSchedulingBlocker(_ text: String) { DispatchQueue.main.async { self.status = text } }
    private func scope(for session: URLSession) -> UploadSessionIdentity? {
        if session.configuration.identifier == Self.backgroundID { return .background }
        if session === fastUploads { return audioScope }
        return nil
    }
    private func requestRetry(at date: Date) {
        if let retryDate, retryDate <= date { return }
        retryWork?.cancel(); retryDate = date
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }; self.retryDate = nil; self.retryWork = nil; self.scheduleUploads()
        }
        retryWork = work; queue.asyncAfter(deadline: .now() + max(1, date.timeIntervalSinceNow), execute: work)
    }
    private func createTask(chunk: Chunk, scope: UploadSessionIdentity, when: Date,
                            config: PairingConfiguration, handoff: Bool = false) throws -> OwnedTask {
        guard let store, let attempts else { throw StoreError.invalidState }
        var prepared: UUID?; var task: URLSessionTask?
        do {
            // Persist a bounded retry clock even when source reading/sealing fails.
            try store.markAttempt(chunk.id, uploading: true, now: when)
            let source = try FileHandle(forReadingFrom: store.fileURL(for: chunk))
            let audio: Data
            do { audio = try source.read(upToCount: SealedTransport.maxAudioBytes + 1) ?? Data(); try source.close() }
            catch { try? source.close(); throw error }
            let (a, cipher) = try SealedTransport.seal(metadata: chunk.uploadMetadata, audio: audio, configuration: config)
            prepared = a.id; try attempts.prepare(a, ciphertext: cipher)
            var request = URLRequest(url: try config.validatedURL().appendingPathComponent(String(a.path.dropFirst())))
            request.httpMethod = "PUT"; request.allowsCellularAccess = UserDefaults.standard.bool(forKey: Self.cellularKey)
            request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            request.setValue(String(a.cipherByteCount), forHTTPHeaderField: "Content-Length")
            request.setValue(a.challenge, forHTTPHeaderField: "X-Watch-Challenge")
            let session = scope == .background ? uploads : fastUploads
            let created = session.uploadTask(with: request, fromFile: try attempts.cipherURL(a)); task = created
            created.taskDescription = a.id.uuidString.lowercased()
            if scope == .background { created.earliestBeginDate = when }
            try attempts.bind(a.id, taskID: created.taskIdentifier, session: scope)
            let bound = try attempts.load(a.id)
            currentOwners.add(event: chunk.id, attempt: a.id)
            created.resume()
            PairingDiagnostics.record("watch_upload_scheduled", ["kind": scope.kind, "task": String(created.taskIdentifier), "handoff": String(handoff)])
            return OwnedTask(task: created, scope: scope, attempt: bound, event: chunk.id)
        } catch {
            task?.cancel()
            if let prepared { try? attempts.finish(prepared, succeeded: false) }
            if !handoff { try? store.markFailed(chunk.id, message: "这段录音暂未送达，原音已保留") }
            throw error
        }
    }
    private func owned(_ tasks: [URLSessionTask], scope: UploadSessionIdentity, config: PairingConfiguration) -> [OwnedTask] {
        guard let attempts else { return [] }
        return tasks.compactMap { task in
            guard [.running, .suspended, .canceling].contains(task.state),
                  let name = task.taskDescription, let id = UUID(uuidString: name), name == id.uuidString.lowercased(),
                  let r = task.originalRequest, let url = r.url, let method = r.httpMethod,
                  let challenge = r.value(forHTTPHeaderField: "X-Watch-Challenge"),
                  let a = try? attempts.validateBoundTask(id, taskID: task.taskIdentifier, originalURL: url,
                      httpMethod: method, challengeHeader: challenge, configuration: config,
                      checkCiphertext: task.state == .suspended, session: scope),
                  let event = UUID(uuidString: a.expectedMetadata.chunk_id) else { task.cancel(); return nil }
            if task.state == .suspended { task.resume() }
            return OwnedTask(task: task, scope: scope, attempt: a, event: event)
        }
    }
    private func performSchedulingPass(completion: @escaping () -> Void) {
        guard let store, let attempts else { showSchedulingBlocker("录音资料暂时无法打开，原音已保留"); completion(); return }
        do {
            guard let config = try KeychainConfiguration.read() else {
                showSchedulingBlocker("请先连接处理电脑"); refresh(); completion(); return
            }
            guard try config.validatedURL().scheme == "https" else { throw StoreError.invalidMetadata }
            // A lost default session never claims ownership after a cold restart.
            if !recoveredLostAudioScopes {
                _ = try attempts.abandonOtherAudioScopes(current: audioScope)
                recoveredLostAudioScopes = true
            }
            // Do not change chunk state here: a background handoff may already own it.
            // Uploading with no surviving owner is eligible for a fresh background plan.
            uploads.getAllTasks { background in
                self.fastUploads.getAllTasks { fast in
                    self.queue.async {
                        defer { self.refresh(); completion() }
                        var bg = self.owned(background, scope: .background, config: config)
                        let fg = self.owned(fast, scope: self.audioScope, config: config)
                        // Revocation never cancels a fast owner before a distinct durable background owner exists.
                        for record in fg where !self.supersededFast.contains(record.attempt.id) {
                            guard let chunk = store.chunk(record.event) else { continue }
                            if [.uploaded, .cleared].contains(chunk.state) {
                                self.supersededFast.insert(record.attempt.id); record.task.cancel(); continue
                            }
                            if chunk.sessionID != self.audioRuntimeSession || chunk.sequence < self.audioMinimumSequence {
                                if !bg.contains(where: { $0.event == record.event }) {
                                    guard bg.count < 3, (self.handoffFailures[record.attempt.id] ?? 0) < 3 else { continue }
                                    if let due = self.handoffRetryAt[record.attempt.id], due > Date() { self.requestRetry(at: due); continue }
                                    do {
                                        bg.append(try self.createTask(chunk: chunk, scope: .background, when: Date(), config: config, handoff: true))
                                    } catch {
                                        let count = (self.handoffFailures[record.attempt.id] ?? 0) + 1
                                        self.handoffFailures[record.attempt.id] = count
                                        let due = Date().addingTimeInterval(5)
                                        self.handoffRetryAt[record.attempt.id] = due
                                        if count < 3 { self.requestRetry(at: due) }
                                        self.showSchedulingBlocker("后台接续暂未安排，原音与当前传输均保留")
                                        continue
                                    }
                                }
                                self.supersededFast.insert(record.attempt.id); record.task.cancel()
                            }
                        }
                        let records = bg + fg
                        var active = Set(records.map { $0.attempt.id })
                        var events = Set(records.map { $0.event })
                        self.finishingLock.lock(); let finishing = self.finishingAttemptIDs; self.finishingLock.unlock()
                        var finishingFast = 0; var finishingBG = 0
                        for id in finishing where !active.contains(id) {
                            guard let a = try? attempts.load(id), a.state == "scheduled",
                                  let event = UUID(uuidString: a.expectedMetadata.chunk_id) else { continue }
                            active.insert(id); events.insert(event)
                            if (a.sessionIdentity ?? .background) == .background { finishingBG += 1 } else { finishingFast += 1 }
                        }
                        self.currentOwners.clear()
                        for record in records { self.currentOwners.add(event: record.event, attempt: record.attempt.id) }
                        for id in finishing {
                            if let a = try? attempts.load(id), a.state == "scheduled", let event = UUID(uuidString: a.expectedMetadata.chunk_id) { self.currentOwners.add(event: event, attempt: id) }
                        }
                        try? attempts.reconcileAndCompact(active: active)
                        for item in store.all() where item.state == .received { try? store.requeuePhoneAcknowledgedForDirectUpload(item.id) }
                        let bgCount = bg.count + finishingBG
                        let fastCount = fg.count + finishingFast
                        let fastCandidates = store.all().filter {
                            AudioUploadEligibility.canStartFast(chunk: $0, runtimeSession: self.audioRuntimeSession, fastCount: fastCount, backgroundCount: bgCount, minimumSequence: self.audioMinimumSequence) && !events.contains($0.id)
                        }
                        if let plan = BackgroundUploadPlanning.plan(fastCandidates, activeEvents: events, limit: 1).first {
                            if plan.eligibleAt <= Date() {
                                do {
                                    let record = try self.createTask(chunk: plan.chunk, scope: self.audioScope, when: Date(), config: config)
                                    events.insert(record.event); active.insert(record.attempt.id)
                                } catch { self.requestRetry(at: Date().addingTimeInterval(60)); self.showSchedulingBlocker("及时同步暂未完成，原音保留") }
                            } else { self.requestRetry(at: plan.eligibleAt) }
                        }
                        // All current-session chunks reserve the single fast lane while audio runs.
                        // Existing background-owned chunks never get moved into it.
                        let normal = store.all().filter { $0.sessionID != self.audioRuntimeSession || $0.sequence < self.audioMinimumSequence }
                        for plan in BackgroundUploadPlanning.plan(normal, activeEvents: events, limit: max(0, 2 - bgCount)) {
                            do {
                                let record = try self.createTask(chunk: plan.chunk, scope: .background, when: plan.earliestBeginDate, config: config)
                                events.insert(record.event)
                            } catch { self.showSchedulingBlocker("后台同步暂未安排，原音保留") }
                        }
                        let free = (try? store.directory.resourceValues(forKeys: [.volumeAvailableCapacityKey]).volumeAvailableCapacity) ?? 0
                        for item in store.all() where item.state == .uploaded {
                            if let ack = item.acknowledgedAt, Date().timeIntervalSince(ack) > 86400 || free < 128 * 1024 * 1024 { try? store.clearUploadedAudio(item.id) }
                        }
                        try? store.compactCleared()
                    }
                }
            }
        } catch { showSchedulingBlocker("连接暂时无法读取，原音保留"); refresh(); completion() }
    }

    private func refresh() {
        let all = store?.all() ?? []
        let remaining = all.filter { $0.isFinalized && ![.uploaded, .cleared].contains($0.state) }.count
        let done = all.filter { [.uploaded, .cleared].contains($0.state) }.count + (store?.archivedChunkCount ?? 0)
        let config = try? KeychainConfiguration.read()
        let configured = config != nil
        let verified = config.map {
            UserDefaults.standard.string(forKey: "watchVerifiedReceiverKeyID") == SealedTransport.pairingKeyID($0.pairing_secret) &&
            UserDefaults.standard.string(forKey: "watchVerifiedEndpointDigest") == SealedTransport.digest(Data($0.endpoint.utf8))
        } ?? false
        DispatchQueue.main.async {
            self.pending = remaining; self.completed = done
            self.status = configured ? (remaining == 0 ? (verified ? "录音已直接送达处理电脑" : "设置已保存，等待首段验证处理电脑") : "\(remaining) 段保留在手表，等待直接上传") : "请先用手机设置处理电脑"
        }
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let scope = scope(for: session), responseBuffers.append(data, for: UploadResponseKey(session: scope, taskID: dataTask.taskIdentifier)) else { dataTask.cancel(); return }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let scope = scope(for: session), let name = task.taskDescription, let id = UUID(uuidString: name) else { return }
        let body = responseBuffers.take(UploadResponseKey(session: scope, taskID: task.taskIdentifier))
        finishingLock.lock(); finishingAttemptIDs.insert(id); finishingLock.unlock()
        queue.async {
            defer {
                self.finishingLock.lock(); self.finishingAttemptIDs.remove(id); self.finishingLock.unlock()
                self.supersededFast.remove(id); self.handoffFailures.removeValue(forKey: id); self.handoffRetryAt.removeValue(forKey: id)
                if let a = try? self.attempts?.load(id), let event = UUID(uuidString: a.expectedMetadata.chunk_id) {
                    self.currentOwners.remove(event: event, attempt: id)
                }
                self.refresh(); self.scheduleUploads()
            }
            guard let attempts = self.attempts, let a = try? attempts.load(id), let store = self.store,
                  let event = UUID(uuidString: a.expectedMetadata.chunk_id) else { return }
            do {
                guard error == nil, let r = task.response as? HTTPURLResponse,
                      let c = try KeychainConfiguration.read(), let request = task.originalRequest,
                      let url = request.url, let method = request.httpMethod,
                      let challenge = request.value(forHTTPHeaderField: "X-Watch-Challenge") else { throw TransportError.incompleteAttempt }
                _ = try attempts.validateBoundTask(id, taskID: task.taskIdentifier, originalURL: url, httpMethod: method,
                    challengeHeader: challenge, configuration: c, checkCiphertext: false, session: scope)
                let receipt = try SealedTransport.verifyReceipt(body, macBase64: r.value(forHTTPHeaderField: "X-Watch-Receipt-MAC"),
                    statusCode: r.statusCode, attempt: a, configuration: c)
                try store.acknowledgeServer(event, receipt: receipt, statusCode: r.statusCode)
                try? attempts.finish(id, succeeded: true)
                UserDefaults.standard.set(SealedTransport.pairingKeyID(c.pairing_secret), forKey: "watchVerifiedReceiverKeyID")
                UserDefaults.standard.set(SealedTransport.digest(Data(c.endpoint.utf8)), forKey: "watchVerifiedEndpointDigest")
                PairingDiagnostics.record("watch_upload_verified", ["kind": scope.kind, "task": String(task.taskIdentifier)])
            } catch {
                try? attempts.finish(id, succeeded: false)
                let otherOwner = self.currentOwners.hasOtherOwner(event: event, excluding: id)
                if !self.supersededFast.contains(id) && !otherOwner { try? store.markFailed(event, message: "尚未收到处理电脑的已验证确认，手表原音保留") }
                let e = error as NSError
                let fields: [String: Any] = ["at": Date().timeIntervalSince1970, "domain": e.domain, "code": e.code,
                    "httpStatus": (task.response as? HTTPURLResponse)?.statusCode ?? 0, "sessionKind": scope.kind]
                if let data = try? JSONSerialization.data(withJSONObject: fields) { try? data.write(to: store.directory.appendingPathComponent("last-direct-error.json"), options: .atomic) }
            }
        }
    }
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        if session.configuration.identifier == Self.backgroundID { scheduling.didFinishBackgroundEvents() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
