import SwiftUI
import WatchMemoryCore

private struct FlowFrameKey: PreferenceKey {
    static var defaultValue: CGRect = .zero
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) { value = nextValue() }
}

struct TodayHome: View {
    @EnvironmentObject private var model: TodayCoordinator
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var systemMotionLimited = ReadingMotionBudget.systemLimited
    @State private var flowVisible = false
    @Namespace private var flow
    @State private var selectedDate = Date()
    @State private var settings = false
    @State private var dates = false
    @State private var hours = false
    @State private var showAll = false
    @State private var bodyExpanded = false
    private var motionLimited: Bool { reduceMotion || systemMotionLimited || scenePhase != .active }
    private var day: String {
        let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian); f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current; f.dateFormat = "yyyy-MM-dd"; return f.string(from: selectedDate)
    }
    private var cards: [PhoneToday.Card] { (model.today?.cardsNewestFirst ?? []).filter { $0.id != model.deletionStatus?.cardId } }
    private var visibleCards: [PhoneToday.Card] { showAll ? cards : cards.filter(\.isMainReading) }
    private var featured: PhoneToday.Card? { visibleCards.first }
    var body: some View {
        NavigationStack {
            GeometryReader { viewport in
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            opening(width: viewport.size.width)
                            recordStatus.padding(.top, 20)
                            if let error = model.operationError { Label(error, systemImage: "info.circle").font(.footnote).foregroundStyle(ReadingPalette.quiet).padding(.vertical, 12) }
                            if let status = model.deletionStatus { DeletionProgress(status: status) }
                            if !model.connected { connectionNotice }
                            HStack {
                                Text(showAll ? "全部记录" : "继续回看").font(.system(.title2, design: .serif))
                                Spacer()
                                Menu {
                                    Button("主线", systemImage: showAll ? "circle" : "checkmark.circle") { showAll = false }
                                    Button("全部记录", systemImage: showAll ? "checkmark.circle" : "circle") { showAll = true }
                                } label: { Text(showAll ? "全部记录" : "主线 / 全部记录").font(.caption).padding(.vertical, 14) }
                                    .accessibilityLabel("阅读范围，当前" + (showAll ? "全部记录" : "主线"))
                            }.padding(.top, 28)
                            ForEach(Array(visibleCards.dropFirst())) { card in cardLink(card) }
                            if !showAll && cards.contains(where: { !$0.isMainReading }) {
                                Button("其他记录也已保留，查看全部") { showAll = true }.font(.subheadline).padding(.vertical, 20)
                            }
                            if visibleCards.isEmpty {
                                Text(cards.isEmpty ? "有内容整理好后，会在这里留下一条线索。" : "其他记录已保留，可以切换全部查看。")
                                    .font(.callout).foregroundStyle(ReadingPalette.quiet).padding(.vertical, 20)
                            }
                        }.padding(.horizontal, 24).padding(.bottom, 36)
                    }
                    .coordinateSpace(name: "readingFlowScroll")
                    .onPreferenceChange(FlowFrameKey.self) { frame in
                        guard frame.width > 0, frame.height > 0 else { return }
                        flowVisible = frame.maxY > 0 && frame.minY < viewport.size.height
                    }
                    .background(ReadingPalette.paper).refreshable { await model.load(day: day) }
                    .toolbar(.hidden, for: .navigationBar)
                    .sheet(isPresented: $settings) { TodaySettings() }
                    .sheet(isPresented: $dates) {
                        NavigationStack {
                            DatePicker("查看日期", selection: $selectedDate, displayedComponents: .date).datePickerStyle(.graphical).padding()
                                .navigationTitle("选择日期")
                                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dates = false } } }
                        }.presentationDetents([.medium, .large])
                    }
                    .sheet(isPresented: $hours) {
                        HourIndex(coverage: model.today?.coverage ?? [], selectedDate: selectedDate) { date in
                            hours = false
                            let target = model.today?.card(atOrAfter: date, excluding: model.deletionStatus?.cardId)
                            if let target {
                                if !target.isMainReading { showAll = true }
                                Task { @MainActor in
                                    await Task.yield()
                                    if motionLimited { proxy.scrollTo(target.id, anchor: .top) }
                                    else { withAnimation(.easeInOut(duration: 0.25)) { proxy.scrollTo(target.id, anchor: .top) } }
                                }
                            }
                        }
                    }
                    .onReceive(NotificationCenter.default.publisher(for: ProcessInfo.thermalStateDidChangeNotification)) { _ in systemMotionLimited = ReadingMotionBudget.systemLimited }
                    .task(id: day + (model.connected ? ":connected" : ":waiting") + String(describing: scenePhase)) {
                        guard model.connected, scenePhase == .active else { return }; await model.load(day: day)
                    }
                }
            }
        }.tint(ReadingPalette.cobalt).preferredColorScheme(.light)
    }
    private func opening(width: CGFloat) -> some View {
        let heroHeight = max(430, min(600, width * 1.54))
        return VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("小鹿录音").font(.system(size: 18, weight: .regular, design: .serif)).tracking(4)
                    Spacer()
                    Button { settings = true } label: { Image(systemName: "gearshape").font(.title3).frame(width: 44, height: 44).background(ReadingPalette.paper.opacity(0.96), in: Circle()) }
                        .accessibilityLabel("设置")
                }.padding(.top, 22)
                Text(ingestionHeadline).font(.caption2).foregroundStyle(ReadingPalette.quiet)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 4)
                Button { dates = true } label: {
                    HStack(alignment: .center, spacing: 16) {
                        Text(String(Calendar.current.component(.day, from: selectedDate))).font(.system(size: typeSize.isAccessibilitySize ? 90 : 116, weight: .regular, design: .serif)).fixedSize(horizontal: true, vertical: true).padding(.trailing, 8)
                        VStack(alignment: .leading, spacing: 12) {
                            Text(selectedDate.formatted(.dateTime.month().locale(Locale(identifier: "zh_CN"))).replacingOccurrences(of: " ", with: ""))
                            Text("／").font(.system(size: 20, weight: .ultraLight)).foregroundStyle(ReadingPalette.quiet)
                            Text(weekday)
                        }.font(.system(.title3, design: .serif))
                    }.foregroundStyle(ReadingPalette.ink)
                }.buttonStyle(.plain).padding(.top, 32)
                    .accessibilityLabel("选择日期，" + selectedDate.formatted(date: .complete, time: .omitted))
                Spacer(minLength: 0)
            }.frame(height: heroHeight, alignment: .top)
                .background { GeometryReader { geometry in Color.clear.preference(key: FlowFrameKey.self, value: geometry.frame(in: .named("readingFlowScroll"))) } }
            overview
            if let card = featured {
                NavigationLink { ConversationDetail(card: card).flowDestination(card.id, in: flow, motionDisabled: motionLimited) } label: {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(card.title).font(.system(.subheadline, design: .serif)).foregroundStyle(ReadingPalette.ink).fixedSize(horizontal: false, vertical: true)
                        HStack(spacing: 10) { Text("查看完整稿"); Image(systemName: "arrow.right") }.font(.subheadline).foregroundStyle(ReadingPalette.cobalt)
                    }.padding(.top, 28).padding(.bottom, 24).frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).id(card.id).flowSource(card.id, in: flow, motionDisabled: motionLimited)
            }
            bodyContext.padding(.top, 8)
        }
        .background(alignment: .top) {
            EditorialPointFlow(animate: !motionLimited && flowVisible, renderAllowed: scenePhase == .active && flowVisible)
                .frame(width: width, height: 844)
                // Preserve the selected composition but keep all reading text clear.
                .overlay(alignment: .top) {
                    LinearGradient(stops: [
                        .init(color: .clear, location: 0),
                        .init(color: .clear, location: (heroHeight - 20) / 844),
                        .init(color: ReadingPalette.paper, location: min(1, (heroHeight + 220) / 844))
                    ], startPoint: .top, endPoint: .bottom)
                }
                .allowsHitTesting(false).accessibilityHidden(true)
        }
    }
    private var weekday: String {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "EEE"; return formatter.string(from: selectedDate)
    }
    private var overview: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 16) {
                Text("今日概览").font(.system(.subheadline, design: .serif)).tracking(3)
                Rectangle().fill(ReadingPalette.quiet.opacity(0.45)).frame(width: 120, height: 0.5)
            }.foregroundStyle(ReadingPalette.ink)
            if let value = model.today?.overview, !value.text.isEmpty {
                Text(value.text).font(.system(.title2, design: .serif)).foregroundStyle(ReadingPalette.ink).lineSpacing(7).textSelection(.enabled)
                if let start = value.coverageStartAt, let end = value.coverageEndAt {
                    Text("内容覆盖 " + clock(start) + "–" + clock(end) + (value.isPartial == true ? " · 部分完成" : ""))
                        .font(.caption).foregroundStyle(ReadingPalette.quiet)
                } else if value.isPartial == true { Text("已整理部分，后续内容会继续更新").font(.caption).foregroundStyle(ReadingPalette.quiet) }
            } else {
                Text("正在整理这一天的记录。").font(.system(.title2, design: .serif)).foregroundStyle(ReadingPalette.quiet)
                Text("已完成的片段可以先阅读全文。").font(.caption).foregroundStyle(ReadingPalette.quiet)
            }
        }
    }
    private var bodyContext: some View {
        DisclosureGroup(isExpanded: $bodyExpanded) {
            VStack(alignment: .leading, spacing: 12) {
                if let value = model.today?.body {
                    ForEach(Array(value.lines.enumerated()), id: \.offset) { _, line in Text(line).font(.callout).lineSpacing(4) }
                    Text("参考 \(value.sampleCount) 条样本 · 生理信号估计").font(.caption)
                }
                Text("背景仅作参考，不说明某段对话造成了身体变化。").font(.caption)
            }.foregroundStyle(ReadingPalette.quiet).padding(.top, 16)
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text("身体背景 · " + (model.today?.body.measuredThroughAt.map { "测量截至" + clock($0) } ?? "测量时间待同步"))
                    .font(.caption).foregroundStyle(ReadingPalette.quiet)
                if model.today?.body.state == "unknown" { Text("数据不足，暂不判断").font(.caption).foregroundStyle(ReadingPalette.quiet) }
            }
        }.tint(ReadingPalette.quiet)
    }
    private var ingestionHeadline: String {
        guard let ingestion = model.today?.ingestion else { return "录音状态待同步" }
        guard let latest = ingestion.latestRecordedAt else {
            return ingestion.state == "empty" ? "当天尚未收到录音" : "录音状态待同步"
        }
        let recorded = "最新录到 " + clock(latest)
        if ingestion.queuedTextCount > 0 { return recorded + " · 还有 \(ingestion.queuedTextCount) 段待整理" }
        if ingestion.queuedAsrCount > 0 { return recorded + " · 还有 \(ingestion.queuedAsrCount) 段待转写" }
        return recorded
    }
    private var recordStatus: some View {
        Button { hours = true } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "waveform").padding(.top, 2)
                VStack(alignment: .leading, spacing: 5) {
                    if let ingestion = model.today?.ingestion {
                        Text(ingestion.latestRecordedAt.map { "最新录到 " + clock($0) }
                             ?? (ingestion.state == "empty" ? "当天尚未收到录音" : "录音状态待同步"))
                        Text("转写至 " + (ingestion.latestTranscribedAt.map(clock) ?? "待同步")
                             + " · 整理至 " + (ingestion.latestOrganizedAt.map(clock) ?? "待同步"))
                        if ingestion.queuedAsrCount > 0 || ingestion.queuedTextCount > 0 {
                            Text("\(ingestion.queuedAsrCount) 段待转写 · \(ingestion.queuedTextCount) 段待整理")
                        }
                        if ingestion.state == "budget_wait" { Text("等待文字整理额度，已收到的录音仍保留") }
                    } else { Text("录音状态待同步") }
                    Text(model.readStatus).foregroundStyle(ReadingPalette.quiet)
                }.fixedSize(horizontal: false, vertical: true)
                Spacer(); Image(systemName: "chevron.right").padding(.top, 2)
            }.font(.caption).foregroundStyle(ReadingPalette.quiet).frame(minHeight: 44)
        }.buttonStyle(.plain).accessibilityLabel("录音覆盖与处理进度")
    }
    private var connectionNotice: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.connecting ? "正在连接" : "连接你的记录").font(.headline)
            Button("检查连接") { model.connectImported() }.buttonStyle(.bordered)
        }.readingSurface()
    }
    private func cardLink(_ card: PhoneToday.Card) -> some View {
        NavigationLink { ConversationDetail(card: card).flowDestination(card.id, in: flow, motionDisabled: motionLimited) } label: {
            ConversationRow(card: card, isLast: card.id == visibleCards.last?.id)
        }.buttonStyle(.plain).id(card.id).flowSource(card.id, in: flow, motionDisabled: motionLimited)
    }
}
private func clock(_ value: String) -> String { PhoneV3Policy.date(value)?.formatted(date: .omitted, time: .shortened) ?? "待确认" }

private func hourState(_ rows: [PhoneToday.Coverage], hour: Int, date: Date) -> String {
    let start = Calendar.current.date(bySettingHour: hour, minute: 0, second: 0, of: date) ?? date
    let end = start.addingTimeInterval(3600)
    let states = rows.filter { (PhoneV3Policy.date($0.startAt) ?? .distantFuture) < end && (PhoneV3Policy.date($0.endAt) ?? .distantPast) > start }.map(\.state)
    return ["recorded", "no_speech", "pending"].first(where: { states.contains($0) }) ?? "unknown"
}
private func stateText(_ state: String) -> String {
    switch state { case "recorded": return "有已收到的录音"; case "no_speech": return "已处理，未识别到清晰语音"; case "pending": return "等待传输或处理"; default: return "覆盖未知" }
}
private struct HourIndex: View {
    let coverage: [PhoneToday.Coverage]; let selectedDate: Date; let selected: (Date) -> Void
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List(0..<24) { hour in
                let date = Calendar.current.date(bySettingHour: hour, minute: 0, second: 0, of: selectedDate) ?? selectedDate
                Button { selected(date) } label: { HStack { Text(String(format: "%02d:00", hour)); Text(stateText(hourState(coverage, hour: hour, date: selectedDate))) } }
                    .foregroundStyle(.primary).padding(.vertical, 5)
            }.navigationTitle("时间定位")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } } }
        }
    }
}
private func ownerText(_ label: String) -> String {
    label == "self" ? "疑似本人讲话" : label == "other" ? "包含未匹配本人的声音" : "归属未确认"
}
private func timeRange(_ card: PhoneToday.Card) -> String {
    guard let a = PhoneV3Policy.date(card.startAt), let b = PhoneV3Policy.date(card.endAt) else { return "时间待确认" }
    return a.formatted(date: .abbreviated, time: .shortened) + " — " + b.formatted(date: .omitted, time: .shortened)
}
private struct ConversationRow: View {
    let card: PhoneToday.Card
    let isLast: Bool
    @Environment(\.dynamicTypeSize) private var typeSize
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(clock(card.startAt) + " – " + clock(card.endAt)).font(.caption).foregroundStyle(ReadingPalette.quiet)
            HStack(alignment: .top, spacing: 12) {
                Text(card.title).font(.system(.title3, design: .serif)).foregroundStyle(ReadingPalette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4); Image(systemName: "arrow.up.right").font(.subheadline).foregroundStyle(ReadingPalette.cobalt)
            }
            if !card.summary.isEmpty {
                Text(card.summary).font(.subheadline).foregroundStyle(ReadingPalette.quiet).lineSpacing(4)
                    .lineLimit(typeSize.isAccessibilitySize ? nil : 3)
            } else {
                Text(card.summaryState == "background" ? "完整记录已保留" : card.summaryState == "failed" ? "摘要暂未完成 · 全文可读" : "摘要整理中 · 全文可读")
                    .font(.caption).foregroundStyle(ReadingPalette.quiet)
            }
            if !isLast { Divider().padding(.top, 14) }
        }.padding(.vertical, 14).accessibilityElement(children: .combine)
    }
}
private struct ConversationDetail: View {
    @EnvironmentObject private var model: TodayCoordinator
    @Environment(\.dismiss) private var dismiss
    let card: PhoneToday.Card
    @State private var level = "摘要"
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var plan: PhoneDeletionPlan?
    @State private var preparing = false
    @State private var error: String?
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text(timeRange(card)).font(.callout).foregroundStyle(.secondary)
                Text(ownerText(card.ownerLabel)).font(.callout)
                if typeSize.isAccessibilitySize {
                    Picker("阅读版本", selection: $level) { ForEach(["摘要", "口语清稿", "七删一改整理稿"], id: \.self) { Text($0) } }.pickerStyle(.menu)
                } else {
                    Picker("阅读版本", selection: $level) {
                        Text("摘要").tag("摘要"); Text("口语清稿").tag("口语清稿"); Text("整理稿").tag("七删一改整理稿")
                    }.pickerStyle(.segmented)
                }
                let text = level == "摘要" ? (card.summary.isEmpty ? "摘要暂未完成，仍可切换阅读完整稿。" : card.summary) : level == "口语清稿" ? card.clean : card.distilled
                ForEach(Array(text.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, paragraph in
                    if paragraph.hasPrefix("## ") { Text(String(paragraph.dropFirst(3))).font(.title3.bold()) }
                    else { Text(.init(paragraph)).lineSpacing(5).textSelection(.enabled) }
                }
                if let error { Text(error).foregroundStyle(.secondary) }
                Button(role: .destructive) {
                    preparing = true
                    Task { do { plan = try await model.makeDeletionPlan(card) } catch { self.error = "无法取得精确回收预览，未删除任何内容" }; preparing = false }
                } label: { Label(preparing ? "正在取得预览…" : "回收这段对话", systemImage: "trash") }.disabled(preparing)
            }.padding(20)
        }.navigationTitle(card.title).navigationBarTitleDisplayMode(.inline)
            .sheet(item: $plan) { DeletionPreview(plan: $0, card: card) }
            .onChange(of: model.deletionStatus?.deletionId, initial: true) { _, _ in
                if model.deletionStatus?.cardId == card.id {
                    dismiss()
                    Task { if let day = model.today?.date { await model.load(day: day) } }
                }
            }
    }
}
private struct DeletionPreview: View {
    @EnvironmentObject private var model: TodayCoordinator
    @Environment(\.dismiss) private var dismiss
    let plan: PhoneDeletionPlan; let card: PhoneToday.Card
    @State private var committing = false
    var body: some View {
        NavigationStack {
            Form {
                Section("回收预览") {
                    Text(card.title).font(.headline); Text(timeRange(card))
                    Text("包括此对话在接收端已关联的续段。回收会传播到对应文稿、摘要和已管理的笔记副本；其他对话应保留。")
                    Text("受影响记录：\(plan.affectedCards.filter { $0.impact == "delete" }.count) 条")
                }
                Section("原音范围") {
                    if plan.audioStatus == "shared_audio_pending" {
                        Label("部分原音与其他对话共享，尚不能完整回收", systemImage: "exclamationmark.triangle")
                        Text("共享或粗时间边界会保留，不能把这一结果当作全部原音已删除。")
                    } else { Text(plan.audioStatus == "no_audio" ? "没有可回收的对应原音" : "对应原音可进入接收端恢复区") }
                }
                Section {
                    Text("已发往文字供应商的数据无法由本应用撤回。用户自行复制或手改的稿件不会被强制覆盖。")
                    Button(role: .destructive) {
                        committing = true
                        Task { await model.confirmDeletion(plan); committing = false; if model.deletionStatus?.deletionId == plan.planId { dismiss() } }
                    } label: { Text(committing ? "正在确认…" : "按此范围回收并同步") }.disabled(committing)
                }
                if let error = model.operationError { Text(error).foregroundStyle(.secondary) }
            }.navigationTitle("确认回收范围")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(committing) } }
        }.interactiveDismissDisabled(committing)
    }
}
private struct DeletionProgress: View {
    let status: PhoneDeletionStatus
    var body: some View {
        Label(message, systemImage: status.state == "completed" ? "checkmark.circle" : "clock")
            .font(.callout).todayCard()
    }
    private var message: String {
        switch status.state {
        case "completed": return "对应原音与管理文稿已确认回收"
        case "shared_audio_pending": return "回收尚未全部完成：共享原音仍被保留，未影响其他对话"
        case "retryable_failed": return "部分回收步骤待重试，未确认全部完成"
        default: return "回收正在传播，尚未确认全部完成"
        }
    }
}
private struct TodaySettings: View {
    @EnvironmentObject private var model: TodayCoordinator
    @Environment(\.dismiss) private var dismiss
    @State private var connection = false
    var body: some View {
        NavigationStack {
            Form {
                Section("处理电脑") {
                    Text(model.connected ? "手机阅读连接已保存" : "手机阅读尚未连接")
                    Button("检查手机连接资料") { model.connectImported() }
                    Button("原有手表连接设置") { connection = true }
                    Text(model.contextStatus).font(.footnote)
                }
                Section("身体背景，只读") {
                    Toggle("启用身体背景", isOn: Binding(get: { model.healthEnabled }, set: { model.requestHealth($0) })).disabled(!model.connected)
                    Text(model.healthStatus).font(.footnote)
                    Text("读取可用的心率、静息心率、HRV SDNN、睡眠、步数和运动记录，不写入健康数据。空结果不能确定是没记录还是未允许读取。原始样本只送你的处理电脑，不直接发文字模型。")
                        .font(.footnote)
                }
                Section("大致地点") {
                    Toggle("启用地点背景", isOn: Binding(get: { model.locationEnabled }, set: { model.requestLocation($0) })).disabled(!model.connected)
                    Text(model.placeStatus).font(.footnote)
                    Button("允许后台停留记录") { model.requestBackgroundLocation() }.disabled(!model.locationEnabled)
                    Text("使用手机的大致位置和停留事件，不持续采集精确轨迹。系统解析城市／街区时会把粗坐标交给 Apple 的地理服务；失败显示地点待确认。后台更新可能延迟。") .font(.footnote)
                }
                Section("派生背景与 AI") {
                    Toggle("允许派生背景用于 AI", isOn: Binding(get: { model.shareDerivedWithAI }, set: { model.updateShareDerived($0) })).disabled(!model.connected)
                    Text("默认关闭。开启后只允许有限身体标签、大致地点、时间范围和不确定性进入相关 AI 请求；原始心率、HRV 与坐标不直接发出。关闭停止未来附加，不能撤回此前已发送的数据。") .font(.footnote)
                }
                Section("阅读与数据范围") {
                    Text("覆盖条只表示已取得的记录，不是压力或效率。语音判定、健康数据缺失和录音状态分别呈现；身体估计不说明某次对话造成变化。摘要是额外阅读层，完整两稿仍保留。")
                    Text("关闭背景不等于删除历史。对话详情提供回收范围预览；共享原音无法安全拆分时会保留并显示未完成，不影响别的对话。")
                }
            }.navigationTitle("设置")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
                .sheet(isPresented: $connection) { SetupView() }
        }
    }
}
private extension View {
    func todayCard() -> some View { self.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18)) }
}
