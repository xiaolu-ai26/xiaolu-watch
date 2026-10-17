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
    public mutating func activationSucceeded() { interruptionActive = false; manualResumeRequired = false }
    public mutating func activationFailed() { manualResumeRequired = wanted }
}
