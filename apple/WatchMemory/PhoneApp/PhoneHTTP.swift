import Foundation
import WatchMemoryCore

final class PhoneHTTP: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    enum Failure: Error { case status(Int), invalidResponse, tooLarge }
    private struct Pending {
        let maximum: Int; var bytes = Data()
        let completion: (Result<Data, Error>) -> Void
    }
    private let lock = NSLock()
    private var requests: [Int: Pending] = [:]
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 60
        config.urlCache = nil; config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()
    func request(base: URL, path: String, query: [URLQueryItem] = [], method: String = "GET", token: String? = nil,
                 body: Data? = nil, maximum: Int = 2 * 1024 * 1024) async throws -> Data {
        guard path.hasPrefix("/v3/phone/"), !path.contains(".."), !path.contains("?"), !path.contains("#") else { throw StoreError.unsafePath }
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.path = path; components.queryItems = query.isEmpty ? nil : query
        var r = URLRequest(url: components.url!); r.httpMethod = method; r.httpBody = body
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let token { r.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        return try await withCheckedThrowingContinuation { continuation in
            let task = session.dataTask(with: r)
            lock.lock(); requests[task.taskIdentifier] = Pending(maximum: maximum, completion: { continuation.resume(with: $0) }); lock.unlock()
            task.resume()
        }
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock(); defer { lock.unlock() }
        guard var value = requests[dataTask.taskIdentifier] else { return }
        guard value.bytes.count + data.count <= value.maximum else { dataTask.cancel(); return }
        value.bytes.append(data); requests[dataTask.taskIdentifier] = value
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock(); let value = requests.removeValue(forKey: task.taskIdentifier); lock.unlock()
        guard let value else { return }
        if let error { value.completion(.failure(error)); return }
        guard let response = task.response as? HTTPURLResponse else { value.completion(.failure(Failure.invalidResponse)); return }
        guard (200...299).contains(response.statusCode) else { value.completion(.failure(Failure.status(response.statusCode))); return }
        value.completion(.success(value.bytes))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
