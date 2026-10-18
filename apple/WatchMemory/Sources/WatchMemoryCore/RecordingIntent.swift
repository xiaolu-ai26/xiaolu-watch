import Foundation

/// User intent and interruption arbitration, separate from physical engine state.
/// A cold process always starts idle; a diagnostic checkpoint never grants mic intent.
public struct RecordingIntent: Sendable {
    public var wanted = false
    public var userPaused = false
    public private(set) var interruptionActive = false
    public private(set) var manualResumeRequired = false
    public init() {}
    public mutating func start() { wanted = true; userPaused = false; manualResumeRequired = false }
    public mutating func stop() { wanted = false; userPaused = false; manualResumeRequired = false }
    public mutating func pause() { userPaused = true; manualResumeRequired = false }
    public mutating func requestManualResume() { userPaused = false; manualResumeRequired = true }
    public mutating func interruptionBegan() { interruptionActive = true }
    public mutating func interruptionEnded(shouldResume: Bool, engineRunning: Bool) -> Bool {
        interruptionActive = false
        guard wanted, !userPaused, !engineRunning else { return false }
        manualResumeRequired = !shouldResume
        return shouldResume
    }
    public var canRestartAutomatically: Bool { wanted && !userPaused && !interruptionActive && !manualResumeRequired }
    public func mayActivate(manual: Bool) -> Bool { wanted && !userPaused && (manual || canRestartAutomatically) }
    /// A bounded, OS-authorized recovery episode may retry a failed activation.
    public mutating func authorizeSystemRetry() -> Bool {
        guard wanted, !userPaused, !interruptionActive else { return false }
        manualResumeRequired = false
        return true
    }
    public mutating func activationSucceeded() { interruptionActive = false; manualResumeRequired = false }
    public mutating func activationFailed() { manualResumeRequired = wanted }
}

/// A process-local grant issued only after an OS shouldResume recommendation.
/// Each ticket is consumed once; cancellation makes already queued callbacks inert.
public struct RecordingRecovery: Sendable {
    public struct Ticket: Equatable, Sendable {
        public let episode: UUID
        public let session: UUID
        public let attempt: Int
        public let delay: Double
    }
    private var episode: UUID?
    private var session: UUID?
    private var attempts = 0
    public init() {}
    public mutating func begin(session: UUID) { episode = UUID(); self.session = session; attempts = 0 }
    public mutating func cancel() { episode = nil; session = nil; attempts = 0 }
    public mutating func next(domain: String, code: Int) -> Ticket? {
        guard domain == "com.apple.coreaudio.avfaudio", code == 2003329396,
              let episode, let session, attempts < 3 else { cancel(); return nil }
        let delay = [0.5, 1.5, 3.0][attempts]; attempts += 1
        return Ticket(episode: episode, session: session, attempt: attempts, delay: delay)
    }
    public func owns(_ ticket: Ticket) -> Bool { ticket.episode == episode && ticket.attempt == attempts }
    public func permits(_ ticket: Ticket, session: UUID, intent: RecordingIntent,
                        engineRunning: Bool, batteryLevel: Float, permissionGranted: Bool) -> Bool {
        ticket.episode == episode && ticket.session == self.session && session == self.session &&
        ticket.attempt == attempts && intent.wanted && !intent.userPaused && !intent.interruptionActive &&
        !engineRunning && batteryLevel >= 0.05 && permissionGranted
    }
}

public enum RecordingProtectionStop {
    public static func finalized(saved: Bool, chunks: [Chunk], session: UUID) -> Bool {
        saved && chunks.filter { $0.sessionID == session }.allSatisfy { $0.isFinalized }
    }
    public static func checkpoint(saved: Bool, reason: String) -> (clean: Bool, event: String) {
        (saved, saved ? reason + "_complete" : reason + "_needs_recovery")
    }
}
