import SwiftUI

@main
struct WatchMemoryWatchApp: App {
    @WKApplicationDelegateAdaptor(WatchApplicationDelegate.self) private var delegate
    @StateObject private var recorder = WatchRecorder()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup { RecorderView().environmentObject(recorder) }
            .onChange(of: scenePhase) { _, phase in
                recorder.noteScenePhase(phase == .active ? "active" : phase == .background ? "background" : "inactive")
                if phase == .active { WatchPairingSetup.shared.resumeAutomatically() }
            }
    }
}

private struct RecorderView: View {
    @EnvironmentObject var model: WatchRecorder
    @ObservedObject private var direct = WatchDirectUploader.shared
    @State private var showingSetup = false
    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                HStack { Image(systemName: model.recording ? "waveform" : "mic.fill"); Text("小鹿录音").font(.headline) }
                    .foregroundStyle(.blue)
                if let error = model.fatalStorageError {
                    Text(error).font(.footnote)
                } else {
                    Text(model.notice).font(.footnote).multilineTextAlignment(.center)
                    if model.sessionActive {
                        Text(Duration.seconds(model.elapsed).formatted(.time(pattern: .hourMinuteSecond)))
                            .font(.system(.title3, design: .monospaced)).monospacedDigit()
                        HStack {
                            Button(model.recording ? "暂停" : (model.systemInterrupted ? "尝试继续" : "继续")) { model.togglePause() }.tint(.blue)
                            Button("结束") { model.stop() }.tint(.gray)
                        }
                    } else {
                        Button { model.start() } label: { Label("开始记录", systemImage: "record.circle") }
                            .tint(.blue).buttonStyle(.borderedProminent)
                    }
                    if let interruption = model.recentInterruption { Text(interruption).font(.caption2).foregroundStyle(.secondary) }
                    Button("连接处理电脑") { showingSetup = true }.font(.caption2)
                    Text(direct.status)
                        .font(.caption2).foregroundStyle(.secondary)
                    Toggle("允许蜂窝自动同步", isOn: Binding(get: { direct.allowsCellular }, set: { direct.setCellular($0) }))
                        .font(.caption2)
                    Text("有网络时手表直接上传；离线时原音留在手表。蜂窝同步需要手表具备可用的蜂窝网络。")
                        .font(.caption2).foregroundStyle(.secondary)
                    if model.batteryLow { Label("电量较低，留意充电", systemImage: "battery.25percent").font(.caption2).foregroundStyle(.orange) }
                    if let notice = model.alertNotice { Text(notice).font(.caption2).foregroundStyle(.orange) }
                    Text("录音时请让对话者知情").font(.caption2).foregroundStyle(.secondary)
                }
            }.padding(.horizontal, 4)
        }.sheet(isPresented: $showingSetup) { IndependentSetupView() }
    }
}

private struct IndependentSetupView: View {
    @ObservedObject private var setup = WatchPairingSetup.shared
    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                Text("连接处理电脑").font(.headline)
                TextField("https://处理端地址", text: $setup.endpoint)
                SecureField("16位一次性配对码", text: $setup.code)
                Button(setup.busy ? "连接中…" : "连接") { setup.connect() }.disabled(setup.busy)
                Text(setup.message).font(.caption2)
                Text("配对码由你选择的 Mac 或 Windows 处理端生成；之后手表可自行上传。").font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}
