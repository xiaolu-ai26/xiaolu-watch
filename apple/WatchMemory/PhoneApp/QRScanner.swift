import AVFoundation
import SwiftUI
import UIKit

struct QRScanner: UIViewControllerRepresentable {
    let found: (String) -> Void
    let failed: (String) -> Void
    func makeUIViewController(context: Context) -> ScannerController {
        let controller = ScannerController(); controller.found = found; controller.failed = failed; return controller
    }
    func updateUIViewController(_ uiViewController: ScannerController, context: Context) {}
    static func dismantleUIViewController(_ controller: ScannerController, coordinator: ()) { controller.stop() }
}

final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var found: ((String) -> Void)?
    var failed: ((String) -> Void)?
    private let capture = AVCaptureSession()
    private let cameraQueue = DispatchQueue(label: "xiaolu.watch.qr")
    private var preview: AVCaptureVideoPreviewLayer?
    private var delivered = false
    private var stopped = false
    override func viewDidLoad() {
        super.viewDidLoad(); view.backgroundColor = .black
        AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
            guard let self else { return }
            if granted { self.cameraQueue.async { self.setup() } }
            else { DispatchQueue.main.async { self.failed?("请允许相机，或使用粘贴连接信息") } }
        }
    }
    private func setup() {
        guard !stopped else { return }
        do {
            guard let device = AVCaptureDevice.default(for: .video) else { throw NSError(domain: "NoCamera", code: 1) }
            let input = try AVCaptureDeviceInput(device: device)
            guard capture.canAddInput(input) else { throw NSError(domain: "NoCameraInput", code: 1) }
            capture.addInput(input)
            let output = AVCaptureMetadataOutput()
            guard capture.canAddOutput(output) else { throw NSError(domain: "NoMetadataOutput", code: 1) }
            capture.addOutput(output); output.setMetadataObjectsDelegate(self, queue: .main); output.metadataObjectTypes = [.qr]
            DispatchQueue.main.async {
                let preview = AVCaptureVideoPreviewLayer(session: self.capture); preview.videoGravity = .resizeAspectFill
                preview.frame = self.view.bounds; self.view.layer.insertSublayer(preview, at: 0); self.preview = preview
            }
            capture.startRunning()
        } catch { DispatchQueue.main.async { self.failed?("相机暂时无法使用，请粘贴连接信息") } }
    }
    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); preview?.frame = view.bounds }
    func stop() { cameraQueue.async { self.stopped = true; if self.capture.isRunning { self.capture.stopRunning() } } }
    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard !delivered, let value = (objects.first as? AVMetadataMachineReadableCodeObject)?.stringValue else { return }
        delivered = true; stop(); found?(value)
    }
}
