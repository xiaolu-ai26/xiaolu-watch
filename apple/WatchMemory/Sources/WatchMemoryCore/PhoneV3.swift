import Foundation

/// A separate principal for phone context/read operations. Never derives from the audio pairing secret.
public struct PhoneV3Configuration: Codable, Equatable, Sendable {
    public let version: Int
    public let endpoint: String
    public let token: String
    public let principalID: String
    public let scopes: [String]
    public let protocolName: String
    enum CodingKeys: String, CodingKey {
        case version, endpoint, token, scopes
        case principalID = "principal_id"
        case protocolName = "protocol"
    }
    public func baseURL() throws -> URL {
        let base = try PairingClaimPolicy.baseURL(endpoint)
        guard version == 3, protocolName == "watch-memory-phone-v3",
              UUID(uuidString: principalID)?.uuidString.lowercased() == principalID,
              token.utf8.count == 43, token.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }),
              Set(["context:write", "today:read", "cards:delete"]).isSubset(of: Set(scopes)) else { throw StoreError.invalidMetadata }
        return base
    }
    public static func response(_ data: Data, origin: URL) throws -> Self {
        guard data.count <= 8192 else { throw StoreError.invalidMetadata }
        let result = try JSONDecoder().decode(Self.self, from: data)
        let base = try result.baseURL()
        guard base.host == origin.host, (base.port ?? 443) == (origin.port ?? 443) else { throw StoreError.invalidMetadata }
        return result
    }
}

public enum PhoneV3Policy {
    public static func day(_ value: String) -> Bool {
        guard value.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil else { return false }
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(secondsFromGMT: 0); f.dateFormat = "yyyy-MM-dd"; f.isLenient = false
        return f.date(from: value).map { f.string(from: $0) == value } ?? false
    }
    public static func uuid(_ value: String) -> Bool { UUID(uuidString: value)?.uuidString.lowercased() == value }
    public static func date(_ value: String) -> Date? {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: value) { return d }
        f.formatOptions = [.withInternetDateTime]; return f.date(from: value)
    }
    public static func timestamp(_ date: Date) -> String {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f.string(from: date)
    }
}

public struct PhoneContextAcknowledgement: Decodable, Sendable {
    public let version: Int
    public let requestId: String
    public let accepted: Bool
    public let storedAt: String
    public let contextRevision: Int
    public static func validate(_ data: Data, requestID: String) throws -> Self {
        guard data.count <= 65536, PhoneV3Policy.uuid(requestID) else { throw StoreError.invalidMetadata }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let result = try decoder.decode(Self.self, from: data)
        guard result.version == 3, result.requestId == requestID, result.accepted,
              PhoneV3Policy.date(result.storedAt) != nil, result.contextRevision > 0 else { throw StoreError.receiptMismatch }
        return result
    }
}

/// Shared by synchronous location samples and delayed place-name callbacks.
/// A recent timestamp alone does not authorize collection across an opt-out boundary.
public enum PhoneCollectionPolicy {
    public static func accepts(start: Date, floor: Date, capturedEpoch: String, currentEpoch: String,
                               enabled: Bool, now: Date) -> Bool {
        enabled && PhoneV3Policy.uuid(capturedEpoch) && capturedEpoch == currentEpoch && start >= floor && start <= now
    }
}
