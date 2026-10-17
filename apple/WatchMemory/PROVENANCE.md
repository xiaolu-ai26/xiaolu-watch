# 源码与设计来源

本目录 Swift 代码为本项目独立实现，没有复制第三方实现或将第三方 App 改名。使用 Apple 公开 AVFAudio、WatchConnectivity、URLSession、Keychain 和 SwiftUI API。

调研过的设计线索：

- Owl：https://github.com/OwlAIProject/Owl —— 连续采集、分段与离线队列思路。未复制其源码；尤其没有沿用 HTTP 非 2xx 时仍删文件或启动删除未完成录音的行为。
- Teika：https://github.com/gustavscirulis/teika —— Watch→iPhone 的离线录音交接思路。未复制 GPL 源码。
- Nomily：https://github.com/Nomily-Ai/nomily-ios —— 录音文件持久接收确认思路。未复制其自定义许可证源码。

App 图标是本项目直接按蓝白麦克风几何形状生成的 PNG，不含第三方图像、照片或字体。配对密钥、设备标识、签名凭据都不在仓库里。

Apple API 资料：

- https://developer.apple.com/documentation/avfaudio/avaudiofile/init(forwriting:settings:commonformat:interleaved:)
- https://developer.apple.com/documentation/xcode/configuring-background-execution-modes
- https://developer.apple.com/documentation/watchkit/enabling-background-sessions
- https://developer.apple.com/documentation/avfaudio/avaudiosession/setallowhapticsandsystemsoundsduringrecording(_:)

文档用于实现依据，不替代目标 SDK 编译与真机验收。
