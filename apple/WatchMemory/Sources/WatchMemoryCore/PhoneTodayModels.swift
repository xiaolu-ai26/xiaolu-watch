import Foundation

public struct PhoneToday: Codable, Sendable {
    public let version: Int; public let date: String; public let timezone: String
    public let generatedAt: String; public let revision: Int
    public let overview: Overview; public let body: BodyContext
    public let coverage: [Coverage]; public let cards: [Card]
    public let ingestion: Ingestion?
    public struct Ingestion: Codable, Sendable {
        public let latestRecordedAt: String?; public let latestTranscribedAt: String?; public let latestOrganizedAt: String?
        public let receivedCount: Int; public let transcribedCount: Int
        public let queuedTextCount: Int; public let queuedAsrCount: Int
        public let state: String
    }
    public struct Overview: Codable, Sendable {
        public let state: String; public let text: String
        public let coverageStartAt: String?; public let coverageEndAt: String?; public let updatedAt: String?
        public let isPartial: Bool?; public let coveredCardCount: Int?; public let pendingCardCount: Int?
    }
    public struct BodyContext: Codable, Sendable {
        public let state: String; public let lines: [String]; public let sampleCount: Int
        public let measuredThroughAt: String?
    }
    public struct Coverage: Codable, Sendable { public let startAt: String; public let endAt: String; public let state: String }
    public struct Card: Codable, Identifiable, Sendable {
        public let id: String; public let version: Int; public let title: String; public let summary: String
        public let startAt: String; public let endAt: String; public let ownerLabel: String
        public let clean: String; public let distilled: String
        public let readingRole: String?; public let summaryState: String?
        public var isMainReading: Bool { readingRole != "background" }

    }
    /// Reading order follows the recording event, never the model update time.
    public var cardsNewestFirst: [Card] {
        let dated: [(card: Card, end: Date)] = cards.map { ($0, PhoneV3Policy.date($0.endAt) ?? Date.distantPast) }
        let ordered = dated.sorted { left, right in
            if left.end == right.end { return left.card.id < right.card.id }
            return left.end > right.end
        }
        return ordered.map { $0.card }
    }
    public func card(atOrAfter date: Date, excluding excludedID: String? = nil) -> Card? {
        let eligible = cards.compactMap { card -> (Card, Date)? in
            guard card.id != excludedID, let end = PhoneV3Policy.date(card.endAt), end >= date else { return nil }
            return (card, end)
        }
        return eligible.min { $0.1 == $1.1 ? $0.0.id < $1.0.id : $0.1 < $1.1 }?.0
    }
    public static func decode(_ data: Data, requestedDay: String) throws -> Self {
        guard data.count <= 2 * 1024 * 1024 else { throw StoreError.invalidMetadata }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let v = try decoder.decode(Self.self, from: data)
        guard v.version == 3, v.date == requestedDay, PhoneV3Policy.day(v.date), TimeZone(identifier: v.timezone) != nil,
              PhoneV3Policy.date(v.generatedAt) != nil, v.revision >= 0,
              ["ready", "pending", "no_data", "disabled"].contains(v.overview.state),
              ["unknown", "within_observed_range", "deviation", "disabled"].contains(v.body.state),
              v.body.sampleCount >= 0, v.body.lines.count <= 64, v.coverage.count <= 2048, v.cards.count <= 512,
              Set(v.cards.map(\.id)).count == v.cards.count else { throw StoreError.invalidMetadata }
        let dates = [v.overview.coverageStartAt, v.overview.coverageEndAt, v.overview.updatedAt, v.body.measuredThroughAt].compactMap { $0 }
        guard dates.allSatisfy({ PhoneV3Policy.date($0) != nil }),
              v.overview.coveredCardCount.map({ $0 >= 0 }) ?? true,
              v.overview.pendingCardCount.map({ $0 >= 0 }) ?? true else { throw StoreError.invalidMetadata }
        if let a = v.overview.coverageStartAt, let b = v.overview.coverageEndAt {
            guard PhoneV3Policy.date(a)! <= PhoneV3Policy.date(b)! else { throw StoreError.invalidMetadata }
        }
        if let ingestion = v.ingestion {
            let stageDates = [ingestion.latestRecordedAt, ingestion.latestTranscribedAt, ingestion.latestOrganizedAt].compactMap { $0 }
            guard stageDates.allSatisfy({ PhoneV3Policy.date($0) != nil }),
                  ingestion.receivedCount >= 0, ingestion.transcribedCount >= 0,
                  ingestion.queuedTextCount >= 0, ingestion.queuedAsrCount >= 0,
                  ["budget_wait", "processing", "up_to_date", "empty"].contains(ingestion.state) else { throw StoreError.invalidMetadata }
        }
        for c in v.coverage {
            guard let a = PhoneV3Policy.date(c.startAt), let b = PhoneV3Policy.date(c.endAt), b >= a,
                  ["recorded", "no_speech", "pending", "unknown"].contains(c.state) else { throw StoreError.invalidMetadata }
        }
        for c in v.cards {
            guard cardID(c.id), c.version > 0, c.title.count <= 256, c.summary.count <= 4000,
                  let a = PhoneV3Policy.date(c.startAt), let b = PhoneV3Policy.date(c.endAt), b >= a,
                  ["self", "other", "unknown"].contains(c.ownerLabel),
                  c.readingRole.map({ ["main", "background", "unknown"].contains($0) }) ?? true,
                  c.summaryState.map({ ["ready", "pending", "background", "failed"].contains($0) }) ?? true else { throw StoreError.invalidMetadata }
        }
        return v
    }
    public static func cardID(_ text: String) -> Bool { text.range(of: #"^[A-Za-z0-9_-]{1,128}$"#, options: .regularExpression) != nil }
}

public struct PhoneDeletionPlan: Codable, Identifiable, Sendable {
    public var id: String { planId }
    public let version: Int; public let planId: String; public let planRevision: Int
    public let cardId: String; public let cardVersion: Int; public let expiresAt: String
    public let confirmationToken: String; public let affectedCards: [Affected]; public let audioStatus: String; public let warnings: [String]
    public struct Affected: Codable, Sendable { public let id: String; public let impact: String }
    public func validate(card: PhoneToday.Card) throws {
        guard version == 3, PhoneV3Policy.uuid(planId), planRevision > 0, cardId == card.id, cardVersion == card.version,
              let expiry = PhoneV3Policy.date(expiresAt), expiry > Date(), expiry.timeIntervalSinceNow <= 600,
              !confirmationToken.isEmpty, confirmationToken.utf8.count <= 1024,
              ["deletable", "shared_audio_pending", "no_audio"].contains(audioStatus),
              affectedCards.count <= 512, warnings.count <= 32,
              affectedCards.contains(where: { $0.id == card.id && $0.impact == "delete" }),
              affectedCards.allSatisfy({ PhoneToday.cardID($0.id) && ["delete", "retain"].contains($0.impact) }) else { throw StoreError.invalidMetadata }
    }
}
public struct PhoneDeletionStatus: Codable, Sendable {
    public let version: Int; public let deletionId: String; public let cardId: String; public let state: String
    public let audioStatus: String; public let retryable: Bool; public let warnings: [String]; public let retainedOtherCards: [String]
    public func validate(expectedID: String, cardID: String) throws {
        guard version == 3, deletionId == expectedID, self.cardId == cardID,
              ["pending", "running", "completed", "shared_audio_pending", "retryable_failed"].contains(state),
              ["deletable", "shared_audio_pending", "no_audio"].contains(audioStatus), warnings.count <= 32,
              retainedOtherCards.count <= 512 else { throw StoreError.invalidMetadata }
        guard state != "completed" || audioStatus != "shared_audio_pending" else { throw StoreError.invalidMetadata }
    }
}
