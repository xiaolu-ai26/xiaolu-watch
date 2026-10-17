import WatchKit
import Foundation

final class WatchApplicationDelegate: NSObject, WKApplicationDelegate {
    func applicationDidFinishLaunching() { DispatchQueue.main.async { WatchPairingSetup.shared.resumeAutomatically() } }
    func handle(_ backgroundTasks: Set<WKRefreshBackgroundTask>) {
        for task in backgroundTasks {
            if let transfer = task as? WKURLSessionRefreshBackgroundTask { WatchDirectUploader.shared.handle(transfer) }
            else if let snapshot = task as? WKSnapshotRefreshBackgroundTask {
                snapshot.setTaskCompleted(restoredDefaultState: true, estimatedSnapshotExpiration: .distantFuture, userInfo: nil)
            }
            else { WatchDirectUploader.shared.scheduleUploads { task.setTaskCompletedWithSnapshot(false) } }
        }
    }
}
