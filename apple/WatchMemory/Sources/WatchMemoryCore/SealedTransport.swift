import Foundation
import CryptoKit

public enum TransportError: Error {
    case invalidPairing, oversized, invalidEvent, invalidChallenge, missingMAC, authenticationFailed, incompleteAttempt
}

public struct UploadAttempt: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let challenge: String
    public let path: String
    public let cipherSHA256: String
    public let cipherByteCount: Int
    public let cipherFileName: String
    public let pairingKeyID: String
    public let expectedMetadata: UploadMetadata
    public let createdAt: Date
    public var systemTaskID: Int?
    public var state: String
    public var sessionIdentity: UploadSessionIdentity? = nil
}

/// Wire v2. Only ciphertext, a random event UUID and a one-attempt challenge leave
/// the phone. HTTP redirects are NOT a security boundary for background sessions.
public enum SealedTransport {
    public static let protocolName = "watch-memory-chunks-v2"
    public static let maxAudioBytes = 16 * 1024 * 1024
    public static let maxMetadataBytes = 64 * 1024
    public static let maxResponseBytes = 64 * 1024
    public static let maxWireBytes = maxAudioBytes + maxMetadataBytes + 32
    private static let salt = Data("watch-memory-v2".utf8)

    private static func key(_ secret: String, info: String) -> SymmetricKey {
        HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: Data(secret.utf8)), salt: salt,
                              info: Data(info.utf8), outputByteCount: 32)
    }
    public static func pairingKeyID(_ secret: String) -> String { digest(Data(secret.utf8)) }
    public static func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    public static func aad(path: String, challenge: String) throws -> Data {
        guard let uuid = UUID(uuidString: challenge), uuid.uuidString.lowercased() == challenge,
              path.hasPrefix("/v2/chunks/"), let event = UUID(uuidString: String(path.dropFirst("/v2/chunks/".count))),
              path == "/v2/chunks/" + event.uuidString.lowercased() else { throw TransportError.invalidChallenge }
        return Data("watch-memory-upload-v2\nPUT\n\(path)\n\(challenge)".utf8)
    }
    public static func seal(metadata: UploadMetadata, audio: Data, configuration: PairingConfiguration,
                            attemptID: UUID = UUID(), nonceBytes: Data? = nil) throws -> (UploadAttempt, Data) {
        _ = try configuration.validatedURL()
        _ = try metadata.makeChunk()
        guard audio.count <= maxAudioBytes, audio.count == metadata.byte_count,
              digest(audio) == metadata.sha256 else { throw TransportError.invalidEvent }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let meta = try encoder.encode(metadata)
        guard !meta.isEmpty, meta.count <= maxMetadataBytes else { throw TransportError.oversized }
        let path = "/v2/chunks/" + metadata.chunk_id
        let challenge = attemptID.uuidString.lowercased()
        let associated = try aad(path: path, challenge: challenge)
        var count = UInt32(meta.count).bigEndian
        var plaintext = withUnsafeBytes(of: &count) { Data($0) }
        plaintext.append(meta); plaintext.append(audio)
        let nonce = try nonceBytes.map { try AES.GCM.Nonce(data: $0) } ?? AES.GCM.Nonce()
        let box = try AES.GCM.seal(plaintext, using: key(configuration.pairing_secret, info: "upload-encryption"),
                                   nonce: nonce, authenticating: associated)
        guard let combined = box.combined, combined.count <= maxWireBytes else { throw TransportError.oversized }
        let attempt = UploadAttempt(id: attemptID, challenge: challenge, path: path,
            cipherSHA256: digest(combined), cipherByteCount: combined.count,
            cipherFileName: challenge + ".sealed", pairingKeyID: pairingKeyID(configuration.pairing_secret),
            expectedMetadata: metadata, createdAt: Date(), systemTaskID: nil, state: "prepared")
        return (attempt, combined)
    }
    public static func verifyReceipt(_ body: Data, macBase64: String?, statusCode: Int,
                                     attempt: UploadAttempt, configuration: PairingConfiguration) throws -> ServerReceipt {
        _ = try configuration.validatedURL()
        guard body.count <= maxResponseBytes, (100...599).contains(statusCode),
              pairingKeyID(configuration.pairing_secret) == attempt.pairingKeyID,
              attempt.challenge == attempt.id.uuidString.lowercased(),
              attempt.path == "/v2/chunks/" + attempt.expectedMetadata.chunk_id else { throw TransportError.invalidEvent }
        guard let macBase64, macBase64.utf8.count <= 64, let code = Data(base64Encoded: macBase64), code.count == 32 else { throw TransportError.missingMAC }
        var authenticated = Data("watch-memory-receipt-v2\n\(attempt.challenge)\n\(attempt.cipherSHA256)\n\(statusCode)\n".utf8)
        authenticated.append(body)
        // CryptoKit performs constant-time comparison. Decode untrusted JSON ONLY after MAC.
        guard HMAC<SHA256>.isValidAuthenticationCode(code, authenticating: authenticated,
            using: key(configuration.pairing_secret, info: "receipt-authentication")) else { throw TransportError.authenticationFailed }
        let receipt = try JSONDecoder().decode(ServerReceipt.self, from: body)
        let expected = try attempt.expectedMetadata.makeChunk()
        guard receipt.validates(statusCode: statusCode, chunk: expected) else { throw StoreError.receiptMismatch }
        return receipt
    }
    /// Used only by deterministic, public fixtures / the interoperability check tool.
    public static func fixtureReceiptMAC(body: Data, statusCode: Int, attempt: UploadAttempt, secret: String) -> String {
        var input = Data("watch-memory-receipt-v2\n\(attempt.challenge)\n\(attempt.cipherSHA256)\n\(statusCode)\n".utf8)
        input.append(body)
        return Data(HMAC<SHA256>.authenticationCode(for: input, using: key(secret, info: "receipt-authentication"))).base64EncodedString()
    }
}

/// Distinct owner-deletion evidence. A 410 or stored receipt alone can never authorize local retirement.
public struct OwnerRetirementReceipt: Codable, Sendable {
    public let schema_version: Int
    public let `protocol`: String
    public let purpose: String
    public let retired: Bool
    public let source_id: String
    public let sha256: String
    public let chunk_id: String
    public let session_id: String
    public let sequence: Int
    public let deletion_id: String
    public func validates(_ chunk: Chunk) -> Bool {
        schema_version == 2 && self.protocol == SealedTransport.protocolName && purpose == "owner_deletion" && retired &&
        source_id == chunk.sha256 && sha256 == chunk.sha256 && chunk_id == chunk.id.uuidString.lowercased() &&
        session_id == chunk.sessionID.uuidString.lowercased() && sequence == chunk.sequence && PhoneV3Policy.uuid(deletion_id)
    }
}
extension SealedTransport {
    public static func verifyRetirement(_ body: Data, macBase64: String?, statusCode: Int,
                                        attempt: UploadAttempt, configuration: PairingConfiguration) throws -> OwnerRetirementReceipt {
        _ = try configuration.validatedURL()
        guard statusCode == 410, body.count <= maxResponseBytes, pairingKeyID(configuration.pairing_secret) == attempt.pairingKeyID,
              attempt.challenge == attempt.id.uuidString.lowercased(),
              attempt.path == "/v2/chunks/" + attempt.expectedMetadata.chunk_id else { throw TransportError.invalidEvent }
        guard let macBase64, macBase64.utf8.count <= 64, let mac = Data(base64Encoded: macBase64), mac.count == 32 else { throw TransportError.missingMAC }
        var input = Data("watch-memory-retired-v2\n\(attempt.challenge)\n\(attempt.cipherSHA256)\n410\n".utf8); input.append(body)
        guard HMAC<SHA256>.isValidAuthenticationCode(mac, authenticating: input,
            using: key(configuration.pairing_secret, info: "retirement-authentication")) else { throw TransportError.authenticationFailed }
        let receipt = try JSONDecoder().decode(OwnerRetirementReceipt.self, from: body)
        guard receipt.validates(try attempt.expectedMetadata.makeChunk()) else { throw StoreError.receiptMismatch }
        return receipt
    }
    public static func fixtureRetirementMAC(body: Data, attempt: UploadAttempt, secret: String) -> String {
        var input = Data("watch-memory-retired-v2\n\(attempt.challenge)\n\(attempt.cipherSHA256)\n410\n".utf8); input.append(body)
        return Data(HMAC<SHA256>.authenticationCode(for: input, using: key(secret, info: "retirement-authentication"))).base64EncodedString()
    }
}
