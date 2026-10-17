import Foundation

/// Initial receiver setup only. Long-term audio authentication stays on sealed v2.
public enum PairingClaimPolicy {
    public static func baseURL(_ text: String) throws -> URL {
        guard text.utf8.count <= 2048, var c = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              c.scheme?.lowercased() == "https", let host = c.host, !host.isEmpty,
              c.user == nil, c.password == nil, c.query == nil, c.fragment == nil,
              c.path.isEmpty || c.path == "/", c.port == nil || (1...65535).contains(c.port!) else { throw StoreError.invalidMetadata }
        c.scheme = "https"; c.host = host.lowercased(); c.path = ""
        guard let url = c.url else { throw StoreError.invalidMetadata }; return url
    }
    public static func code(_ text: String) throws -> String {
        guard text.utf8.count <= 64 else { throw StoreError.invalidMetadata }
        let value = text.uppercased().filter { $0 != "-" && $0 != " " }
        guard value.count == 16, value.allSatisfy({ "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567".contains($0) }) else { throw StoreError.invalidMetadata }
        return value
    }
    public static func validateResponse(_ data: Data, requestedBase: URL) throws -> PairingConfiguration {
        guard data.count <= 8192 else { throw StoreError.invalidMetadata }
        let c = try JSONDecoder().decode(PairingConfiguration.self, from: data)
        let endpoint = try c.validatedURL()
        guard endpoint.scheme == "https", endpoint.host?.lowercased() == requestedBase.host?.lowercased(),
              (endpoint.port ?? 443) == (requestedBase.port ?? 443) else { throw StoreError.invalidMetadata }
        return c
    }
}
