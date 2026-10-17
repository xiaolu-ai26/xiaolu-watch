import Foundation
import WatchMemoryCore

public struct PlannedBackgroundUpload: Sendable {
    public let chunk: Chunk
    public let eligibleAt: Date
    public let earliestBeginDate: Date
}

/// Phone-only policy adapter. Chunk remains the single persisted retry source;
/// URLSession persists each planned earliestBeginDate. No second due journal.
public enum BackgroundUploadPlanning {
    public static func eligibleAt(_ chunk: Chunk) -> Date {
        guard let last = chunk.lastAttemptAt else { return .distantPast }
        // Keep parity with ChunkStore.retryable's existing policy.
        let delay = min(900.0, 30.0 * pow(2, Double(min(chunk.attempts, 5))))
        return last.addingTimeInterval(delay)
    }
    public static func plan(_ chunks: [Chunk], activeEvents: Set<UUID>, limit: Int,
                            now: Date = Date()) -> [PlannedBackgroundUpload] {
        guard limit > 0 else { return [] }
        return chunks.filter { item in
            item.isFinalized && ![.received, .uploaded, .cleared].contains(item.state) && !activeEvents.contains(item.id)
        }.sorted { left, right in
            let a = eligibleAt(left), b = eligibleAt(right)
            if a != b { return a < b }
            if left.capturedAt != right.capturedAt { return left.capturedAt < right.capturedAt }
            return left.id.uuidString < right.id.uuidString
        }.prefix(limit).map { item in
            let due = eligibleAt(item)
            return PlannedBackgroundUpload(chunk: item, eligibleAt: due, earliestBeginDate: max(now, due))
        }
    }
}

/// Serialize/coalesce asynchronous getAllTasks passes without dropping waiters.
/// A pass must call done only after durable task registration + resume, no pending
/// work, or a surfaced blocker. Background app completion cannot race ahead of it.
public final class BackgroundUploadScheduler: @unchecked Sendable {
    public typealias Pass = (@escaping () -> Void) -> Void
    private let queue: DispatchQueue
    private let completionQueue: DispatchQueue
    private let performPass: Pass
    private var running = false
    private var rerun = false
    private var waiters: [() -> Void] = []
    private var backgroundHandlers: [() -> Void] = []
    private var backgroundEventsFinished = false
    private var backgroundDrainInFlight = false

    public init(queue: DispatchQueue, completionQueue: DispatchQueue = .main, performPass: @escaping Pass) {
        self.queue = queue; self.completionQueue = completionQueue; self.performPass = performPass
    }
    public func request(completion: (() -> Void)? = nil) {
        queue.async { self.enqueue(completion) }
    }
    public func registerBackgroundCompletion(_ completion: @escaping () -> Void) {
        queue.async {
            self.backgroundHandlers.append(completion)
            self.flushBackgroundHandlersIfReady()
        }
    }
    public func didFinishBackgroundEvents() {
        queue.async {
            self.backgroundEventsFinished = true
            self.flushBackgroundHandlersIfReady()
        }
    }
    private func flushBackgroundHandlersIfReady() {
        guard backgroundEventsFinished, !backgroundHandlers.isEmpty else { return }
        // Keep the event batch open until asynchronous reconciliation finishes.
        // A handler registered during that pass joins this batch and requests a
        // fresh pass; it must not wait for another OS didFinishEvents callback.
        if backgroundDrainInFlight { enqueue(nil); return }
        backgroundDrainInFlight = true
        enqueue {
            let handlers = self.backgroundHandlers
            self.backgroundHandlers.removeAll()
            self.backgroundEventsFinished = false
            self.backgroundDrainInFlight = false
            self.completionQueue.async { handlers.forEach { $0() } }
        }
    }

    private func enqueue(_ completion: (() -> Void)?) {
        if let completion { waiters.append(completion) }
        if running { rerun = true; return }
        beginPass()
    }
    private func beginPass() {
        running = true
        performPass {
            self.queue.async {
                if self.rerun {
                    self.rerun = false
                    self.beginPass()
                } else {
                    self.running = false
                    let finished = self.waiters; self.waiters.removeAll()
                    finished.forEach { $0() }
                }
            }
        }
    }
}
