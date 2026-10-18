import XCTest
@testable import WatchMemoryCore

final class PhoneV3Tests: XCTestCase {
    private let request = "00000000-0000-4000-8000-000000000001"
    // Deterministic PUBLIC fixture, never a production credential.
    private let token = String(repeating: "A", count: 43)
    private func configuration(endpoint: String = "https://example.invalid", scopes: [String] = ["context:write", "today:read", "cards:delete"]) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["version": 3, "protocol": "watch-memory-phone-v3", "endpoint": endpoint,
            "token": token, "principal_id": request, "scopes": scopes])
    }
    func testPhonePrincipalHasSeparatePurposeAndSameOrigin() throws {
        let origin = URL(string: "https://example.invalid")!
        XCTAssertNoThrow(try PhoneV3Configuration.response(configuration(), origin: origin))
        XCTAssertThrowsError(try PhoneV3Configuration.response(configuration(endpoint: "https://different.invalid"), origin: origin))
        XCTAssertThrowsError(try PhoneV3Configuration.response(configuration(endpoint: "http://example.invalid"), origin: origin))
        XCTAssertThrowsError(try PhoneV3Configuration.response(configuration(scopes: ["audio:write"]), origin: origin))
        var object = try JSONSerialization.jsonObject(with: configuration()) as! [String: Any]
        object["protocol"] = "watch-memory-chunks-v2"
        XCTAssertThrowsError(try PhoneV3Configuration.response(JSONSerialization.data(withJSONObject: object), origin: origin))
    }
    func testContextACKMustMatchAndCannotBeNumericBoolean() throws {
        var object: [String: Any] = ["version": 3, "request_id": request, "accepted": true, "stored_at": "2026-01-01T10:00:00.123Z", "context_revision": 1]
        func check() throws { _ = try PhoneContextAcknowledgement.validate(JSONSerialization.data(withJSONObject: object), requestID: request) }
        XCTAssertNoThrow(try check())
        object["request_id"] = "00000000-0000-4000-8000-000000000002"; XCTAssertThrowsError(try check())
        object["request_id"] = request; object["accepted"] = 1; XCTAssertThrowsError(try check())
        object["accepted"] = true; object["stored_at"] = "unknown"; XCTAssertThrowsError(try check())
    }
    func testTodayFailsClosedOnDayAndCardPathOrIdentityMismatch() throws {
        var card: [String: Any] = ["id": "public-group", "version": 1, "title": "Public fixture", "summary": "A short fixture.",
            "start_at": "2026-01-01T10:00:00Z", "end_at": "2026-01-01T10:01:00Z", "owner_label": "unknown", "clean": "Full text.", "distilled": "## Topic\nFull text."]
        var object: [String: Any] = ["version": 3, "date": "2026-01-01", "timezone": "Asia/Taipei", "generated_at": "2026-01-01T11:00:00Z", "revision": 1,
            "overview": ["state": "ready", "text": "A public fixture."], "body": ["state": "unknown", "lines": [], "sample_count": 0], "coverage": [], "cards": [card]]
        func decode() throws { _ = try PhoneToday.decode(JSONSerialization.data(withJSONObject: object), requestedDay: "2026-01-01") }
        XCTAssertNoThrow(try decode())
        object["date"] = "2026-01-02"; XCTAssertThrowsError(try decode()); object["date"] = "2026-01-01"
        card["id"] = "../secret"; object["cards"] = [card]; XCTAssertThrowsError(try decode())
        card["id"] = "public-group"; card["owner_label"] = "certainly_you"; object["cards"] = [card]; XCTAssertThrowsError(try decode())
        XCTAssertFalse(PhoneV3Policy.day("2026-02-30")); XCTAssertTrue(PhoneV3Policy.day("2026-10-18"))
    }
    func testSharedAudioCannotBeReportedFullyCompleted() throws {
        let object: [String: Any] = ["version": 3, "deletion_id": request, "card_id": "public-group", "state": "completed", "audio_status": "shared_audio_pending", "retryable": true, "warnings": [], "retained_other_cards": ["another-public-group"]]
        let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase
        let result = try d.decode(PhoneDeletionStatus.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertThrowsError(try result.validate(expectedID: request, cardID: "public-group"))
    }
    func testLocationEpochRejectsCachedOrDelayedEventsAcrossOptOut() {
        let enabledEpoch = "00000000-0000-4000-8000-000000000010"
        let reenabledEpoch = "00000000-0000-4000-8000-000000000011"
        let firstEnable = Date(timeIntervalSince1970: 100)
        let reenable = Date(timeIntervalSince1970: 200)
        let now = Date(timeIntervalSince1970: 300)
        // A cached point can be very recent yet still fall inside the disabled interval.
        XCTAssertFalse(PhoneCollectionPolicy.accepts(start: Date(timeIntervalSince1970: 190), floor: reenable,
            capturedEpoch: reenabledEpoch, currentEpoch: reenabledEpoch, enabled: true, now: now))
        // An old geocoder result must not regain authority after the switch returns to on.
        XCTAssertFalse(PhoneCollectionPolicy.accepts(start: Date(timeIntervalSince1970: 210), floor: reenable,
            capturedEpoch: enabledEpoch, currentEpoch: reenabledEpoch, enabled: true, now: now))
        XCTAssertFalse(PhoneCollectionPolicy.accepts(start: Date(timeIntervalSince1970: 110), floor: firstEnable,
            capturedEpoch: enabledEpoch, currentEpoch: enabledEpoch, enabled: false, now: now))
        XCTAssertTrue(PhoneCollectionPolicy.accepts(start: Date(timeIntervalSince1970: 210), floor: reenable,
            capturedEpoch: reenabledEpoch, currentEpoch: reenabledEpoch, enabled: true, now: now))
        XCTAssertFalse(PhoneCollectionPolicy.accepts(start: Date(timeIntervalSince1970: 310), floor: reenable,
            capturedEpoch: reenabledEpoch, currentEpoch: reenabledEpoch, enabled: true, now: now))
    }

}
