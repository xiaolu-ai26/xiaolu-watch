import Foundation

public struct PairingConfiguration: Codable, Equatable, Sendable {
    public let version: Int
    public let endpoint: String
    public let pairing_secret: String
    public let `protocol`: String
    public init(version: Int = 2, endpoint: String, pairing_secret: String, protocolName: String = "watch-memory-chunks-v2") {
        self.version = version; self.endpoint = endpoint; self.pairing_secret = pairing_secret; self.protocol = protocolName
    }
    public func validatedURL() throws -> URL {
        // Canonical unpadded base64url of 32 random bytes, provisioned by the receiver.
        let alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        let decoded = Data(base64Encoded: pairing_secret.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/") + "=")
        guard version == 2, self.protocol == "watch-memory-chunks-v2", pairing_secret.count == 43,
              pairing_secret.allSatisfy({ alphabet.contains($0) }), decoded?.count == 32,
              decoded?.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "") == pairing_secret,
              let url = URL(string: endpoint), let host = url.host, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty || url.path == "/",
              url.scheme == "https" || (url.scheme == "http" && Self.isPrivateHost(host)) else { throw StoreError.invalidMetadata }
        return url
    }
    public static func isPrivateHost(_ host: String) -> Bool {
        if host == "localhost" || host == "127.0.0.1" || host == "::1" || host.hasSuffix(".ts.net") { return true }
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        let parts = labels.compactMap { Int($0) }
        guard labels.count == 4, parts.count == 4, parts.allSatisfy({ (0...255).contains($0) }) else { return false }
        return parts[0] == 10 || (parts[0] == 172 && (16...31).contains(parts[1])) ||
            (parts[0] == 192 && parts[1] == 168) || (parts[0] == 100 && (64...127).contains(parts[1]))
    }
}
