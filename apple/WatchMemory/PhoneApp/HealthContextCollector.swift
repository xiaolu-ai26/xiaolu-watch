import Foundation
import HealthKit
import UIKit
import WatchMemoryCore

struct PhoneHealthSample: Codable {
    let id: String; let kind: String; let startAt: String; let endAt: String
    let value: Double; let unit: String; let category: Int?; let activity: String?; let source: String; let revision: Int; let motionContext: String?
}

/// Only HealthKit's stored samples. No workout, generated heart-rate sample or read-denial inference.
final class HealthContextCollector {
    private let store = HKHealthStore()
    private let queue = DispatchQueue(label: "xiaolu.phone.health", qos: .utility)
    private var observers: [HKObserverQuery] = []
    private var active = Set<String>()
    private var completions: [String: [() -> Void]] = [:]
    private let defaults = UserDefaults.standard
    var receive: (([PhoneHealthSample], [String], String, Data) throws -> Void)?
    var hasPendingCheckpoint: ((String) -> Bool)?
    var status: ((String) -> Void)?
    var enabled: Bool { defaults.bool(forKey: "phoneHealthContextEnabled") }
    private var types: [(String, HKSampleType)] {
        let value: [(String, HKSampleType)] = [
            ("heart_rate", HKObjectType.quantityType(forIdentifier: .heartRate)!),
            ("resting_heart_rate", HKObjectType.quantityType(forIdentifier: .restingHeartRate)!),
            ("hrv_sdnn", HKObjectType.quantityType(forIdentifier: .heartRateVariabilitySDNN)!),
            ("steps", HKObjectType.quantityType(forIdentifier: .stepCount)!),
            ("sleep", HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!),
            ("workout", HKObjectType.workoutType())]
        // SDK availability is not evidence of S9 measurements. Probe type availability only.
        if #available(iOS 27.0, *) { _ = HKObjectType.quantityType(forIdentifier: .heartRateVariabilityRMSSD) }
        return value
    }
    func requestEnable() {
        guard HKHealthStore.isHealthDataAvailable() else { report("此设备暂不支持健康资料读取"); return }
        store.requestAuthorization(toShare: [], read: Set(types.map { $0.1 })) { [weak self] success, _ in
            guard let self else { return }
            guard success else { self.report("健康授权流程未完成，可在设置中检查"); return }
            // Completion is NOT permission to every read type. Empty results remain ambiguous.
            let first = !self.defaults.bool(forKey: "phoneHealthContextEverEnabled")
            self.defaults.set(true, forKey: "phoneHealthContextEnabled")
            self.defaults.set(true, forKey: "phoneHealthContextEverEnabled")
            self.defaults.set(Date().addingTimeInterval(first ? -30 * 86400 : 0), forKey: "phoneHealthCollectionFloor")
            self.start()
        }
    }
    func disable() {
        defaults.set(false, forKey: "phoneHealthContextEnabled")
        queue.async {
            self.observers.forEach { self.store.stop($0) }; self.observers.removeAll()
            for (_, type) in self.types { self.store.disableBackgroundDelivery(for: type) { _, _ in } }
            self.report("身体背景已关闭；已有副本需单独删除")
        }
    }
    func start() {
        guard enabled, HKHealthStore.isHealthDataAvailable() else { return }
        queue.async { [self] in
            guard self.observers.isEmpty else {
                for (kind, type) in self.types { self.drain(kind, type, completion: {}) }; return
            }
            self.report("等待可用健康记录；无结果不等于未授权")
            for (kind, type) in self.types {
                let observer = HKObserverQuery(sampleType: type, predicate: nil) { [weak self] _, completion, error in
                    guard let self else { completion(); return }
                    if error != nil { self.report("健康记录暂不可读取，可稍后查看授权与设备状态"); completion(); return }
                    self.drain(kind, type, completion: completion)
                }
                self.observers.append(observer); self.store.execute(observer)
                self.store.enableBackgroundDelivery(for: type, frequency: .hourly) { [weak self] success, _ in
                    if !success { self?.report("后台健康交付暂不可用；下次打开时继续读取") }
                }
                self.drain(kind, type, completion: {})
            }
        }
    }
    private func drain(_ kind: String, _ type: HKSampleType, completion: @escaping () -> Void) {
        queue.async {
            self.completions[kind, default: []].append(completion)
            guard !self.active.contains(kind) else { return }
            self.active.insert(kind); self.query(kind, type)
        }
    }
    private func anchorURL(_ kind: String) throws -> URL {
        let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("WatchMemory/phone-health-anchors", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: root.path)
        var u = root; var resource = URLResourceValues(); resource.isExcludedFromBackup = true; try u.setResourceValues(resource)
        return root.appendingPathComponent(kind + ".anchor")
    }
    private func query(_ kind: String, _ type: HKSampleType) {
        guard enabled else { finish(kind); return }
        if hasPendingCheckpoint?(kind) == true { finish(kind); return }
        var anchor: HKQueryAnchor?
        do {
            let path = try anchorURL(kind)
            if FileManager.default.fileExists(atPath: path.path) {
                let data = try Data(contentsOf: path); guard data.count <= 1024 * 1024 else { throw StoreError.invalidMetadata }
                anchor = try NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
            }
        } catch { report("健康增量位置暂不可读取，未跳过记录"); finish(kind); return }
        let predicateFloor = self.defaults.object(forKey: "phoneHealthPredicateFloor") as? Date ?? Date().addingTimeInterval(-30 * 86400)
        self.defaults.set(predicateFloor, forKey: "phoneHealthPredicateFloor")
        let predicate = HKQuery.predicateForSamples(withStart: predicateFloor, end: nil)
        let query = HKAnchoredObjectQuery(type: type, predicate: predicate, anchor: anchor, limit: 200) { [weak self] _, samples, deleted, newAnchor, error in
            guard let self else { return }
            self.queue.async {
                guard error == nil, let newAnchor, self.enabled else {
                    self.report("尚无可用健康资料，或设备暂时锁定；可检查授权"); self.finish(kind); return
                }
                do {
                    let floor = self.defaults.object(forKey: "phoneHealthCollectionFloor") as? Date ?? Date()
                    let records = (samples ?? []).filter { $0.startDate >= floor }.compactMap { self.record($0, kind: kind) }
                    let ids = (deleted ?? []).map { $0.uuid.uuidString.lowercased() }
                    let data = try NSKeyedArchiver.archivedData(withRootObject: newAnchor, requiringSecureCoding: true)
                    if !records.isEmpty || !ids.isEmpty {
                        guard let receiver = self.receive else { throw StoreError.invalidMetadata }
                        try receiver(records, ids, kind, data) // Persist checkpoint with the request; commit only after matching ACK.
                    } else {
                        try data.write(to: self.anchorURL(kind), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                    }
                    self.report(records.isEmpty ? "尚无新的可用健康记录；可能无样本或尚未同步" : "已保存健康增量，等待处理电脑确认")
                    if records.isEmpty && ids.isEmpty && ((samples?.count ?? 0) >= 200 || (deleted?.count ?? 0) >= 200) { self.query(kind, type) }
                    else { self.finish(kind) }
                } catch { self.report("健康增量未保存，已保留原读取位置待重试"); self.finish(kind) }
            }
        }
        store.execute(query)
    }
    func commitCheckpoint(kind: String, data: Data, completion: @escaping (Error?) -> Void) {
        queue.async {
            do {
                guard self.types.contains(where: { $0.0 == kind }), data.count <= 1024 * 1024,
                      try NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data) != nil else { throw StoreError.invalidMetadata }
                try data.write(to: self.anchorURL(kind), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                completion(nil)
            } catch { completion(error) }
        }
    }
    func resumeAfterAcknowledgement(_ kind: String) {
        guard enabled, let type = types.first(where: { $0.0 == kind })?.1 else { return }
        drain(kind, type, completion: {})
    }
    private func finish(_ kind: String) {
        active.remove(kind); let callbacks = completions.removeValue(forKey: kind) ?? []; callbacks.forEach { $0() }
    }
    private func report(_ value: String) { DispatchQueue.main.async { self.status?(value) } }
    private func record(_ sample: HKSample, kind: String) -> PhoneHealthSample? {
        var value: Double; var unit: String; var category: Int?; var activity: String?
        if let q = sample as? HKQuantitySample {
            switch kind {
            case "heart_rate", "resting_heart_rate": value = q.quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute())); unit = "count/min"
            case "hrv_sdnn", "hrv_rmssd": value = q.quantity.doubleValue(for: .secondUnit(with: .milli)); unit = "ms"
            case "steps": value = q.quantity.doubleValue(for: .count()); unit = "count"
            default: return nil
            }
        } else if let c = sample as? HKCategorySample { value = c.endDate.timeIntervalSince(c.startDate) / 60; category = c.value; unit = "min" }
        else if let w = sample as? HKWorkout { value = w.endDate.timeIntervalSince(w.startDate) / 60; activity = String(w.workoutActivityType.rawValue); unit = "min" }
        else { return nil }
        guard value.isFinite, sample.endDate >= sample.startDate else { return nil }
        var motion: String?
        if kind == "heart_rate", let raw = sample.metadata?[HKMetadataKeyHeartRateMotionContext] as? NSNumber,
           let context = HKHeartRateMotionContext(rawValue: raw.intValue) {
            if context == .sedentary { motion = "sedentary" }
            else if context == .active { motion = "active" }
        }
        return PhoneHealthSample(id: sample.uuid.uuidString.lowercased(), kind: kind,
            startAt: PhoneV3Policy.timestamp(sample.startDate), endAt: PhoneV3Policy.timestamp(sample.endDate),
            value: value, unit: unit, category: category, activity: activity, source: sample.sourceRevision.source.bundleIdentifier, revision: 1, motionContext: motion)
    }
}
