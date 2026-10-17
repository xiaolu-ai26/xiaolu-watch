import SwiftUI
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == PhoneCoordinator.backgroundID else { completionHandler(); return }
        PhoneCoordinator.shared.setBackgroundCompletion(completionHandler)
    }
}

@main
struct WatchMemoryPhoneApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = PhoneCoordinator.shared
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup { PhoneHome().environmentObject(model).tint(.blue) }
            .onChange(of: scenePhase) { _, state in if state == .active { model.publishWatchConfiguration(); model.scheduleUploads() } }
    }
}

private struct PhoneHome: View {
    @EnvironmentObject var model: PhoneCoordinator
    @State private var showingSetup = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Image(systemName: "waveform.circle.fill").font(.system(size: 64)).foregroundStyle(.blue)
                    Text("把值得留下的话，\n慢慢整理好。").font(.largeTitle.bold())
                    VStack(alignment: .leading, spacing: 12) {
                        Label(model.watchStatus, systemImage: "applewatch")
                        Label(model.uploadStatus, systemImage: model.verifiedConnection && model.pending == 0 ? "checkmark.circle" : "arrow.triangle.2.circlepath")
                    }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.blue.opacity(0.07), in: RoundedRectangle(cornerRadius: 20))
                    HStack(spacing: 32) {
                        VStack(alignment: .leading) { Text("\(model.pending)").font(.title.bold()); Text("等待送达").foregroundStyle(.secondary) }
                        VStack(alignment: .leading) { Text("\(model.completed)").font(.title.bold()); Text("已送达电脑").foregroundStyle(.secondary) }
                    }
                    if let error = model.lastError { Label(error, systemImage: "info.circle").font(.callout).foregroundStyle(.secondary) }
                    if !model.ready {
                        Button("连接我的电脑") { showingSetup = true }.buttonStyle(.borderedProminent).controlSize(.large)
                    }
                    Text("在手表点一次开始，录音分段后由手表直接上传到你选择的 Mac 或 Windows 处理端。手机只负责首次连接设置；手表离线时原音保留。")
                        .font(.callout).foregroundStyle(.secondary)
                    Text("请让对话者知情。系统中断、强制关闭或重启后，请查看手表上的录音状态。")
                        .font(.footnote).foregroundStyle(.secondary)
                }.padding(24)
            }.navigationTitle("小鹿录音")
                .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("连接设置") { showingSetup = true } } }
                .sheet(isPresented: $showingSetup) { SetupView() }
        }
    }
}

private struct SetupView: View {
    @EnvironmentObject var model: PhoneCoordinator
    @Environment(\.dismiss) private var dismiss
    @State private var scan = false
    @State private var text = ""
    @State private var connecting = false
    @State private var scannerError: String?
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("扫描你选择的 Mac 或 Windows 处理端提供的 HTTPS 连接。设置会保存到手表，之后手表可以自行上传，不需要手机转发录音。")
                    Button { scan = true } label: { Label("扫描电脑上的二维码", systemImage: "qrcode.viewfinder") }
                }
                Section("也可以粘贴连接信息") {
                    SecureField("粘贴电脑提供的完整连接信息", text: $text).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button(connecting ? "正在保存…" : "保存连接信息") { connect(text) }.disabled(text.isEmpty || connecting)
                }
                if let error = scannerError { Section { Text(error).foregroundStyle(.secondary) } }
                if let error = model.lastError { Section { Text(error).foregroundStyle(.secondary) } }
                Section("可选备用方式") {
                    Toggle("允许手机备用中转", isOn: Binding(get: { model.phoneFallbackEnabled }, set: { model.setPhoneFallback($0) }))
                    Text("默认关闭。开启后手机可代传保留的旧录音；手表直传仍会继续。")
                }
                Section { Text("密钥保存在手机和手表各自的钥匙串。手表上传前会加密录音；不要分享连接二维码。").font(.footnote) }
            }.navigationTitle("连接我的电脑").toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } } }
                .sheet(isPresented: $scan) {
                    NavigationStack {
                        QRScanner(found: { value in scan = false; connect(value) }, failed: { message in scannerError = message; scan = false })
                            .ignoresSafeArea().navigationTitle("扫描连接二维码")
                            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { scan = false } } }
                    }
                }
        }
    }
    private func connect(_ value: String) {
        connecting = true
        model.configure(value) { success in connecting = false; text = ""; if success { dismiss() } }
    }
}
