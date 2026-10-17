import Foundation
import Darwin

/// Private, runtime-only installer delivery. Never accepts a long-term secret.
public struct ReceiverBootstrapImport {
    public let endpoint: String
    public let code: String
    public static func readRegularFile(_ file: URL) throws -> ReceiverBootstrapImport? {
        let fd = Darwin.open(file.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        if fd < 0 {
            if errno == ENOENT { return nil }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_size > 0, info.st_size <= 8192 else { throw StoreError.invalidMetadata }
        let data = try handle.read(upToCount: 8193) ?? Data()
        guard data.count <= 8192, data.count == Int(info.st_size),
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["endpoint", "code"]),
              let endpoint = object["endpoint"] as? String, let code = object["code"] as? String else { throw StoreError.invalidMetadata }
        return ReceiverBootstrapImport(endpoint: try PairingClaimPolicy.baseURL(endpoint).absoluteString,
                                       code: try PairingClaimPolicy.code(code))
    }
}
