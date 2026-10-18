import Foundation
import Security
import WatchMemoryCore
import Darwin

enum PhonePrincipalStore {
    private static let service = "xiaolu.phone.context.v3"
    static func read<T: Decodable>(_ account: String, as type: T.Type) throws -> T? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
            kSecAttrAccount as String: account, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var value: CFTypeRef?; let status = SecItemCopyMatching(query as CFDictionary, &value)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = value as? Data else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        return try JSONDecoder().decode(type, from: data)
    }
    static func save<T: Encodable>(_ value: T, account: String) throws {
        let data = try JSONEncoder().encode(value)
        let identity: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
        let change: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(identity as CFDictionary, change as CFDictionary)
        if status == errSecItemNotFound { status = SecItemAdd(identity.merging(change) { _, new in new } as CFDictionary, nil) }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
    static func removePending() { SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: "bootstrap"] as CFDictionary) }
    struct Pending: Codable { let endpoint: String; let code: String; let attemptID: String; let expiresAt: Date }
    static func importPending() throws -> Pending? {
        let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("WatchMemory", isDirectory: true)
        let path = root.appendingPathComponent("phone-onboarding.json")
        let fd = open(path.path, O_RDONLY | O_NOFOLLOW)
        if fd < 0 {
            if errno == ENOENT { return try read("bootstrap", as: Pending.self) }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        defer { close(fd) }
        var info = stat(); guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_size > 0, info.st_size <= 8192 else { throw StoreError.invalidMetadata }
        var bytes = [UInt8](repeating: 0, count: Int(info.st_size)); var offset = 0
        while offset < bytes.count {
            let remaining = bytes.count - offset
            let n = bytes.withUnsafeMutableBytes { raw in Darwin.read(fd, raw.baseAddress!.advanced(by: offset), remaining) }
            guard n > 0 else { throw StoreError.invalidMetadata }; offset += n
        }
        guard let json = try JSONSerialization.jsonObject(with: Data(bytes)) as? [String: String], Set(json.keys) == Set(["endpoint", "code"]),
              let endpoint = json["endpoint"], let code = json["code"] else { throw StoreError.invalidMetadata }
        let base = try PairingClaimPolicy.baseURL(endpoint); let normalized = try PairingClaimPolicy.code(code)
        let previous = try read("bootstrap", as: Pending.self)
        let pending: Pending
        if let previous, previous.endpoint == base.absoluteString, previous.code == normalized { pending = previous }
        else { pending = Pending(endpoint: base.absoluteString, code: normalized, attemptID: UUID().uuidString.lowercased(), expiresAt: Date().addingTimeInterval(600)) }
        try save(pending, account: "bootstrap") // Keep recoverable until the TLS result is safely saved.
        try FileManager.default.removeItem(at: path)
        return pending
    }
}
