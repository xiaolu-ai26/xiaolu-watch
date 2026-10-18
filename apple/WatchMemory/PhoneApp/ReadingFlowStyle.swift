import SwiftUI
import UIKit

enum ReadingMotionBudget {
    static var systemLimited: Bool {
        let info = ProcessInfo.processInfo
        return info.thermalState == .critical
    }
}

enum ReadingPalette {
    static let paper = Color(red: 0.985, green: 0.979, blue: 0.958)
    static let ink = Color(red: 0.025, green: 0.075, blue: 0.14)
    static let cobalt = Color(red: 0.025, green: 0.16, blue: 0.36)
    static let quiet = Color(red: 0.43, green: 0.45, blue: 0.48)
}

/// The full editorial composition is retained in both moving and static states.
struct EditorialPointFlow: View {
    let animate: Bool
    let renderAllowed: Bool
    var body: some View {
        GeometryReader { geometry in EditorialGPUContent(size: geometry.size, animate: animate, renderAllowed: renderAllowed) }
            .allowsHitTesting(false).accessibilityHidden(true)
    }
}

struct ReadingSurface: ViewModifier {
    func body(content: Content) -> some View { content.padding(.vertical, 16).frame(maxWidth: .infinity, alignment: .leading) }
}
extension View {
    func readingSurface() -> some View { modifier(ReadingSurface()) }
    @ViewBuilder func flowSource(_ id: String, in namespace: Namespace.ID, motionDisabled: Bool) -> some View {
        if #available(iOS 18, *), !motionDisabled { self.matchedTransitionSource(id: id, in: namespace) } else { self }
    }
    @ViewBuilder func flowDestination(_ id: String, in namespace: Namespace.ID, motionDisabled: Bool) -> some View {
        if #available(iOS 18, *), !motionDisabled { self.navigationTransition(.zoom(sourceID: id, in: namespace)) } else { self }
    }
}
