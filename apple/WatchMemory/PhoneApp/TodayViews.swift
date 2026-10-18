import SwiftUI
import WatchMemoryCore

struct TodayHome: View {
    @EnvironmentObject private var model: TodayCoordinator
    @EnvironmentObject private var recorder: PhoneCoordinator
    @State private var selectedDate = Date()
    @State private var settings = false
    @State private var hours = false
    private var day: String {
        let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian); f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current; f.dateFormat = "yyyy-MM-dd"; return f.string(from: selectedDate)
    }
    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        DatePicker("查看日期", selection: $selectedDate, displayedComponents: .date)
                            .datePickerStyle(.compact).accessibilityHint("切换到其他日期查看历史记录")
                        VStack(alignment: .leading, spacing: 10) {
                            Label("录音覆盖与处理进度", systemImage: "waveform")
                            Text(model.readStatus).font(.footnote).foregroundStyle(.secondary)
                            Button { hours = true } label: { CoverageStrip(coverage: model.today?.coverage ?? [], day: selectedDate) }
                                .buttonStyle(.plain).accessibilityLabel("查看每小时录音覆盖")
                        }
                        if !model.connected {
                            VStack(alignment: .leading, spacing: 8) {
                                Label(model.connecting ? "正在连接阅读服务" : "尚未连接阅读服务", systemImage: "network")
                                Text("首次连接后，今天的记录会自动出现在这里。").font(.callout)
                                Button("检查连接资料") { model.connectImported() }.buttonStyle(.bordered)
                            }.todayCard()
                        }
                        VStack(alignment: .leading, spacing: 10) {
                            Text("今日概览").font(.title2.bold())
                            if let overview = model.today?.overview, !overview.text.isEmpty {
                                Text(overview.text).textSelection(.enabled)
                            } else {
                                Text(model.today?.overview.state == "pending" ? "已有内容正在整理，完整稿会先出现在下方。" : "暂时没有已整理的概览。已有录音可能仍在上传或处理中。")
                                    .foregroundStyle(.secondary)
                            }
                        }.todayCard()
                        VStack(alignment: .leading, spacing: 8) {
                            Label("身体背景", systemImage: "heart.text.clipboard").font(.headline)
                            if let body = model.today?.body, !body.lines.isEmpty {
                                ForEach(Array(body.lines.enumerated()), id: \.offset) { _, line in Text(line).font(.callout) }
                                Text("生理信号估计 · 参考 \(body.sampleCount) 条样本")
                                    .font(.footnote).foregroundStyle(.secondary)
                            } else {
                                Text(model.healthEnabled ? "尚无足够数据判断身体状态，缺少记录不代表正常或平静。" : "身体背景未启用，录音和全文仍可使用。")
                                    .font(.callout).foregroundStyle(.secondary)
                            }
                        }.todayCard()
                        if let error = model.operationError { Label(error, systemImage: "info.circle").font(.callout) }
                        if let status = model.deletionStatus { DeletionProgress(status: status) }
                        Text("自然对话").font(.title2.bold())
                        if let cards = model.today?.cards, !cards.isEmpty {
                            ForEach(cards.filter { $0.id != model.deletionStatus?.cardId }) { card in
                                NavigationLink { ConversationDetail(card: card) } label: { ConversationRow(card: card) }
                                    .buttonStyle(.plain).id(card.id)
                            }
                        } else { Text("暂未取得可读对话；不会用空小时填满今天。") .foregroundStyle(.secondary) }
                    }.padding(20)
                }.background(Color(.systemGroupedBackground)).refreshable { await model.load(day: day) }
                    .navigationTitle(Calendar.current.isDateInToday(selectedDate) ? "今天" : selectedDate.formatted(date: .abbreviated, time: .omitted))
                    .toolbar { ToolbarItem(placement: .topBarTrailing) {
                        Button { settings = true } label: { Image(systemName: "gearshape") }.accessibilityLabel("设置")
                    } }
                    .sheet(isPresented: $settings) { TodaySettings() }
                    .sheet(isPresented: $hours) {
                        HourIndex(coverage: model.today?.coverage ?? [], selectedDate: selectedDate) { date in
                            hours = false
                            if let card = model.today?.cards.first(where: { (PhoneV3Policy.date($0.endAt) ?? .distantPast) >= date }) {
                                withAnimation { proxy.scrollTo(card.id, anchor: .top) }
                            }
                        }
                    }
                    .task(id: day + (model.connected ? ":connected" : ":waiting")) { if model.connected { await model.load(day: day) } }
            }
        }
    }
}

private struct CoverageStrip: View {
    let coverage: [PhoneToday.Coverage]; let day: Date
    var body: some View {
        VStack(spacing: 4) {
            HStack(spacing: 3) {
                ForEach(0..<24) { hour in
                    let state = hourState(coverage, hour: hour, date: day)
                    RoundedRectangle(cornerRadius: 2).fill(state == "recorded" ? Color.blue : state == "no_speech" ? .gray : .secondary.opacity(0.2))
                        .frame(height: 10).overlay { if state == "pending" { Image(systemName: "ellipsis").font(.system(size: 7)).foregroundStyle(.primary) } }
                }
            }.accessibilityHidden(true)
            HStack { Text("00"); Spacer(); Text("06"); Spacer(); Text("12"); Spacer(); Text("18"); Spacer(); Text("24") }
                .font(.caption).foregroundStyle(.secondary).accessibilityHidden(true)
        }.padding(.vertical, 8)
    }
}
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
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(timeRange(card)).font(.caption).foregroundStyle(.secondary)
            Text(card.title).font(.headline)
            Text(card.summary.isEmpty ? "摘要整理中，点击可读已有全文。" : card.summary).font(.body).lineLimit(3)
            Label(ownerText(card.ownerLabel), systemImage: "person.wave.2").font(.caption).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading).todayCard().accessibilityElement(children: .combine)
    }
}
private struct ConversationDetail: View {
    @EnvironmentObject private var model: TodayCoordinator
    @Environment(\.dismiss) private var dismiss
    let card: PhoneToday.Card
    @State private var level = "摘要"
    @State private var plan: PhoneDeletionPlan?
    @State private var preparing = false
    @State private var error: String?
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text(timeRange(card)).font(.callout).foregroundStyle(.secondary)
                Text(ownerText(card.ownerLabel)).font(.callout)
                Picker("阅读版本", selection: $level) { ForEach(["摘要", "口语清稿", "七删一改整理稿"], id: \.self) { Text($0) } }.pickerStyle(.menu)
                let text = level == "摘要" ? (card.summary.isEmpty ? "摘要暂未完成，仍可切换阅读完整稿。" : card.summary) : level == "口语清稿" ? card.clean : card.distilled
                ForEach(Array(text.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, paragraph in
                    if paragraph.hasPrefix("## ") { Text(String(paragraph.dropFirst(3))).font(.title3.bold()) }
                    else { Text(.init(paragraph)).textSelection(.enabled) }
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
