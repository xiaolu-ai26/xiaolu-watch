import Foundation
import Security
import Combine
import WatchMemoryCore

private struct PendingReceiverClaim: Codable {
    let endpoint: String
    let code: String
    let attemptID: UUID
    let expiresAt: Date
}
private enum PendingClaimKeychain {
    private static let identity: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "xiaolu.watch.pending-claim.v1", kSecAttrAccount as String: "pending"]
    static func read() throws -> PendingReceiverClaim? {
        var q = identity; q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?; let s = SecItemCopyMatching(q as CFDictionary, &result)
        if s == errSecItemNotFound { return nil }
        guard s == errSecSuccess, let d = result as? Data else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(s)) }
        return try JSONDecoder().decode(PendingReceiverClaim.self, from: d)
    }
    static func save(_ value: PendingReceiverClaim) throws {
        let changes: [String: Any] = [kSecValueData as String: try JSONEncoder().encode(value),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var s = SecItemUpdate(identity as CFDictionary, changes as CFDictionary)
        if s == errSecItemNotFound { s = SecItemAdd(identity.merging(changes) { _, b in b } as CFDictionary, nil) }
        guard s == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(s)) }
    }
    static func clear() throws {
        let s = SecItemDelete(identity as CFDictionary)
        guard s == errSecSuccess || s == errSecItemNotFound else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(s)) }
    }
}

/// Foreground one-time setup. Ephemeral session, bounded response and no redirects.
private final class ClaimRequest: NSObject, URLSessionDataDelegate {
    private var data = Data()
    private var status = 0
    private let completion: (Int, Data, Error?) -> Void
    init(completion: @escaping (Int, Data, Error?) -> Void) { self.completion = completion }
    func start(base: URL, pending: PendingReceiverClaim) throws {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 20; cfg.timeoutIntervalForResource = 30
        cfg.urlCache = nil; cfg.httpCookieStorage = nil; cfg.urlCredentialStorage = nil; cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
        var request = URLRequest(url: base.appendingPathComponent("v2/pair/claim"))
        request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["code": pending.code, "attempt_id": pending.attemptID.uuidString.lowercased()])
        guard request.httpBody!.count <= 1024 else { throw StoreError.invalidMetadata }
        session.dataTask(with: request).resume()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        status = (response as? HTTPURLResponse)?.statusCode ?? 0
        completionHandler(response.expectedContentLength > 8192 ? .cancel : .allow)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive chunk: Data) {
        guard data.count + chunk.count <= 8192 else { dataTask.cancel(); return }; data.append(chunk)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        completion(status, data, error); session.finishTasksAndInvalidate()
    }
}

final class WatchPairingSetup: ObservableObject {
    static let shared = WatchPairingSetup()
    private var automaticAttempts = 0
    private var automatic = false
    @Published var endpoint = ""
    @Published var code = ""
    @Published private(set) var busy = false
    @Published private(set) var message = "输入处理电脑上的 HTTPS 地址和一次性配对码。"
    init() {
        do {
            if let pending = try PendingClaimKeychain.read() {
                if pending.expiresAt <= Date() { try PendingClaimKeychain.clear(); message = "旧配对码已过期，请在处理电脑生成新码。" }
                else { endpoint = pending.endpoint; message = "上次配对未完成，可点连接继续；无需重新输入旧码。" }
            }
        } catch { message = "暂时无法读取配对进度，请稍后重试。" }
    }
    /// The signed development installer can deliver only a short-term bootstrap file.
    /// No user typing and no direct Keychain writes by external tools.
    func resumeAutomatically() {
        guard !busy else { return }
        do {
            let file = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("WatchMemory/onboarding.json")
            if let imported = try ReceiverBootstrapImport.readRegularFile(file) {
                let previous = try PendingClaimKeychain.read()
                let pending: PendingReceiverClaim
                if let previous, previous.endpoint == imported.endpoint, previous.code == imported.code, previous.expiresAt > Date() { pending = previous }
                else { pending = PendingReceiverClaim(endpoint: imported.endpoint, code: imported.code, attemptID: UUID(), expiresAt: Date().addingTimeInterval(600)); automaticAttempts = 0 }
                try PendingClaimKeychain.save(pending)
                try FileManager.default.removeItem(at: file) // only after durable Keychain recovery exists
                PairingDiagnostics.record("watch_installer_bootstrap_imported")
            }
            guard let pending = try PendingClaimKeychain.read() else { return }
            guard pending.expiresAt > Date() else { try PendingClaimKeychain.clear(); message = "初始连接已过期，请在安装工具中重新连接。"; return }
            guard automaticAttempts < 3 else { return }
            endpoint = pending.endpoint; code = ""; automatic = true; automaticAttempts += 1
            connect()
        } catch {
            PairingDiagnostics.failure("watch_installer_bootstrap_error", error)
            message = "自动连接尚未完成，原音已保留。"
        }
    }
    private func failed(_ text: String) {
        DispatchQueue.main.async {
            self.busy = false; self.message = text
            if self.automatic && self.automaticAttempts < 3 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in self?.resumeAutomatically() }
            }
        }
    }
    func connect() {
        guard !busy else { return }
        do {
            let base = try PairingClaimPolicy.baseURL(endpoint)
            let previous = try PendingClaimKeychain.read()
            let normalized = code.isEmpty ? nil : try PairingClaimPolicy.code(code)
            let pending: PendingReceiverClaim
            if let previous, previous.endpoint == base.absoluteString, normalized == nil || normalized == previous.code {
                guard previous.expiresAt > Date() else { try PendingClaimKeychain.clear(); message = "配对码已过期，请在电脑生成新码。"; return }
                pending = previous
            } else {
                guard let normalized else { message = "请填写电脑显示的一次性配对码。"; return }
                pending = PendingReceiverClaim(endpoint: base.absoluteString, code: normalized, attemptID: UUID(), expiresAt: Date().addingTimeInterval(600))
                try PendingClaimKeychain.save(pending) // retain identity before the first network request
            }
            busy = true; message = "正在安全连接处理电脑…"
            let request = ClaimRequest { [weak self] status, body, error in
                guard let self else { return }
                if let error {
                    PairingDiagnostics.failure("watch_claim_network_error", error)
                    self.failed("网络连接未完成，原音保留；稍后会自动重试。"); return
                }
                guard status == 200 else {
                    PairingDiagnostics.record("watch_claim_http_error", ["status": String(status)])
                    if status == 410 { try? PendingClaimKeychain.clear() }
                    self.failed(status == 410 ? "初始连接已过期，请重新安装连接信息。" : "自动连接尚未完成，原音保留；稍后重试。"); return
                }
                do {
                    let config = try PairingClaimPolicy.validateResponse(body, requestedBase: base)
                    let encoded = try JSONEncoder().encode(config)
                    WatchDirectUploader.shared.receiveConfiguration(encoded, phoneFallback: false) { saved in
                        guard saved != nil else { self.failed("设置尚未保存，保留配对进度；稍后重试。"); return }
                        // Never discard recovery credentials until the long-term config is durable.
                        do { try PendingClaimKeychain.clear() }
                        catch { PairingDiagnostics.failure("watch_claim_cleanup_error", error) }
                        PairingDiagnostics.record("watch_independent_config_saved")
                        DispatchQueue.main.async { self.busy = false; self.code = ""; self.message = "处理电脑已连接，已有录音将自动上传。" }
                    }
                } catch {
                    PairingDiagnostics.failure("watch_claim_response_error", error)
                    self.failed("电脑返回的连接信息无法验证，原音已保留。")
                }
            }
            try request.start(base: base, pending: pending)
        } catch {
            busy = false; message = "请输入有效的 HTTPS 根地址及16位配对码。"
            PairingDiagnostics.failure("watch_claim_setup_error", error)
        }
    }
}
