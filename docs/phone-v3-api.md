# Phone v3 contract (version 3)

All routes use the same configured HTTPS origin, reject redirects, and send `Cache-Control: no-store`. Audio v2 is unchanged. Phone token is an independent 32-byte base64url credential (43 chars), sent only as `Authorization: Bearer <token>`, never URL/query/log. Fixed role `phone`; scopes `context:write`, `today:read`, `cards:delete`. Phone bootstrap has independent secret/purpose, ledger and rate limits, not the audio key.

Dates are ISO8601 with explicit UTC offset (fractional seconds allowed). `request_id`, `attempt_id`, `plan_id`, `deletion_id`, `principal_id` are canonical lower-case UUIDs. POST idempotency binds principal + request_id + exact canonical body; changed payload is 409. Errors: `{ "version":3, "error":"code", "message":"safe short message" }`; never include health, coordinates, paths or tokens in errors.

## Bootstrap

`POST /v3/phone/pair/claim` (no bearer), <=1 KiB:

```json
{"code":"16BASE32CODEHERE!","attempt_id":"00000000-0000-4000-8000-000000000001"}
```

Code is exactly 16 RFC4648 base32 characters (80 bits; example above is a placeholder, not a valid code). Server-only issuance, 600s TTL, hash-only code, atomically binds first attempt_id. Same attempt may retry within TTL; different attempt denied. Global/socket-peer rate limits; do not trust X-Forwarded-For. 401 does not distinguish missing/expired/wrong/claimed code. Response after successful verification:

```json
{"version":3,"protocol":"watch-memory-phone-v3","endpoint":"https://example.invalid","token":"<43-base64url>","principal_id":"00000000-0000-4000-8000-000000000002","scopes":["context:write","today:read","cards:delete"]}
```

Installer-only onboarding JSON stays `{endpoint,code}`; device attempt_id generated/persisted in independent Phone Keychain. Token never embedded in binary or returned by any other public route.

## Durable context upload

`POST /v3/phone/context`, scope context:write, <=256 KiB, at most 512 health rows/128 places per request:

```json
{
 "version":3,"request_id":"00000000-0000-4000-8000-000000000003",
 "observed_at":"2026-01-01T12:00:00Z","timezone":"Asia/Taipei",
 "function_flags":{"health_enabled":true,"location_enabled":true,"share_derived_with_ai":false,"control_revision":1},
 "collection_state":{"health":"no_data","location":"available"},
 "health":[{"id":"00000000-0000-4000-8000-000000000004","source":"com.example.health","revision":1,"kind":"heart_rate","start_at":"2026-01-01T11:59:00Z","end_at":"2026-01-01T11:59:10Z","value":70,"unit":"count/min"}],
 "health_deleted":[],
 "places":[{"id":"00000000-0000-4000-8000-000000000005","start_at":"2026-01-01T11:50:00Z","end_at":"2026-01-01T12:00:00Z","latitude":25.0,"longitude":121.0,"horizontal_accuracy_m":50,"kind":"visit"}]
}
```

Health canonical kinds/units: heart_rate and resting_heart_rate `count/min`; hrv_sdnn `ms`; steps `count`; sleep/workout `min`. Sleep/workout `value` is actual interval duration in minutes; sleep may add `category` integer (actual HealthKit stage); workout may add `activity` string. Never rename SDNN to RMSSD. Backend preserves source/revision and exposes local canonical rows with `deleted` boolean. `health_deleted` is HealthKit UUID string array; local tombstones prevent replay resurrection. This API does not delete HealthKit records.

Function flags carry a persistent per-principal `control_revision` (nonnegative integer), and only a strictly newer revision updates flags/collection state. Older queued uploads cannot re-enable disabled collection. When current flags disable a domain, late data in that domain is acknowledged as ignored rather than stored; deletions still apply. Equal flag revisions must have identical flag values. Optional health `motion_context` is sedentary/active/unknown and must come from actual sample metadata; missing steps never proves rest.

Optional place `alias` is a bounded (128 character) coarse locality label, stored locally only. It is not a free-text address or AI input.

Health collection state: not_queried/available/no_data/unsupported/error; no-data does not assert read permission denied. Location: not_queried/available/no_data/denied/restricted/unsupported/error. All numeric values finite. Exact health/coordinates only in restricted local storage; never returned in today JSON or sent to AI. Function flags are independent from data presence.

ACK only after durable commit:

```json
{"version":3,"request_id":"00000000-0000-4000-8000-000000000003","accepted":true,"stored_at":"2026-01-01T12:00:01Z","context_revision":1}
```

Native clears queued payload/advances the matching local anchor only after validated request_id/accepted ACK. Same request is retry-safe.

## Today read

`GET /v3/phone/today?date=YYYY-MM-DD`, scope today:read. Only derived projection; not arbitrary file reads:

```json
{"version":3,"date":"2026-01-01","timezone":"Asia/Taipei","generated_at":"2026-01-01T12:00:00Z","revision":1,"overview":{"state":"no_data","text":""},"body":{"state":"unknown","lines":[],"sample_count":0},"coverage":[],"cards":[{"id":"group-stable-id","version":1,"title":"Conversation","summary":"","start_at":"2026-01-01T11:00:00Z","end_at":"2026-01-01T11:05:00Z","owner_label":"unknown","clean":"","distilled":""}]}
```

`overview.state`: ready/pending/no_data/disabled; `body.state`: unknown/within_observed_range/deviation/disabled. Coverage rows `{start_at,end_at,state}`, state recorded/no_speech/pending/unknown. owner_label self/other/unknown. Card ID is opaque `[A-Za-z0-9_-]{1,128}`, stable across batch continuation; version positive integer changes with content/source boundaries. Summary/clean/distilled are user-readable text. No audio paths, raw health/coordinates, embeddings, credentials or machine logs.

## Card deletion

`POST /v3/phone/cards/{card_id}/deletion-plan` scope cards:delete:

```json
{"request_id":"00000000-0000-4000-8000-000000000006","expected_card_version":1}
```

Response:

```json
{"version":3,"plan_id":"00000000-0000-4000-8000-000000000007","plan_revision":1,"card_id":"group-stable-id","card_version":1,"expires_at":"2026-01-01T12:05:00Z","confirmation_token":"<opaque>","affected_cards":[{"id":"group-stable-id","impact":"delete"}],"audio_status":"shared_audio_pending","warnings":["Shared audio boundaries require confirmation; other conversations are retained."]}
```

Audio status: deletable/shared_audio_pending/no_audio. Planning is read-only and binds complete relevant card/source snapshot. Only actual word-aligned time intervals may permit shared-source slicing; coarse parent spans, unknown or overlapping boundaries return shared_audio_pending. No character interpolation or LLM timestamps. Never unlink a shared minute source directly.

`POST /v3/phone/deletions/{plan_id}/commit`:

```json
{"request_id":"00000000-0000-4000-8000-000000000008","plan_revision":1,"confirmation_token":"<echo plan token>"}
```

Response and `GET /v3/phone/deletions/{deletion_id}`:

```json
{"version":3,"deletion_id":"00000000-0000-4000-8000-000000000007","card_id":"group-stable-id","state":"running","audio_status":"shared_audio_pending","retryable":true,"warnings":[],"retained_other_cards":[]}
```

State pending/running/completed/shared_audio_pending/retryable_failed. Idempotent retries reuse plan/transaction; stale snapshot/version gives 409, expired uncommitted plan 410. Commit durably tombstones card generation and affected original source IDs before asynchronous work. Completed means audio and all derivative/notes/cloud stages confirmed, not just hidden in UI. Pending shared audio is explicit partial completion; never report all audio deleted.

Original source SHA and historical recording ACK receipts are immutable. Residual audio gets new content-addressed asset IDs plus original-to-residual interval mapping; only after retained assets decode/hash validation can original be moved to protected recoverable trash. Replayed retired original chunks must not restore audio or publications. Card deletion does not delete shared HealthKit or location records.

## Local processor interface (not public/admin HTTP)

Processor writes trusted `reading_root/YYYY-MM-DD.json` with the public today shape above and a private sidecar `reading_root/YYYY-MM-DD.sources.json`:

```json
{"version":1,"revision":1,"cards":[{"id":"group-stable-id","version":1,"group_id":"group-stable-id","generation":1,"source_spans":[{"source_id":"<64hex>","start":1.2,"end":4.5,"precision":"asr_word_span","overlap":false}]}]}
```

Source intervals use original source audio seconds; precision `asr_word_span` or `parent_span`/`unknown`. Backend independently verifies source inventory and protects union of all other cards/unknown complement. Processor must not accept HTTP-supplied paths as artifact paths.

Deletion store Python reader: `context_store.ContextStore(root).retirement_snapshot()` -> `{generation:int,group_ids:[str],retired_group_ids:[str],retired_raw_source_ids:[64hex],source_ids:[],transactions:[...]}`. Generations are monotonic; processor reads before generation, excludes retired groups from all output. Retired raw sources block only new discovery/replay; existing other cards sharing that original source remain visible through retained mappings. The processor holds `generation_guard(expected)` across cloud/manifest/projection commits. `transaction_id` stage completion is persisted by local `report_derivative_stage(transaction_id, stage, status, detail)`; allowed stages transcript/processed_text/notes/cloud/summary/projection. `status` pending/done/retryable_failed/shared_audio_pending; no shared raw context deletion. Retired groups must not be re-exported from cache. New continuation after deletion starts a fresh group ID/generation.

## Receiver deployment configuration

The optional Phone service uses `receiver/context_api.py --config <private-json>`.
Its configuration contains `context_root` (protected local storage), `reading_root`
(the processor projection directory), `endpoint` (the existing HTTPS origin),
`timezone`, `port` (loopback only, default 18439), `ffmpeg_path`,
`managed_audio_roots` (explicit content-addressed audio directories), and
`managed_asr_roots` (explicit ASR homes containing managed `outbox/` and `state/`), and optional `managed_receipt_roots` (managed `YYYY-MM-DD/<source-SHA>/` copies of `receipt.json`, `transcript.json`, `transcript.md`). Historical `client-receipt.json` is retained and is never treated as original text.
The configured roots are installation inputs, never accepted through HTTP.

On Windows the context root must be on NTFS with inheritance disabled and only
the intended service user and SYSTEM permitted **before** creating a credential,
Health row, exact location, or recoverable deletion copy. E:/exFAT is unsuitable.
Run the API under that user; do not assume a SYSTEM process can read user-only
paths without verifying the installed ACL. The original audio receiver receives
only `context_root` in its private configuration to reject retired source replay.
Ingress uses `--phone-port 18439`; all public access remains through the existing
HTTPS origin and exact route allowlist, with no additional public listening port.

Local issuance: `python receiver/phone_pair.py --config <private-json> --output
<context_root>/phone-onboarding.json`. The output contains only short-lived
endpoint/code and must be transported privately into the Phone sandbox importer;
stdout contains expiry/purpose only. Do not commit onboarding/runtime files.

Deletion work journals, retained assets and original recoverable copies live
inside `context_root/deletion/<deletion-id>/`. Complete means moved out of active
managed sources into this recoverable area, not physical secure erasure or
removal from independent user backups. Shared raw context/HealthKit records are
not deleted when one conversation is removed. Partial boundary/consumer/cloud
conflicts remain visible as pending or retryable failures.
