import Foundation
import CoreLocation
import WatchMemoryCore

struct PhonePlaceSample: Codable {
    let id: String; let kind: String; let startAt: String; let endAt: String
    let latitude: Double; let longitude: Double; let horizontalAccuracyM: Double; let alias: String?
}

/// Opt-in, coarse phone events. No continuous route and no Watch-only API assumptions.
final class PlaceContextCollector: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let defaults = UserDefaults.standard
    private let geocoder = CLGeocoder()
    private var geocodingRequestID: UUID?
    private var aliases: [String: String] = [:]
    var receive: (([PhonePlaceSample]) throws -> Void)?
    var status: ((String) -> Void)?
    var enabled: Bool { defaults.bool(forKey: "phonePlaceContextEnabled") }
    private var epoch: String { defaults.string(forKey: "phonePlaceCollectionEpoch") ?? "" }
    private var floor: Date { defaults.object(forKey: "phonePlaceCollectionFloor") as? Date ?? .distantFuture }
    private func accepts(_ start: Date, epoch captured: String) -> Bool {
        PhoneCollectionPolicy.accepts(start: start, floor: floor, capturedEpoch: captured, currentEpoch: epoch, enabled: enabled, now: Date())
    }
    override init() {
        super.init(); manager.delegate = self; manager.desiredAccuracy = kCLLocationAccuracyThreeKilometers
        manager.distanceFilter = 1000; manager.pausesLocationUpdatesAutomatically = true
    }
    func requestEnable() {
        defaults.set(UUID().uuidString.lowercased(), forKey: "phonePlaceCollectionEpoch")
        defaults.set(true, forKey: "phonePlaceContextEnabled")
        defaults.set(Date(), forKey: "phonePlaceCollectionFloor")
        if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
        else { resume() }
    }
    func requestBackgroundPermission() {
        guard enabled, manager.authorizationStatus == .authorizedWhenInUse else { return }
        manager.requestAlwaysAuthorization() // Only from an explicit settings button.
    }
    func disable() {
        defaults.set(UUID().uuidString.lowercased(), forKey: "phonePlaceCollectionEpoch")
        defaults.set(false, forKey: "phonePlaceContextEnabled")
        geocodingRequestID = nil; aliases.removeAll()
        manager.stopMonitoringVisits(); manager.stopMonitoringSignificantLocationChanges(); manager.stopUpdatingLocation()
        manager.allowsBackgroundLocationUpdates = false; geocoder.cancelGeocode()
        status?("地点背景已关闭；已有副本需单独删除")
    }
    func resume() {
        guard enabled else { return }
        switch manager.authorizationStatus {
        case .authorizedAlways:
            manager.allowsBackgroundLocationUpdates = true
            manager.startMonitoringVisits()
            if CLLocationManager.significantLocationChangeMonitoringAvailable() { manager.startMonitoringSignificantLocationChanges() }
            status?("已启用大致停留事件；后台更新可能延迟")
        case .authorizedWhenInUse:
            manager.requestLocation(); status?("已允许使用期间定位；后台停留需始终权限")
        case .denied, .restricted:
            manager.stopMonitoringVisits(); manager.stopMonitoringSignificantLocationChanges()
            status?("系统地点权限已关闭，可在设置中检查")
        case .notDetermined: status?("地点权限尚未选择")
        @unknown default: status?("地点权限状态暂不可用")
        }
    }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) { if enabled { resume() } }
    func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        guard enabled, visit.arrivalDate != .distantPast, visit.arrivalDate <= Date(), visit.horizontalAccuracy >= 0 else { return }
        let capturedEpoch = epoch
        guard accepts(visit.arrivalDate, epoch: capturedEpoch) else { return }
        let arrival = PhoneV3Policy.timestamp(visit.arrivalDate)
        let key = "visit:" + arrival
        var identifiers = defaults.dictionary(forKey: "phoneRecentVisitIDs") as? [String: String] ?? [:]
        let id = identifiers[key] ?? UUID().uuidString.lowercased(); identifiers[key] = id
        if identifiers.count > 32 { for old in identifiers.keys.sorted().dropLast(32) { identifiers.removeValue(forKey: old) } }
        defaults.set(identifiers, forKey: "phoneRecentVisitIDs")
        let end = visit.departureDate == .distantFuture ? arrival : PhoneV3Policy.timestamp(visit.departureDate)
        emit(id: id, kind: visit.departureDate == .distantFuture ? "sample" : "visit", start: visit.arrivalDate, end: end, coordinate: visit.coordinate, accuracy: visit.horizontalAccuracy, capturedEpoch: capturedEpoch)
    }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let capturedEpoch = epoch
        guard enabled, let value = locations.last, value.horizontalAccuracy >= 0,
              accepts(value.timestamp, epoch: capturedEpoch), abs(value.timestamp.timeIntervalSinceNow) < 86400 else { return }
        emit(id: UUID().uuidString.lowercased(), kind: "coarse", start: value.timestamp,
             end: PhoneV3Policy.timestamp(value.timestamp), coordinate: value.coordinate, accuracy: value.horizontalAccuracy, capturedEpoch: capturedEpoch)
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if (error as? CLError)?.code == .denied { status?("系统地点权限已关闭，可在设置中检查") }
        else { status?("暂未取得地点事件，不补造轨迹") }
    }
    private func emit(id: String, kind: String, start: Date, end: String, coordinate: CLLocationCoordinate2D, accuracy: Double, capturedEpoch: String) {
        guard accepts(start, epoch: capturedEpoch), CLLocationCoordinate2DIsValid(coordinate), accuracy.isFinite, accuracy <= 99000 else { return }
        let coarseLat = (coordinate.latitude * 100).rounded() / 100
        let coarseLon = (coordinate.longitude * 100).rounded() / 100
        let key = String(coarseLat) + ":" + String(coarseLon)
        let alias = aliases[key]
        let value = PhonePlaceSample(id: id, kind: kind, startAt: PhoneV3Policy.timestamp(start), endAt: end,
            latitude: coarseLat, longitude: coarseLon, horizontalAccuracyM: max(1000, accuracy + 1000), alias: alias)
        do { guard let receive else { throw StoreError.invalidState }; try receive([value]); status?(alias == nil ? "大致区域已保存，地点待确认" : "大致地点事件已保存，等待处理电脑确认") }
        catch { status?("地点事件暂未保存，原有录音不受影响"); return }
        guard alias == nil, geocodingRequestID == nil, accepts(start, epoch: capturedEpoch) else { return }
        let requestID = UUID(); geocodingRequestID = requestID
        // Only coarse coordinates go to Apple's system geocoding service. Never request house/street names.
        geocoder.reverseGeocodeLocation(CLLocation(latitude: coarseLat, longitude: coarseLon)) { [weak self] places, _ in
            DispatchQueue.main.async {
            guard let self, self.geocodingRequestID == requestID else { return }
            self.geocodingRequestID = nil
            guard self.accepts(start, epoch: capturedEpoch), let place = places?.first else { return }
            var pieces: [String] = []
            for item in [place.locality ?? place.administrativeArea, place.subLocality].compactMap({ $0 }) {
                if !item.isEmpty && !pieces.contains(item) { pieces.append(item) }
            }
            guard !pieces.isEmpty else { return }
            let label = String(pieces.joined(separator: " · ").prefix(80))
            if self.aliases.count >= 64 { self.aliases.removeAll() }; self.aliases[key] = label
            let updated = PhonePlaceSample(id: id, kind: kind, startAt: value.startAt, endAt: value.endAt,
                latitude: coarseLat, longitude: coarseLon, horizontalAccuracyM: value.horizontalAccuracyM, alias: label)
            do { guard self.accepts(start, epoch: capturedEpoch), let receive = self.receive else { throw StoreError.invalidState }; try receive([updated]); self.status?("城市／街区背景已保存") }
            catch { self.status?("区域已保存，地点名称等待下次更新") }
            }
        }
    }
}
