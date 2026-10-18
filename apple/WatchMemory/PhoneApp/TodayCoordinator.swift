import Foundation
import Combine
import UIKit
import WatchMemoryCore

final class TodayCoordinator: NSObject, ObservableObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = TodayCoordinator()
    static let backgroundID = "xiaolu.phone.context.v3.uploads"
    @Published private(set) var connected = false
    @Published private(set) var connecting = false
    @Published private(set) var today: PhoneToday?
    @Published private(set) var readStatus = "连接处理电脑后查看今天"
    @Published private(set) var healthStatus = "身体背景未启用"
    @Published private(set) var placeStatus = "地点背景未启用"
    @Published private(set) var contextStatus = "没有待上传的背景资料"
    @Published private(set) var healthEnabled = UserDefaults.standard.bool(forKey: "phoneHealthContextEnabled")
    @Published private(set) var locationEnabled = UserDefaults.standard.bool(forKey: "phonePlaceContextEnabled")
    @Published var shareDerivedWithAI = UserDefaults.standard.bool(forKey: "phoneShareDerivedWithAI")
    @Published private(set) var deletionStatus: PhoneDeletionStatus?
    @Published var operationError: String?
    private let queue = DispatchQueue(label: "xiaolu.phone.context", qos: .utility)
    private let api = PhoneHTTP()
    private let health = HealthContextCollector()
    private let places = PlaceContextCollector()
    private var config: PhoneV3Configuration?
    private var inflight: Set<String> = []
    private var responses: [Int: Data] = [:]
    private var backgroundCompletions: [() -> Void] = []
    private var finishing = 0
    private var fetchGeneration = UUID()
    private var activeDay: String?
    private var deletionInFlight = false
    private var claimRunning = false
    private var timer: Timer?
    private lazy var uploads: URLSession = {
        let c = URLSessionConfiguration.background(withIdentifier: Self.backgroundID)
        c.sessionSendsLaunchEvents = true; c.waitsForConnectivity = true; c.isDiscretionary = false
        c.allowsCellularAccess = true; c.httpMaximumConnectionsPerHost = 1
        c.timeoutIntervalForRequest = 120; c.timeoutIntervalForResource = 24 * 3600
        let q = OperationQueue(); q.maxConcurrentOperationCount = 1
        return URLSession(configuration: c, delegate: self, delegateQueue: q)
    }()
    private override init() {
        super.init()
        health.status = { [weak self] text in guard let self else { return }; self.healthStatus = text
            let enabled = self.health.enabled
            if self.healthEnabled != enabled { self.healthEnabled = enabled; self.publishFlags() } }
        places.status = { [weak self] text in self?.placeStatus = text; self?.locationEnabled = self?.places.enabled ?? false }
        health.hasPendingCheckpoint = { [weak self] kind in
            guard let self else { return true }
            return self.queue.sync { (try? PhoneContextFiles.packets().contains { $0.checkpointKind == kind && $0.principalID == self.config?.principalID }) ?? true }
        }
        health.receive = { [weak self] values, deleted, kind, anchor in
            guard let self else { throw StoreError.invalidState }
            try self.enqueue(health: values, deleted: deleted, places: [], kind: "health", checkpointKind: kind, checkpoint: anchor)
        }
        places.receive = { [weak self] values in
            guard let self else { throw StoreError.invalidState }
            try self.enqueue(health: [], deleted: [], places: values, kind: "location")
        }
        queue.async {
            do {
                self.config = try PhonePrincipalStore.read("configuration", as: PhoneV3Configuration.self)
                if let config = self.config { _ = try config.baseURL(); DispatchQueue.main.async { self.connected = true } }
            } catch { DispatchQueue.main.async { self.operationError = "手机连接资料暂不可读取，请勿卸载应用" } }
            self.restoreUploads()
        }
        DispatchQueue.main.async {
            if self.health.enabled { self.health.start() }
            if self.places.enabled { self.places.resume() }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            guard let self else { return }; self.scheduleContext()
            if UIApplication.shared.applicationState == .active {
                self.resumeDeletion()
                Task { @MainActor in if let day = self.activeDay, self.connected { await self.load(day: day) } }
            }
        }
    }
    func foreground() {
        queue.async {
            if self.config == nil, let saved = try? PhonePrincipalStore.read("configuration", as: PhoneV3Configuration.self), (try? saved.baseURL()) != nil {
                self.config = saved; DispatchQueue.main.async { self.connected = true }
            }
        }
        connectImported()
        if health.enabled { health.start() }; if places.enabled { places.resume() }
        scheduleContext(); resumeDeletion()
    }
    func requestHealth(_ enabled: Bool) {
        if enabled { guard connected else { operationError = "请先连接处理电脑"; return }; health.requestEnable() }
        else { health.disable(); healthEnabled = false; discardPending(kind: "health") }
        publishFlags()
    }
    func requestLocation(_ enabled: Bool) {
        if enabled { guard connected else { operationError = "请先连接处理电脑"; return }; places.requestEnable(); locationEnabled = true }
        else { places.disable(); locationEnabled = false; discardPending(kind: "location") }
        publishFlags()
    }
    func requestBackgroundLocation() { places.requestBackgroundPermission() }
    func updateShareDerived(_ value: Bool) {
        shareDerivedWithAI = value; UserDefaults.standard.set(value, forKey: "phoneShareDerivedWithAI"); publishFlags()
    }
    private func publishFlags() { do { try enqueue(health: [], deleted: [], places: [], kind: "flags") } catch { operationError = "设置已保存在手机，背景开关等待同步" } }
    private func flags(principalID: String) throws -> PhoneContextFlags {
        let account = "control-revision-" + principalID
        let previous = try PhonePrincipalStore.read(account, as: Int.self) ?? 0
        guard previous < Int.max else { throw StoreError.invalidState }
        try PhonePrincipalStore.save(previous + 1, account: account)
        return PhoneContextFlags(healthEnabled: UserDefaults.standard.bool(forKey: "phoneHealthContextEnabled"),
                          locationEnabled: UserDefaults.standard.bool(forKey: "phonePlaceContextEnabled"),
                          shareDerivedWithAi: UserDefaults.standard.bool(forKey: "phoneShareDerivedWithAI"), controlRevision: previous + 1)
    }
    private func enqueue(health: [PhoneHealthSample], deleted: [String], places: [PhonePlaceSample], kind: String,
                         checkpointKind: String? = nil, checkpoint: Data? = nil) throws {
        try queue.sync {
            guard let config else { throw StoreError.invalidState }
            _ = try config.baseURL()
            let id = UUID().uuidString.lowercased()
            let body = PhoneContextBody(requestId: id, observedAt: PhoneV3Policy.timestamp(Date()), timezone: TimeZone.current.identifier,
                functionFlags: try flags(principalID: config.principalID), collectionState: PhoneContextState(health: health.isEmpty && deleted.isEmpty ? "not_queried" : "available", location: places.isEmpty ? "not_queried" : "available"),
                health: health, healthDeleted: deleted, places: places)
            let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase; encoder.outputFormatting = [.sortedKeys]
            let payload = try encoder.encode(body); guard payload.count <= 256 * 1024 else { throw StoreError.invalidMetadata }
            try PhoneContextFiles.save(PhoneContextPacket(id: id, principalID: config.principalID, kind: kind, payload: payload,
                checkpointKind: checkpointKind, checkpointData: checkpoint, attempts: 0, nextAt: Date()))
        }
        scheduleContext()
    }
    func connectImported() {
        queue.async {
            guard !self.claimRunning else { return }
            do {
                guard let pending = try PhonePrincipalStore.importPending() else { return }
                guard pending.expiresAt > Date() else { PhonePrincipalStore.removePending(); DispatchQueue.main.async { self.operationError = "手机连接凭据已过期，需由处理电脑重新投递" }; return }
                self.claimRunning = true
                DispatchQueue.main.async { self.connecting = true }
                Task {
                    defer { self.queue.async { self.claimRunning = false }; DispatchQueue.main.async { self.connecting = false } }
                    do {
                        let base = try PairingClaimPolicy.baseURL(pending.endpoint)
                        let body = try JSONSerialization.data(withJSONObject: ["code": pending.code, "attempt_id": pending.attemptID])
                        let response = try await self.api.request(base: base, path: "/v3/phone/pair/claim", method: "POST", body: body, maximum: 8192)
                        let configuration = try PhoneV3Configuration.response(response, origin: base)
                        try PhonePrincipalStore.save(configuration, account: "configuration")
                        PhonePrincipalStore.removePending()
                        self.queue.async { self.config = configuration }
                        await MainActor.run { self.connected = true; self.operationError = nil; self.readStatus = "处理电脑已连接" }
                        self.scheduleContext()
                    } catch { await MainActor.run { self.operationError = "手机阅读连接暂未完成，已保留凭据待下次重试" } }
                }
            } catch { DispatchQueue.main.async { self.operationError = "手机连接资料导入失败，原有手表连接不受影响" } }
        }
    }
    private func currentConfiguration() throws -> PhoneV3Configuration {
        try queue.sync { guard let config else { throw StoreError.invalidState }; _ = try config.baseURL(); return config }
    }
    @MainActor func load(day: String) async {
        guard PhoneV3Policy.day(day) else { return }
        activeDay = day
        let generation = UUID(); fetchGeneration = generation; readStatus = "正在读取今天的记录"
        do {
            let config = try currentConfiguration()
            let data = try await api.request(base: config.baseURL(), path: "/v3/phone/today", query: [URLQueryItem(name: "date", value: day)], token: config.token)
            let result = try PhoneToday.decode(data, requestedDay: day)
            guard fetchGeneration == generation else { return }
            today = result; readStatus = "资料更新于 " + shortTime(result.generatedAt); operationError = nil
        } catch {
            guard fetchGeneration == generation else { return }
            if today?.date != day { today = nil }
            readStatus = "暂未取得这一天的记录；不代表没有录音"
        }
    }
    private func restoreUploads() {
        uploads.getAllTasks { tasks in self.queue.async {
            for t in tasks { if let id = t.taskDescription, PhoneV3Policy.uuid(id) { self.inflight.insert(id) } else { t.cancel() } }
            self.scheduleLocked()
        } }
    }
    func scheduleContext() { queue.async { self.scheduleLocked() } }
    private func scheduleLocked() {
        do {
            guard let config else { return }
            let pending = try PhoneContextFiles.packets()
            DispatchQueue.main.async { self.contextStatus = pending.isEmpty ? "背景资料已确认送达" : "\(pending.count) 批背景等待送达" }
            guard inflight.isEmpty, let packet = pending.first(where: { $0.principalID == config.principalID }) else { return }
            let base = try config.baseURL(); let file = try PhoneContextFiles.directory().appendingPathComponent(packet.id + ".payload")
            try PhoneContextFiles.write(packet.payload, to: file)
            var request = URLRequest(url: base.appendingPathComponent("v3/phone/context")); request.httpMethod = "POST"
            request.setValue("Bearer " + config.token, forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
            let task = uploads.uploadTask(with: request, fromFile: file); task.taskDescription = packet.id
            task.earliestBeginDate = packet.nextAt
            inflight.insert(packet.id); task.resume()
        } catch { DispatchQueue.main.async { self.contextStatus = "本机背景队列暂不可读取，已有录音不受影响" } }
    }
    private func discardPending(kind: String) {
        queue.async {
            do {
                let packets = try PhoneContextFiles.packets().filter { $0.kind == kind }
                self.uploads.getAllTasks { tasks in tasks.filter { packets.map(\.id).contains($0.taskDescription ?? "") }.forEach { $0.cancel() } }
                for p in packets { try PhoneContextFiles.remove(p) }
            } catch { DispatchQueue.main.async { self.contextStatus = "关闭已保存，部分旧待传资料需稍后清理" } }
        }
    }
    func setBackgroundCompletion(_ completion: @escaping () -> Void) { queue.async { self.backgroundCompletions.append(completion); self.finishBackgroundIfReady() } }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        queue.async {
            var value = self.responses[dataTask.taskIdentifier] ?? Data()
            guard value.count + data.count <= 65536 else { dataTask.cancel(); return }
            value.append(data); self.responses[dataTask.taskIdentifier] = value
        }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        queue.async {
            guard let id = task.taskDescription else { return }
            let body = self.responses.removeValue(forKey: task.taskIdentifier) ?? Data()
            guard let packet = try? PhoneContextFiles.packets().first(where: { $0.id == id }) else {
                self.inflight.remove(id); self.scheduleLocked(); self.finishBackgroundIfReady(); return
            }
            self.finishing += 1
            do {
                guard error == nil, let response = task.response as? HTTPURLResponse, (200...299).contains(response.statusCode),
                      self.config?.principalID == packet.principalID else { throw StoreError.receiptMismatch }
                _ = try PhoneContextAcknowledgement.validate(body, requestID: packet.id)
                if let kind = packet.checkpointKind, let anchor = packet.checkpointData {
                    self.health.commitCheckpoint(kind: kind, data: anchor) { failure in self.queue.async {
                        if failure == nil {
                            do { try PhoneContextFiles.remove(packet) }
                            catch { var retry = packet; retry.attempts += 1; retry.nextAt = Date().addingTimeInterval(300); try? PhoneContextFiles.save(retry) }
                        } else { var retry = packet; retry.attempts += 1; retry.nextAt = Date().addingTimeInterval(300); try? PhoneContextFiles.save(retry) }
                        self.finishUpload(id); if failure == nil { self.health.resumeAfterAcknowledgement(kind) }
                    } }
                } else { try PhoneContextFiles.remove(packet); self.finishUpload(id) }
            } catch {
                var updated = packet; updated.attempts += 1
                updated.nextAt = Date().addingTimeInterval(min(3600, pow(2, Double(min(updated.attempts, 6))) * 30))
                try? PhoneContextFiles.save(updated)
                DispatchQueue.main.async { self.contextStatus = "背景资料尚未确认送达，原样保留自动重试" }
                self.finishUpload(id)
            }
        }
    }
    private func finishUpload(_ id: String) {
        inflight.remove(id); finishing = max(0, finishing - 1); scheduleLocked(); finishBackgroundIfReady()
    }
    private var backgroundEventsDrained = false
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) { queue.async { self.backgroundEventsDrained = true; self.finishBackgroundIfReady() } }
    private func finishBackgroundIfReady() {
        guard backgroundEventsDrained, finishing == 0, !backgroundCompletions.isEmpty else { return }
        let handlers = backgroundCompletions; backgroundCompletions.removeAll(); backgroundEventsDrained = false
        DispatchQueue.main.async { handlers.forEach { $0() } }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    private func shortTime(_ value: String) -> String {
        guard let d = PhoneV3Policy.date(value) else { return "时间待确认" }; return d.formatted(date: .omitted, time: .shortened)
    }
    // Deletion operations are defined below; a confirmed transaction survives a phone restart.
    @MainActor func makeDeletionPlan(_ card: PhoneToday.Card) async throws -> PhoneDeletionPlan {
        let config = try currentConfiguration()
        let body = try JSONSerialization.data(withJSONObject: ["request_id": UUID().uuidString.lowercased(), "expected_card_version": card.version])
        let data = try await api.request(base: config.baseURL(), path: "/v3/phone/cards/" + card.id + "/deletion-plan", method: "POST", token: config.token, body: body, maximum: 65536)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let plan = try decoder.decode(PhoneDeletionPlan.self, from: data); try plan.validate(card: card); return plan
    }
    @MainActor func confirmDeletion(_ plan: PhoneDeletionPlan) async {
        do {
            let config = try currentConfiguration()
            let path = try PhoneContextFiles.directory().appendingPathComponent("confirmed-deletion.json")
            let pending: PhonePendingDeletion
            if FileManager.default.fileExists(atPath: path.path) {
                let previous = try JSONDecoder().decode(PhonePendingDeletion.self, from: Data(contentsOf: path))
                guard previous.principalID == config.principalID, previous.plan.planId == plan.planId else {
                    operationError = "上一条回收还在处理中，请先查看回收状态"; return
                }
                pending = previous
            } else {
                pending = PhonePendingDeletion(principalID: config.principalID, plan: plan, requestID: UUID().uuidString.lowercased())
                try PhoneContextFiles.write(JSONEncoder().encode(pending), to: path)
            }
            try await performDeletion(pending, config: config)
        } catch { operationError = "删除结果尚未确认；未重复提交新事务，可稍后重试" }
    }
    private func resumeDeletion() {
        Task { @MainActor in
            do {
                let file = try PhoneContextFiles.directory().appendingPathComponent("confirmed-deletion.json")
                guard FileManager.default.fileExists(atPath: file.path) else { return }
                let p = try JSONDecoder().decode(PhonePendingDeletion.self, from: Data(contentsOf: file)); let config = try currentConfiguration()
                guard p.principalID == config.principalID else { return }
                try await performDeletion(p, config: config)
            } catch { /* Keep the confirmed transaction for explicit retry; do not claim deletion. */ }
        }
    }
    @MainActor private func performDeletion(_ pending: PhonePendingDeletion, config: PhoneV3Configuration) async throws {
        guard !deletionInFlight else { return }; deletionInFlight = true; defer { deletionInFlight = false }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let body = try JSONSerialization.data(withJSONObject: ["request_id": pending.requestID, "plan_revision": pending.plan.planRevision, "confirmation_token": pending.plan.confirmationToken])
        let data: Data
        do { data = try await api.request(base: config.baseURL(), path: "/v3/phone/deletions/" + pending.plan.planId + "/commit", method: "POST", token: config.token, body: body, maximum: 65536) }
        catch PhoneHTTP.Failure.status(410) {
            try FileManager.default.removeItem(at: PhoneContextFiles.directory().appendingPathComponent("confirmed-deletion.json"))
            throw PhoneHTTP.Failure.status(410)
        }
        let result = try decoder.decode(PhoneDeletionStatus.self, from: data)
        try result.validate(expectedID: pending.plan.planId, cardID: pending.plan.cardId); deletionStatus = result
        if result.state == "completed" {
            try FileManager.default.removeItem(at: PhoneContextFiles.directory().appendingPathComponent("confirmed-deletion.json"))
        }
    }
}
