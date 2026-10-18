import SwiftUI
import MetalKit
import QuartzCore
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

struct EditorialFlowUniforms {
    var viewport: SIMD2<Float>
    var time: Float
    var padding: Float = 0
}

/// A single compile and pipeline per view lifetime. No recording/health state enters the renderer.
final class EditorialFlowRenderer: NSObject, MTKViewDelegate {
    static let particleCount = 50_008
    let device: MTLDevice
    let queue: MTLCommandQueue
    let pipeline: MTLRenderPipelineState
    let fieldPipeline: MTLRenderPipelineState
    let density: MTLTexture
    private(set) var animating = false
    private(set) var renderAllowed = false
    private var staticFrameValid = false
    private var staticSize: CGSize = .zero
    private var elapsed: CFTimeInterval = 0
    private var lastDraw: CFTimeInterval?
    private(set) var presentedFrames = 0
    private(set) var frameIntervals: [Double] = []
    init(device: MTLDevice, densityURL: URL? = Bundle.main.url(forResource: "EditorialFlowDensity", withExtension: "png")) throws {
        self.device = device
        guard let queue = device.makeCommandQueue() else { throw FlowGPUError.unavailable }
        self.queue = queue
        guard let densityURL else { throw FlowGPUError.unavailable }
        density = try MTKTextureLoader(device: device).newTexture(URL: densityURL, options: [.SRGB: false, .origin: MTKTextureLoader.Origin.topLeft, .generateMipmaps: true])
        let library = try device.makeLibrary(source: EditorialFlowShader.source, options: nil)
        let descriptor = MTLRenderPipelineDescriptor()
        descriptor.vertexFunction = library.makeFunction(name: "flowVertex")
        descriptor.fragmentFunction = library.makeFunction(name: "flowFragment")
        descriptor.colorAttachments[0].pixelFormat = .bgra8Unorm
        descriptor.colorAttachments[0].isBlendingEnabled = true
        descriptor.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
        descriptor.colorAttachments[0].destinationRGBBlendFactor = .oneMinusSourceAlpha
        descriptor.colorAttachments[0].sourceAlphaBlendFactor = .one
        descriptor.colorAttachments[0].destinationAlphaBlendFactor = .oneMinusSourceAlpha
        pipeline = try device.makeRenderPipelineState(descriptor: descriptor)
        descriptor.vertexFunction = library.makeFunction(name: "flowFieldVertex")
        descriptor.fragmentFunction = library.makeFunction(name: "flowFieldFragment")
        fieldPipeline = try device.makeRenderPipelineState(descriptor: descriptor)
        super.init()
    }
    func configure(_ view: MTKView, animate: Bool, renderAllowed: Bool) {
        let wasAllowed = self.renderAllowed, wasAnimating = animating
        self.renderAllowed = renderAllowed
        view.device = device; view.delegate = self
        view.colorPixelFormat = .bgra8Unorm
        view.clearColor = MTLClearColorMake(0, 0, 0, 0)
        view.framebufferOnly = true
        view.preferredFramesPerSecond = 60
        animating = animate && renderAllowed
        if animating != wasAnimating || renderAllowed != wasAllowed { lastDraw = nil }
        view.isPaused = !animating
        view.enableSetNeedsDisplay = renderAllowed && !animating
        guard renderAllowed else { return }
        if animating { staticFrameValid = false }
        else if !staticFrameValid || !wasAllowed || wasAnimating || staticSize != view.drawableSize { view.draw() }
    }
    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {
        staticFrameValid = false
        if renderAllowed && !animating { view.draw() }
    }
    func encode(_ encoder: MTLRenderCommandEncoder, size: CGSize, time: Float) {
        guard renderAllowed else { return }
        var uniforms = EditorialFlowUniforms(viewport: SIMD2(Float(size.width), Float(size.height)), time: time)
        encoder.setRenderPipelineState(fieldPipeline)
        encoder.setFragmentBytes(&uniforms, length: MemoryLayout<EditorialFlowUniforms>.stride, index: 0)
        encoder.setFragmentTexture(density, index: 0)
        encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        encoder.setRenderPipelineState(pipeline)
        encoder.setVertexTexture(density, index: 0)
        encoder.setVertexBytes(&uniforms, length: MemoryLayout<EditorialFlowUniforms>.stride, index: 0)
        encoder.drawPrimitives(type: .point, vertexStart: 0, vertexCount: Self.particleCount)
    }
    func draw(in view: MTKView) {
        guard renderAllowed else { return }
        guard let pass = view.currentRenderPassDescriptor, let drawable = view.currentDrawable,
              let buffer = queue.makeCommandBuffer(), let encoder = buffer.makeRenderCommandEncoder(descriptor: pass) else { return }
        let now = CACurrentMediaTime()
        if animating, let prior = lastDraw {
            let interval = now - prior
            elapsed += min(0.1, max(0, interval))
            if interval > 0 && frameIntervals.count < 240 { frameIntervals.append(interval) }
        }
        lastDraw = now
        encode(encoder, size: view.drawableSize, time: Float(elapsed))
        encoder.endEncoding(); buffer.present(drawable); buffer.commit()
        presentedFrames += 1
        if !animating { staticFrameValid = true; staticSize = view.drawableSize }
    }
    enum FlowGPUError: Error { case unavailable }
}

@MainActor private final class EditorialGPUModel: ObservableObject {
    @Published private(set) var renderer: EditorialFlowRenderer?
    private var attempted = false
    func prepareIfAllowed(_ allowed: Bool) {
        // Even texture/mipmap initialization is deferred until the page is renderable.
        guard allowed && !attempted else { return }; attempted = true
        if let device = MTLCreateSystemDefaultDevice() { renderer = try? EditorialFlowRenderer(device: device) }
    }
}

struct EditorialGPUContent: View {
    let size: CGSize
    let animate: Bool
    let renderAllowed: Bool
    @StateObject private var gpu = EditorialGPUModel()
    var body: some View {
        Group {
            if let renderer = gpu.renderer {
                EditorialGPUSurface(renderer: renderer, animate: animate, renderAllowed: renderAllowed)
                    .frame(width: size.width, height: size.height)
            } else {
                Image("EditorialFlowDensity").resizable().frame(width: size.width, height: size.height)
            }
        }.onChange(of: renderAllowed, initial: true) { _, allowed in gpu.prepareIfAllowed(allowed) }
    }
}

#if os(iOS)
private struct EditorialGPUSurface: UIViewRepresentable {
    let renderer: EditorialFlowRenderer; let animate: Bool; let renderAllowed: Bool
    func makeUIView(context: Context) -> MTKView {
        let view = MTKView(frame: .zero, device: renderer.device)
        view.backgroundColor = .clear; view.isOpaque = false
        renderer.configure(view, animate: animate, renderAllowed: renderAllowed); return view
    }
    func updateUIView(_ view: MTKView, context: Context) { renderer.configure(view, animate: animate, renderAllowed: renderAllowed) }
    static func dismantleUIView(_ view: MTKView, coordinator: ()) { if let renderer = view.delegate as? EditorialFlowRenderer { renderer.configure(view, animate: false, renderAllowed: false) }; view.isPaused = true; view.delegate = nil }
}
#elseif os(macOS)
private struct EditorialGPUSurface: NSViewRepresentable {
    let renderer: EditorialFlowRenderer; let animate: Bool; let renderAllowed: Bool
    func makeNSView(context: Context) -> MTKView {
        let view = MTKView(frame: .zero, device: renderer.device)
        view.layer?.isOpaque = false
        renderer.configure(view, animate: animate, renderAllowed: renderAllowed); return view
    }
    func updateNSView(_ view: MTKView, context: Context) { renderer.configure(view, animate: animate, renderAllowed: renderAllowed) }
    static func dismantleNSView(_ view: MTKView, coordinator: ()) { if let renderer = view.delegate as? EditorialFlowRenderer { renderer.configure(view, animate: false, renderAllowed: false) }; view.isPaused = true; view.delegate = nil }
}
#endif
