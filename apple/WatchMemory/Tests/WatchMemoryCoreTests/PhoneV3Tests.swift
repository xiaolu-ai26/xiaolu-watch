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

    func testReadingMetadataIsOptionalAndCannotInventMeasuredTimeOrHideUnknown() throws {
        let card: [String: Any] = ["id": "public-reading", "version": 1, "title": "A brief idea", "summary": "Try walking.",
            "start_at": "2026-01-01T10:00:00Z", "end_at": "2026-01-01T10:01:00Z", "owner_label": "unknown", "clean": "Try walking.", "distilled": "## Walking\nTry walking."]
        var object: [String: Any] = ["version": 3, "date": "2026-01-01", "timezone": "Asia/Taipei", "generated_at": "2026-01-01T11:00:00Z", "revision": 1,
            "overview": ["state": "ready", "text": "A brief idea."], "body": ["state": "unknown", "lines": [], "sample_count": 0], "coverage": [], "cards": [card]]
        func decode() throws -> PhoneToday { try PhoneToday.decode(JSONSerialization.data(withJSONObject: object), requestedDay: "2026-01-01") }
        let legacy = try decode()
        XCTAssertNil(legacy.body.measuredThroughAt)
        XCTAssertTrue(legacy.cards[0].isMainReading)
        var rich = card; rich["reading_role"] = "unknown"; rich["summary_state"] = "pending"
        object["cards"] = [rich]
        XCTAssertTrue(try decode().cards[0].isMainReading)
        rich["reading_role"] = "background"; object["cards"] = [rich]
        XCTAssertFalse(try decode().cards[0].isMainReading)
        rich["reading_role"] = "exclude_unknown_owner"; object["cards"] = [rich]
        XCTAssertThrowsError(try decode())
        object["cards"] = [card]
        object["body"] = ["state": "unknown", "lines": [], "sample_count": 0, "measured_through_at": "not-a-measurement"]
        XCTAssertThrowsError(try decode())
        object["body"] = ["state": "unknown", "lines": [], "sample_count": 0]
        object["overview"] = ["state": "ready", "text": "A brief idea.", "is_partial": true,
            "coverage_start_at": "2026-01-01T11:00:00Z", "coverage_end_at": "2026-01-01T10:00:00Z"]
        XCTAssertThrowsError(try decode())
    }

    func testReadingOrderUsesEventEndAndHourJumpRemainsNearest() throws {
        func card(_ id: String, _ start: String, _ end: String) -> [String: Any] {
            ["id": id, "version": 1, "title": "Public fixture", "summary": "A public idea.",
             "start_at": start, "end_at": end, "owner_label": "unknown", "clean": "Public text.", "distilled": "Public text."]
        }
        let object: [String: Any] = ["version": 3, "date": "2026-01-01", "timezone": "UTC",
            "generated_at": "2026-01-01T20:00:00Z", "revision": 1,
            "overview": ["state": "ready", "text": "Public overview."],
            "body": ["state": "unknown", "lines": [], "sample_count": 0], "coverage": [],
            "cards": [card("old", "2026-01-01T09:00:00Z", "2026-01-01T09:05:00Z"),
                      card("new", "2026-01-01T12:00:00Z", "2026-01-01T12:05:00Z"),
                      card("middle", "2026-01-01T10:00:00Z", "2026-01-01T10:05:00Z")]]
        let today = try PhoneToday.decode(JSONSerialization.data(withJSONObject: object), requestedDay: "2026-01-01")
        XCTAssertEqual(today.cardsNewestFirst.map(\.id), ["new", "middle", "old"])
        XCTAssertEqual(today.card(atOrAfter: PhoneV3Policy.date("2026-01-01T09:30:00Z")!)?.id, "middle")
        XCTAssertEqual(today.card(atOrAfter: PhoneV3Policy.date("2026-01-01T12:03:00Z")!)?.id, "new")
        XCTAssertNil(today.card(atOrAfter: PhoneV3Policy.date("2026-01-01T12:06:00Z")!))
        XCTAssertEqual(today.card(atOrAfter: PhoneV3Policy.date("2026-01-01T09:30:00Z")!, excluding: "middle")?.id, "new")
    }

    func testIngestionProgressIsOptionalAndValidatesStageFields() throws {
        var object: [String: Any] = ["version": 3, "date": "2026-01-01", "timezone": "UTC",
            "generated_at": "2026-01-01T20:00:00Z", "revision": 1,
            "overview": ["state": "ready", "text": "Public overview."],
            "body": ["state": "unknown", "lines": [], "sample_count": 0], "coverage": [], "cards": []]
        func decode() throws -> PhoneToday { try PhoneToday.decode(JSONSerialization.data(withJSONObject: object), requestedDay: "2026-01-01") }
        XCTAssertNil(try decode().ingestion)
        var progress: [String: Any] = ["latest_recorded_at": "2026-01-01T18:07:00Z", "latest_transcribed_at": "2026-01-01T18:06:00Z",
            "latest_organized_at": "2026-01-01T15:09:00Z", "received_count": 10, "transcribed_count": 9,
            "queued_text_count": 3, "queued_asr_count": 1, "state": "budget_wait"]
        object["ingestion"] = progress
        XCTAssertEqual(try decode().ingestion?.latestRecordedAt, "2026-01-01T18:07:00Z")
        XCTAssertEqual(try decode().ingestion?.queuedTextCount, 3)
        progress["queued_text_count"] = -1; object["ingestion"] = progress; XCTAssertThrowsError(try decode())
        progress["queued_text_count"] = 3; progress["latest_recorded_at"] = "unknown"; object["ingestion"] = progress
        XCTAssertThrowsError(try decode())
    }

}
