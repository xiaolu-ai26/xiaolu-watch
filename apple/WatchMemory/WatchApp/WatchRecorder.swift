import AVFoundation
import Combine
import CryptoKit
import WatchKit
import WatchMemoryCore

final class WatchRecorder: ObservableObject {
    @Published private(set) var recording = false
    @Published private(set) var paused = false
    @Published private(set) var sessionActive = false
    @Published private(set) var systemInterrupted = false
    @Published private(set) var recentInterruption: String?
    @Published private(set) var elapsed: TimeInterval = 0
    @Published private(set) var pending = 0
    @Published private(set) var notice = "点一下，开始记录"
    @Published private(set) var batteryLow = false
    @Published private(set) var alertNotice: String?
    @Published private(set) var fatalStorageError: String?
    let store: ChunkStore?
    private let encodingQueue = DispatchQueue(label: "xiaolu.watch.encoding", qos: .utility)
    private let queue = DispatchQueue(label: "xiaolu.watch.audio", qos: .userInitiated)
    private var audioGeneration = UUID()
    private let bufferSlots = DispatchSemaphore(value: 32)
    private let overloadLock = NSLock()
    private var overloaded = false
    private var engine: AVAudioEngine?
    private var converter: AVAudioConverter?
    private var file: AVAudioFile?
    private var current: Chunk?
    private var writtenFrames: AVAudioFramePosition = 0
    private var sessionID = UUID()
    private var sequence = 0
    private var audioUploadFloor = 0
    private var intent = RecordingIntent()
    private var wanted: Bool { get { intent.wanted } set { intent.wanted = newValue } }
    private var userPaused: Bool { get { intent.userPaused } set { intent.userPaused = newValue } }
    private var diagnostics: RecordingDiagnostics?
    private let diagnosticQueue = DispatchQueue(label: "watchmemory.recording.diagnostics", qos: .utility)
    private var lastPCMAt: Date?
    private var lastHeartbeatAt: Date?
    private var stallReported = false
    private var sessionStarted: Date?
    private var timer: Timer?
    private var connection: WatchTransfer?
    private let diagnosticsLock = NSLock()
    private var receivedFirstPCM = false
    private let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!

    init() {
        // One in-memory manifest authority shared with the direct uploader.
        store = WatchDirectUploader.shared.store
        if store == nil { fatalStorageError = "录音资料暂时无法打开，原文件已保留。请勿卸载应用。" }
        WKInterfaceDevice.current().isBatteryMonitoringEnabled = true
        if let store {
            connection = WatchTransfer(store: store) { [weak self] in self?.refresh() }
            diagnostics = try? RecordingDiagnostics(directory: store.directory)
            queue.async { [weak self] in self?.noteAppLaunch(); self?.recoverChunks() }
        }
        if #available(watchOS 27.0, *) {
            NotificationCenter.default.addObserver(self, selector: #selector(sessionDeactivated(_:)), name: AVAudioSession.didBecomeInactiveNotification, object: nil)
            NotificationCenter.default.addObserver(self, selector: #selector(sessionResumption(_:)), name: AVAudioSession.resumptionRecommendationNotification, object: nil)
        } else {
            NotificationCenter.default.addObserver(self, selector: #selector(interrupted(_:)), name: AVAudioSession.interruptionNotification, object: nil)
        }
        NotificationCenter.default.addObserver(self, selector: #selector(routeChanged(_:)), name: AVAudioSession.routeChangeNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(mediaServicesReset), name: AVAudioSession.mediaServicesWereResetNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(engineChanged), name: .AVAudioEngineConfigurationChange, object: nil)
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.tick() }
    }
    deinit { timer?.invalidate(); NotificationCenter.default.removeObserver(self) }
    private func display(_ text: String) { DispatchQueue.main.async { self.notice = text } }
    // Bounded metadata only: no recorded speech, pairing secrets or server credentials.
    private func recordEvent(_ event: String, _ fields: [String: String] = [:]) {
        guard let directory = store?.directory else { return }
        diagnosticsLock.lock(); defer { diagnosticsLock.unlock() }
        do {
            let path = directory.appendingPathComponent("recording-events.jsonl")
            let record: [String: Any] = ["at": Date().timeIntervalSince1970, "event": event, "fields": fields]
            var line = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]); line.append(10)
            if FileManager.default.fileExists(atPath: path.path) {
                let size = (try FileManager.default.attributesOfItem(atPath: path.path)[.size] as? NSNumber)?.intValue ?? 0
                if size + line.count > 256 * 1024 {
                    let previous = try Data(contentsOf: path)
                    var bounded = Data()
                    for row in previous.split(separator: 10).suffix(128) {
                        bounded.append(contentsOf: row); bounded.append(10)
                    }
                    try bounded.write(to: path, options: .atomic)
                }
            } else { try Data().write(to: path, options: .atomic) }
            let file = try FileHandle(forWritingTo: path)
            defer { try? file.close() }
            try file.seekToEnd(); try file.write(contentsOf: line); try file.synchronize()
        } catch { /* Diagnostic failures never change audio custody or stop recording. */ }
    }
    private func publishIntent(_ text: String? = nil) {
        let running = engine?.isRunning ?? false
        let active = wanted; let waiting = intent.interruptionActive; let userPause = userPaused
        DispatchQueue.main.async {
            self.sessionActive = active; self.recording = running && active && !userPause
            self.paused = active && !running; self.systemInterrupted = waiting
            if let text { self.notice = text }
        }
    }
    private func incident(_ event: String, outcome: String, fields: [String: String] = [:]) {
        let at = Date(); let journal = diagnostics
        diagnosticQueue.async { try? journal?.incident(event, outcome: outcome, fields: fields, now: at) }
        let description: String
        switch event {
        case "previous_session_unfinished": description = "上次会话未正常结束，原音已保留"
        case "files_pending_recovery": description = "有片段等待恢复，原音已保留"
        case "pcm_stall_suspected": description = "疑似音频输入停滞，正在保留诊断"
        case "pcm_flow_resumed": description = "音频输入已恢复"
        case "media_reset": description = "音频服务重置，需要点继续"
        default: description = outcome == "recovered" ? "录音已恢复" : (outcome == "waiting" ? "系统音频中断，等待恢复" : "需要点继续，已有片段保留")
        }
        let time = DateFormatter.localizedString(from: at, dateStyle: .none, timeStyle: .short)
        DispatchQueue.main.async { self.recentInterruption = time + " " + description }
    }
    private func checkpoint(clean: Bool, event: String) {
        let journal = diagnostics; let id = sessionStarted == nil ? nil : sessionID; let pcm = lastPCMAt; let at = Date()
        diagnosticQueue.async { try? journal?.update(cleanEnd: clean, sessionID: id, lastPCMAt: pcm, event: event, now: at) }
    }
    private func noteAppLaunch() {
        let previous = try? diagnostics?.checkpoint()
        let recovery = store?.all().filter { [.recording, .needsRecovery].contains($0.state) }.count ?? 0
        recordEvent("app_launch", ["previousUnclean": String(previous?.cleanEnd == false), "recoveryCount": String(recovery)])
        if let previous, !previous.cleanEnd {
            var fields = ["lastKnownEvent": previous.lastKnownEvent, "unknownExitReason": "unknown"]
            if let pcm = previous.lastPCMAt { fields["lastPCMAt"] = String(pcm.timeIntervalSince1970) }
            incident("previous_session_unfinished", outcome: "unknown", fields: fields)
        } else if let last = try? diagnostics?.incidents().last {
            let time = DateFormatter.localizedString(from: last.at, dateStyle: .none, timeStyle: .short)
            let summary = last.outcome == "recovered" ? "上次中断已恢复" : "上次会话有中断记录，原音已保留"
            DispatchQueue.main.async { self.recentInterruption = time + " " + summary }
        }
        if recovery > 0 { incident("files_pending_recovery", outcome: "unknown", fields: ["recoveryCount": String(recovery)]) }
        // Diagnostic history is never an instruction to start a cold microphone.
        publishIntent()
    }
    @discardableResult private func attemptBegin(trigger: String, manual: Bool) -> Bool {
        do {
            if let engine, !engine.isRunning { _ = endEngine(reason: "replace_stopped_engine") }
            try beginEngine(manualProbe: manual)
            guard engine?.isRunning == true else { throw StoreError.invalidState }
            intent.activationSucceeded(); checkpoint(clean: false, event: "engine_started")
            publishIntent("正在记录")
            if trigger != "start" { incident(manual ? "manual_resume" : "automatic_resume", outcome: "recovered", fields: ["lastKnownEvent": trigger]) }
            return true
        } catch {
            recordEvent("activation_failed", errorFields(error).merging(["trigger": trigger]) { _, b in b })
            intent.activationFailed()
            if engine != nil || current != nil { _ = endEngine(reason: "activation_failed") }
            else { WatchDirectUploader.shared.setAudioRuntime(nil); try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
            checkpoint(clean: false, event: "activation_failed")
            incident("activation_failed", outcome: "manual", fields: errorFields(error))
            publishIntent(wanted ? "暂时不能继续录音，已有片段保留；点继续重试" : "录音未能继续，已有片段保留；稍后点开始重试")
            return false
        }
    }
    func noteScenePhase(_ phase: String) {
        queue.async {
            self.recordEvent("scene_phase", ["phase": phase, "wanted": String(self.wanted),
                "engineRunning": String(self.engine?.isRunning ?? false), "userPaused": String(self.userPaused)])
            self.refresh()
            if phase == "active" { self.connection?.sendPending() }
        }
    }
    func start() {
        guard store != nil else { return }
        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            guard let self else { return }
            if !granted { self.display("请在设置中允许麦克风"); return }
            self.queue.async {
                guard !self.wanted else { return }
                self.intent.start(); self.sessionID = UUID(); self.sequence = 0
                self.sessionStarted = Date(); self.lastPCMAt = nil; self.lastHeartbeatAt = Date(); self.stallReported = false
                self.recordEvent("start_requested"); self.checkpoint(clean: false, event: "start_requested")
                _ = self.attemptBegin(trigger: "start", manual: true)
            }
        }
    }
    func togglePause() {
        queue.async {
            guard self.wanted else { return }
            if self.engine?.isRunning == true && !self.intent.interruptionActive {
                self.intent.pause(); let saved = self.endEngine(reason: "user_pause")
                self.publishIntent(saved ? "已暂停，已有录音会继续同步" : "已暂停，有片段保留待恢复")
            } else {
                // One explicit user probe can recover even if the OS omitted an ended event.
                // setActive still arbitrates microphone priority; there is no automatic watchdog.
                self.intent.requestManualResume()
                _ = self.attemptBegin(trigger: "manual_resume", manual: true)
            }
        }
    }
    func stop() {
        queue.async {
            let wasWaiting = self.intent.interruptionActive || self.intent.manualResumeRequired
            self.recordEvent("stop_requested"); self.intent.stop()
            let saved = self.endEngine(reason: "user_stop")
            self.checkpoint(clean: saved, event: saved ? "user_stop_complete" : "user_stop_needs_recovery")
            self.recordEvent("session_ended", ["saved": String(saved)])
            if wasWaiting {
                let time = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .short)
                DispatchQueue.main.async { self.recentInterruption = time + " 已由你结束录音，已有片段保留" }
            }
            self.publishIntent(saved ? "本次录音已保存" : "片段保留待恢复；请勿卸载应用")
            self.connection?.sendPending()
        }
    }
    private func freeBytes() -> Int64 {
        guard let dir = store?.directory else { return 0 }
        let values = try? dir.resourceValues(forKeys: [.volumeAvailableCapacityKey])
        return Int64(values?.volumeAvailableCapacity ?? 0)
    }
    private func beginEngine(manualProbe: Bool = false) throws {
        guard intent.mayActivate(manual: manualProbe), engine == nil else { throw StoreError.invalidState }
        guard freeBytes() > 64 * 1024 * 1024 else { display("空间不足，录音已停止；待传录音已保留"); wanted = false; throw StoreError.invalidState }
        let audio = AVAudioSession.sharedInstance()
        try audio.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers])
        do { try audio.setAllowHapticsAndSystemSoundsDuringRecording(true) }
        catch { DispatchQueue.main.async { self.alertNotice = "录音时消息提醒可能受系统影响" } }
        try audio.setActive(true)
        let next = AVAudioEngine()
        let input = next.inputNode
        let format = input.outputFormat(forBus: 0)
        recordEvent("audio_configured", ["sampleRate": String(format.sampleRate), "channels": String(format.channelCount)])
        guard format.sampleRate > 0, format.channelCount > 0,
              let conversion = AVAudioConverter(from: format, to: outputFormat) else { throw StoreError.invalidMetadata }
        converter = conversion
        try beginFile(at: Date())
        let generation = UUID(); audioGeneration = generation
        overloadLock.lock(); overloaded = false; overloadLock.unlock()
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, when in
            // The tap owns buffer only during callback. Copy PCM before dispatching disk IO.
            guard let self else { return }
            guard self.bufferSlots.wait(timeout: .now()) == .success else {
                self.overloadLock.lock(); let report = !self.overloaded; self.overloaded = true; self.overloadLock.unlock()
                if report { self.queue.async {
                    self.wanted = false; self.endEngine(reason: "buffer_overload"); self.display("保存速度不足，已停止；末尾可能有缺口")
                } }
                return
            }
            guard let copy = Self.copyPCM(buffer) else { self.bufferSlots.signal(); return }
            self.queue.async { [weak self] in
                defer { self?.bufferSlots.signal() }
                guard let self, self.audioGeneration == generation else { return }
                self.consume(copy)
            }
        }
        engine = next
        do {
            next.prepare(); try next.start(); recordEvent("engine_started")
            audioUploadFloor = current?.sequence ?? sequence
            WatchDirectUploader.shared.setAudioRuntime(AudioUploadEligibility.session(engineRunning: next.isRunning, wanted: wanted, paused: userPaused, id: sessionID), minimumSequence: audioUploadFloor)
        }
        catch { recordEvent("engine_start_failed", errorFields(error)); next.inputNode.removeTap(onBus: 0); engine = nil; file = nil; throw error }
        DispatchQueue.main.async { self.recording = true; self.paused = false; self.notice = "正在记录" }
        WKInterfaceDevice.current().play(.start)
    }
    private static func copyPCM(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let output = AVAudioPCMBuffer(pcmFormat: input.format, frameCapacity: input.frameLength) else { return nil }
        output.frameLength = input.frameLength
        let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input.audioBufferList))
        let dst = UnsafeMutableAudioBufferListPointer(output.mutableAudioBufferList)
        guard src.count == dst.count else { return nil }
        for i in 0..<src.count {
            guard let from = src[i].mData, let to = dst[i].mData else { return nil }
            memcpy(to, from, Int(src[i].mDataByteSize)); dst[i].mDataByteSize = src[i].mDataByteSize
        }
        return output
    }
    private func beginFile(at date: Date) throws {
        guard let store else { throw StoreError.invalidState }
        let id = UUID()
        let chunk = Chunk(id: id, sessionID: sessionID, sequence: sequence, capturedAt: date,
                          fileName: id.uuidString.lowercased() + ".caf")
        try store.insert(chunk) // journal before audio file creation
        let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false]
        file = try AVAudioFile(forWriting: store.fileURL(for: chunk), settings: settings,
                               commonFormat: .pcmFormatFloat32, interleaved: false)
        current = chunk; writtenFrames = 0; receivedFirstPCM = false; sequence += 1
        recordEvent("chunk_opened", ["sequence": String(chunk.sequence)])
    }
    private func consume(_ input: AVAudioPCMBuffer) {
        guard let converter, let current, engine != nil, file != nil else { return }
        if !receivedFirstPCM {
            receivedFirstPCM = true
            recordEvent("first_pcm", ["sampleRate": String(input.format.sampleRate), "frames": String(input.frameLength)])
        }
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * 16000 / input.format.sampleRate) + 64)
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return }
        var supplied = false; var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, result in
            if supplied { result.pointee = .noDataNow; return nil }
            supplied = true; result.pointee = .haveData; return input
        }
        do {
            if status == .error { throw error ?? StoreError.invalidState as NSError }
            if output.frameLength > 0 {
                try self.file?.write(from: output); writtenFrames += AVAudioFramePosition(output.frameLength)
                lastPCMAt = Date()
                if stallReported { stallReported = false; incident("pcm_flow_resumed", outcome: "recovered") }
            }
            if writtenFrames >= 60 * 16000 {
                let nextDate = current.capturedAt.addingTimeInterval(Double(writtenFrames) / 16000)
                try finishFile()
                guard freeBytes() > 64 * 1024 * 1024 else {
                    wanted = false; endEngine(reason: "space_limit"); display("空间不足，录音已停止；已有录音保留"); return
                }
                try beginFile(at: nextDate) // only rotate files; input tap/audio session stay running
                recordEvent("segment_rotated", ["nextSequence": String(sequence - 1)])
                connection?.sendPending()
            }
        } catch {
            recordEvent("audio_consume_failed", errorFields(error))
            wanted = false; endEngine(reason: "audio_consume_failed"); display("录音已暂停，片段已保留，请检查空间后重试")
        }
    }
    private func finishFile() throws {
        guard let store, let chunk = current else { file = nil; return }
        file = nil // close CAF before hash, transport or durable final state
        let frames = writtenFrames
        recordEvent("chunk_finalizing", ["frames": String(frames), "duration": String(Double(frames) / 16000),
            "wallAge": String(Date().timeIntervalSince(chunk.capturedAt))])
        current = nil; writtenFrames = 0
        let url = try store.fileURL(for: chunk)
        if frames > 0 {
            let durable = try FileHandle(forWritingTo: url); try durable.synchronize(); try durable.close()
            encodeCompleted(chunk, rawURL: url, duration: Double(frames) / 16000)
        } else { try store.markFailed(chunk.id, message: "片段尚无音频，文件保留待检查") }
        refresh()
    }
    private func encodeCompleted(_ chunk: Chunk, rawURL: URL, duration: Double) {
        // Encoding never runs on the microphone tap/disk capture queue. Raw CAF remains
        // recoverable until the phone's verified, durable application receipt permits cleanup.
        encodingQueue.async {
            guard let store = self.store else { return }
            let encodedName = chunk.id.uuidString.lowercased() + ".m4a"
            let destination = store.directory.appendingPathComponent(encodedName)
            do {
                if FileManager.default.fileExists(atPath: destination.path) {
                    try FileManager.default.removeItem(at: destination) // derived partial; original CAF retained
                }
                let source = try AVAudioFile(forReading: rawURL)
                let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
                    AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32000]
                var encoded: AVAudioFile? = try AVAudioFile(forWriting: destination, settings: settings,
                    commonFormat: .pcmFormatFloat32, interleaved: false)
                guard let buffer = AVAudioPCMBuffer(pcmFormat: source.processingFormat, frameCapacity: 4096) else { throw StoreError.invalidState }
                while source.framePosition < source.length {
                    try source.read(into: buffer)
                    guard buffer.frameLength > 0 else { break }
                    try encoded?.write(from: buffer)
                }
                encoded = nil // finalize AAC container before hashing or sending
                let durable = try FileHandle(forWritingTo: destination); try durable.synchronize(); try durable.close()
                let count = (try FileManager.default.attributesOfItem(atPath: destination.path)[.size] as? NSNumber)?.int64Value ?? 0
                try store.finalize(chunk.id, duration: duration, sha256: Self.hashFile(destination), byteCount: count, encodedFileName: encodedName)
            } catch {
                do {
                    let count = (try FileManager.default.attributesOfItem(atPath: rawURL.path)[.size] as? NSNumber)?.int64Value ?? 0
                    try store.finalize(chunk.id, duration: duration, sha256: Self.hashFile(rawURL), byteCount: count)
                    self.display("已保留原音，等待同步；占用可能较多")
                } catch { self.display("有片段保留待恢复；请勿卸载应用") }
            }
            self.connection?.sendPending(); self.refresh()
        }
    }
    @discardableResult private func endEngine(reason: String) -> Bool {
        WatchDirectUploader.shared.setAudioRuntime(nil)
        if let engine { engine.inputNode.removeTap(onBus: 0); engine.stop() }
        recordEvent("engine_stopped", ["reason": reason, "frames": String(writtenFrames), "wanted": String(wanted)])
        checkpoint(clean: false, event: reason)
        engine = nil; converter = nil; audioGeneration = UUID()
        var saved = true
        do { try finishFile() } catch { saved = false; display("片段保留待恢复；请勿卸载应用") }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        if !saved { incident("capture_error", outcome: "manual", fields: ["lastKnownEvent": "finalize_failed"]) }
        publishIntent()
        return saved
    }
    private func recoverChunks() {
        guard let store else { return }
        do {
            for chunk in try store.recoverInterrupted() {
                let url = try store.fileURL(for: chunk)
                do {
                    let recovered = try AVAudioFile(forReading: url)
                    let frames = recovered.length
                    guard frames > 0 else { continue }
                    let duration = Double(frames) / recovered.fileFormat.sampleRate
                    encodeCompleted(chunk, rawURL: url, duration: duration)
                } catch { display("有一段录音等待恢复，原文件已保留") }
            }
            connection?.sendPending(); refresh()
        } catch { display("录音记录暂时无法打开，原文件已保留") }
    }
    static func hashFile(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        var sha = SHA256()
        while let data = try handle.read(upToCount: 256 * 1024), !data.isEmpty { sha.update(data: data) }
        return sha.finalize().map { String(format: "%02x", $0) }.joined()
    }
    private func tick() {
        queue.async {
            let level = WKInterfaceDevice.current().batteryLevel
            DispatchQueue.main.async {
                self.batteryLow = level >= 0 && level < 0.15
                self.elapsed = (self.store?.all().filter { $0.sessionID == self.sessionID }.reduce(0) { $0 + $1.duration } ?? 0) + Double(self.writtenFrames) / 16000
            }
            if level >= 0 && level < 0.05 && self.wanted {
                self.wanted = false; self.endEngine(reason: "low_battery"); self.display("电量低于 5%，已保存并停止录音")
            }
            if self.wanted, Date().timeIntervalSince(self.lastHeartbeatAt ?? .distantPast) >= 60 {
                self.lastHeartbeatAt = Date()
                self.recordEvent("recording_heartbeat", ["wanted": String(self.wanted), "engineRunning": String(self.engine?.isRunning ?? false),
                    "interruptionActive": String(self.intent.interruptionActive), "frames": String(self.writtenFrames),
                    "lastPCMAt": self.lastPCMAt.map { String($0.timeIntervalSince1970) } ?? "none"])
                self.checkpoint(clean: false, event: "heartbeat")
                if self.engine?.isRunning == true, Date().timeIntervalSince(self.lastPCMAt ?? self.sessionStarted ?? Date()) > 15, !self.stallReported {
                    self.stallReported = true; self.incident("pcm_stall_suspected", outcome: "unknown")
                }
            }
            WatchDirectUploader.shared.setAudioRuntime(AudioUploadEligibility.session(engineRunning: self.engine?.isRunning ?? false, wanted: self.wanted, paused: self.userPaused, id: self.sessionID), minimumSequence: self.audioUploadFloor)
            self.connection?.sendPending(); self.refresh()
        }
    }
    private func refresh() {
        // Read at publication time: an older enqueue notification must not overwrite
        // a newer ACK with a count captured before the durable receipt arrived.
        DispatchQueue.main.async {
            self.pending = self.store?.all().filter { ![.received, .cleared].contains($0.state) }.count ?? 0
        }
    }
    private func errorFields(_ error: Error) -> [String: String] {
        let value = error as NSError
        return ["domain": value.domain, "code": String(value.code)]
    }
    private func systemInterruptionBegan(fields: [String: String]) {
        recordEvent("audio_interruption", fields.merging(["type": "began", "wanted": String(wanted), "frames": String(writtenFrames)]) { _, b in b })
        intent.interruptionBegan()
        let saved = engine != nil || current != nil ? endEngine(reason: "system_interruption") : true
        if wanted { incident("system_interruption", outcome: "waiting", fields: fields); publishIntent(saved ? "系统正在使用音频，已有片段保存，等待恢复" : "系统正在使用音频，有片段待恢复") }
    }
    private func systemInterruptionEnded(shouldResume: Bool) {
        recordEvent("audio_interruption", ["type": "ended", "shouldResume": String(shouldResume), "wanted": String(wanted)])
        let running = engine?.isRunning ?? false
        if intent.interruptionEnded(shouldResume: shouldResume, engineRunning: running) {
            _ = attemptBegin(trigger: "system_resume", manual: false)
        } else if wanted && !running {
            if userPaused { publishIntent("已由你暂停，点继续可恢复") }
            else { incident("no_resume_hint", outcome: "manual"); publishIntent("系统未建议自动恢复，点继续重试") }
        } else { publishIntent() }
    }
    @objc private func interrupted(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        let reason = note.userInfo?[AVAudioSessionInterruptionReasonKey] as? UInt
        let options = AVAudioSession.InterruptionOptions(rawValue: note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
        queue.async {
            if type == .began { self.systemInterruptionBegan(fields: reason.map { ["reasonCode": String($0)] } ?? [:]) }
            else { self.systemInterruptionEnded(shouldResume: options.contains(.shouldResume)) }
        }
    }
    @available(watchOS 27.0, *)
    @objc private func sessionDeactivated(_ note: Notification) {
        guard let context = note.userInfo?[AVAudioSession.deactivationContextKey] as? AVAudioSession.DeactivationContext,
              context.source == .system else { return } // own pause/stop deactivation is not a system interruption
        var fields = ["sourceCode": String(context.source.rawValue)]
        if let reason = context.interruptionContext?.reason { fields["reasonCode"] = String(reason.rawValue) }
        queue.async { self.systemInterruptionBegan(fields: fields) }
    }
    @available(watchOS 27.0, *)
    @objc private func sessionResumption(_ note: Notification) {
        guard let context = note.userInfo?[AVAudioSession.resumptionContextKey] as? AVAudioSession.ResumptionContext else { return }
        queue.async { self.systemInterruptionEnded(shouldResume: context.recommendation == .shouldResume) }
    }
    @objc private func routeChanged(_ note: Notification) {
        let reason = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
        queue.async { self.recordEvent("audio_route_changed", ["reasonCode": reason.map(String.init) ?? "unknown"]) }
    }
    @objc private func mediaServicesReset() {
        queue.async {
            self.recordEvent("media_services_reset")
            if self.wanted {
                self.intent.activationFailed(); _ = self.endEngine(reason: "media_services_reset")
                self.incident("media_reset", outcome: "manual"); self.publishIntent("音频服务重置，原音保留；点继续恢复")
            }
        }
    }
    @objc private func engineChanged() {
        queue.async {
            self.recordEvent("engine_configuration_changed", ["engineRunning": String(self.engine?.isRunning ?? false),
                "wanted": String(self.wanted), "userPaused": String(self.userPaused), "interruptionActive": String(self.intent.interruptionActive)])
            guard self.intent.canRestartAutomatically, let engine = self.engine, !engine.isRunning else { return }
            _ = self.endEngine(reason: "engine_configuration_change")
            _ = self.attemptBegin(trigger: "engine_configuration_change", manual: false)
        }
    }
}
