import Foundation
import WatchMemoryCore
import PhoneScheduling

private final class Events: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String] = []
    func append(_ value: String) { lock.lock(); defer { lock.unlock() }; values.append(value) }
    func read() -> [String] { lock.lock(); defer { lock.unlock() }; return values }
}
private final class ControlledPasses: @unchecked Sendable {
    private let lock = NSLock()
    private var completions: [() -> Void] = []
    let started = DispatchSemaphore(value: 0)
    let events: Events
    init(events: Events) { self.events = events }
    func begin(_ done: @escaping () -> Void) {
        lock.lock(); completions.append(done); let number = completions.count; lock.unlock()
        events.append("getAllTasks-awaiting-\(number)"); started.signal()
    }
    func finish(_ number: Int) {
        lock.lock(); let done = completions[number - 1]; lock.unlock()
        events.append("cipher-context-bound-\(number)")
        events.append("future-task-resumed-\(number)")
        done()
    }
}

@main
struct SchedulingChecks {
    static func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        guard try condition() else { throw NSError(domain: message, code: 1) }
    }
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try ChunkStore(directory: root)
        let time = Date(timeIntervalSince1970: 1_000_000)
        let session = UUID()
        var ids: [UUID] = []
        for sequence in 0..<3 {
            let id = UUID(); ids.append(id)
            let item = Chunk(id: id, sessionID: session, sequence: sequence,
                capturedAt: time.addingTimeInterval(Double(sequence)), fileName: id.uuidString.lowercased() + ".caf")
            try store.insert(item)
            try Data("original".utf8).write(to: store.fileURL(for: item))
            try store.finalize(id, duration: 1, sha256: String(repeating: "a", count: 64), byteCount: 8)
            try store.markAttempt(id, uploading: true, now: time.addingTimeInterval(Double(sequence * 20)))
            try store.markFailed(id, message: "Simulated Mac offline: no trusted receipt")
        }
        // Reopen real persisted chunk state; no future WC event / foreground advance.
        let reopened = try ChunkStore(directory: root)
        let now = time.addingTimeInterval(1)
        try require(reopened.retryable(now: now, uploading: true).isEmpty, "old now-only filter reproduces background retry gap")
        let plan = BackgroundUploadPlanning.plan(reopened.all(), activeEvents: [], limit: 2, now: now)
        try require(plan.count == 2, "future eligible retries must be registered now")
        try require(plan.map { $0.chunk.id } == Array(ids.prefix(2)), "plan must prioritize persisted due dates")
        try require(plan.map(\.earliestBeginDate) == [time.addingTimeInterval(60), time.addingTimeInterval(80)], "existing backoff must remain 60 seconds after first attempt")
        let active = BackgroundUploadPlanning.plan(reopened.all(), activeEvents: [ids[0]], limit: 1, now: now)
        try require(active.first?.chunk.id == ids[1], "active system event must not get a duplicate task")
        for entry in plan {
            try require(!reopened.retryable(now: entry.eligibleAt.addingTimeInterval(-0.01), uploading: true).contains(where: { $0.id == entry.chunk.id }), "phone due must match existing core before-boundary")
            try require(reopened.retryable(now: entry.eligibleAt, uploading: true).contains(where: { $0.id == entry.chunk.id }), "phone due must match existing core at-boundary")
        }
        try reopened.markAttempt(ids[0], uploading: true, now: plan[0].earliestBeginDate)
        try reopened.markFailed(ids[0], message: "Fast offline failure after planned start")
        try require(BackgroundUploadPlanning.eligibleAt(reopened.chunk(ids[0])!) == time.addingTimeInterval(180), "next backoff must start at planned earliest execution, not early queue creation")
        // Repeated persisted attempts retain the original capped policy.
        for _ in 0..<8 { try reopened.markAttempt(ids[0], uploading: true, now: time) }
        try require(BackgroundUploadPlanning.eligibleAt(reopened.chunk(ids[0])!) == time.addingTimeInterval(900), "backoff cap must remain 900 seconds")
        print("PASS offline persisted future-due retries planned without Timer/foreground/new WC / max slots / active exclusion / parity with original backoff and cap")

        let queue = DispatchQueue(label: "SchedulingChecks.productionQueue")
        let callbacks = DispatchQueue(label: "SchedulingChecks.completionQueue")
        let events = Events(); let controlled = ControlledPasses(events: events)
        let scheduler = BackgroundUploadScheduler(queue: queue, completionQueue: callbacks) { done in controlled.begin(done) }
        let background = DispatchSemaphore(value: 0); let secondBackground = DispatchSemaphore(value: 0); let normal = DispatchSemaphore(value: 0)
        scheduler.request()
        try require(controlled.started.wait(timeout: .now() + 2) == .success, "first asynchronous getAllTasks pass must begin")
        scheduler.registerBackgroundCompletion { events.append("app-completion"); background.signal() }
        scheduler.registerBackgroundCompletion { events.append("second-app-completion"); secondBackground.signal() }
        scheduler.didFinishBackgroundEvents()
        scheduler.request { events.append("overlapping-completion"); normal.signal() }
        queue.sync {} // ensure overlap requests are registered before simulated query returns
        try require(background.wait(timeout: .now() + 0.03) == .timedOut, "app completion must not race asynchronous query")
        controlled.finish(1)
        try require(controlled.started.wait(timeout: .now() + 2) == .success, "overlap must cause a fresh reconciliation pass")
        try require(background.wait(timeout: .now() + 0.03) == .timedOut, "app completion must wait for replacement tasks to bind/resume")
        controlled.finish(2)
        try require(background.wait(timeout: .now() + 2) == .success, "app completion waiter lost")
        try require(secondBackground.wait(timeout: .now() + 2) == .success, "overlapping AppDelegate completion overwritten")
        try require(normal.wait(timeout: .now() + 2) == .success, "overlapping normal completion waiter lost")
        let log = events.read()
        try require(log.firstIndex(of: "app-completion")! > log.firstIndex(of: "future-task-resumed-2")!, "completion must follow durable registration and resume")
        // AppDelegate can store its handler after session delegate reports finish.
        scheduler.didFinishBackgroundEvents(); queue.sync {}
        let late = DispatchSemaphore(value: 0)
        scheduler.registerBackgroundCompletion { events.append("late-handler"); late.signal() }
        try require(controlled.started.wait(timeout: .now() + 2) == .success, "early finish event must be remembered for late handler")
        try require(late.wait(timeout: .now() + 0.03) == .timedOut, "late handler must still cross actual scheduling barrier")
        // Exact regression: finish event -> h1 starts async drain -> h2 arrives
        // while the drain is held. Both belong to the same completed OS batch.
        let lateSecond = DispatchSemaphore(value: 0)
        scheduler.registerBackgroundCompletion { events.append("late-second-handler"); lateSecond.signal() }
        queue.sync {}
        controlled.finish(3)
        try require(controlled.started.wait(timeout: .now() + 2) == .success, "late second handler must request a fresh reconciliation")
        try require(late.wait(timeout: .now() + 0.03) == .timedOut, "first late handler must wait for the same batch rerun")
        try require(lateSecond.wait(timeout: .now() + 0.03) == .timedOut, "second late handler must wait for registered retry tasks")
        controlled.finish(4)
        try require(late.wait(timeout: .now() + 2) == .success, "late AppDelegate completion lost")
        try require(lateSecond.wait(timeout: .now() + 2) == .success, "handler registered during in-flight completion batch lost")
        print("PASS real coalescing scheduler async-pass ordering / overlap callbacks / delayed task resume before app completion / finish-before-handler race")
        print("ALL SCHEDULING CHECKS PASSED (system URLSession wake timing still requires iPhone verification)")
    }
}
