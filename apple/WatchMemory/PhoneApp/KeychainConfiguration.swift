import Foundation
import Security
import WatchMemoryCore

enum KeychainConfiguration {
    private static let service = "xiaolu.watch.pairing.v2"
    static func read() throws -> PairingConfiguration? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "receiver",
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        let value = try JSONDecoder().decode(PairingConfiguration.self, from: data)
        _ = try value.validatedURL()
        return value
    }
    static func save(_ config: PairingConfiguration) throws {
        _ = try config.validatedURL()
        let data = try JSONEncoder().encode(config)
        let identity: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "receiver"]
        let changes: [String: Any] = [kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(identity as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(identity.merging(changes) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
}

// Fixed, bounded connection metadata only; never credentials, context bodies or speech.
enum PairingDiagnostics {
    private static let lock = NSLock()
    static func record(_ event: String, _ fields: [String: String] = [:]) {
        lock.lock(); defer { lock.unlock() }
        do {
            let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("WatchMemory", isDirectory: true)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            let path = root.appendingPathComponent("pairing-events.jsonl")
            if ((try? FileManager.default.attributesOfItem(atPath: path.path)[.size] as? NSNumber)?.intValue ?? 0) > 65536 { try Data().write(to: path, options: .atomic) }
            var line = try JSONSerialization.data(withJSONObject: ["at": Date().timeIntervalSince1970, "event": event, "fields": fields]); line.append(10)
            if !FileManager.default.fileExists(atPath: path.path) { try Data().write(to: path, options: .atomic) }
            let f = try FileHandle(forWritingTo: path); defer { try? f.close() }
            try f.seekToEnd(); try f.write(contentsOf: line); try f.synchronize()
        } catch { /* Diagnostic failure never changes configuration or custody. */ }
    }
    static func failure(_ event: String, _ error: Error) {
        let e = error as NSError; record(event, ["domain": e.domain, "code": String(e.code)])
    }
}
