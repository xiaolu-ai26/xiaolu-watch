import SwiftUI
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        if identifier == PhoneCoordinator.backgroundID { PhoneCoordinator.shared.setBackgroundCompletion(completionHandler) }
        else if identifier == TodayCoordinator.backgroundID { TodayCoordinator.shared.setBackgroundCompletion(completionHandler) }
        else { completionHandler() }
    }
}

@main
struct WatchMemoryPhoneApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = PhoneCoordinator.shared
    @StateObject private var today = TodayCoordinator.shared
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup { TodayHome().environmentObject(model).environmentObject(today).tint(.blue) }
            .onChange(of: scenePhase, initial: true) { _, state in if state == .active { model.publishWatchConfiguration(); model.scheduleUploads(); today.foreground() } }
    }
}

struct SetupView: View {
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
