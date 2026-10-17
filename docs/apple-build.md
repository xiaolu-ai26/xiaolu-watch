# 编译安装手表 App 并配对

App 不在 App Store 上，需要你用自己的 Apple ID 在 Xcode 里签名，装到自己的 iPhone 和 Apple Watch。手表 App 随 iPhone 伴侣 App 一起分发，但录音和上传都由手表自己完成。

## 准备

- 一台 Mac，安装完整 Xcode（不是只装 Command Line Tools）。Xcode 必须支持你手表当前的 watchOS 版本：手表升级到 watchOS 27 后需要 Xcode 27，而 Xcode 27 要求 macOS 26.6 或更高，详见 [troubleshooting.md](troubleshooting.md#xcode-连不上手表)。以 [Apple 官方 Xcode 系统要求](https://developer.apple.com/xcode/system-requirements/)为准。
- Apple ID：在 Xcode → Settings → Accounts 登录。免费的 Personal Team 可以把 App 签名装到自己的设备；免费签名的有效期和可注册设备数以 Xcode 显示为准，过期后用 Xcode 重新安装即可（同一 Bundle ID 覆盖安装不会清掉手表上的待传录音；**删除 App 会**）。
- XcodeGen：`brew install xcodegen`。
- 已与这块手表配对的 iPhone，数据线一根。
- 部署目标是 iOS 17 / watchOS 10，但作者只在 Apple Watch Series 9（watchOS 27）上实测过。

## 1. 填签名信息

```bash
python3 scripts/setup.py --apple-only
```

它会问你的 Team ID 和一个唯一的 Bundle ID（默认帮你生成一个随机后缀的），写入 `apple/WatchMemory/Config/Local.xcconfig`（已被 git 忽略）。手表 App 的 Bundle ID 自动是 `<你的 Bundle ID>.watchkitapp`。也可以复制 `Config/Local.xcconfig.example` 手工填写；命令行构建时还可以用 `WATCH_MEMORY_DEVELOPMENT_TEAM=<TeamID>` 覆盖。

## 2. 生成 Xcode 工程

```bash
apple/scripts/generate-project.sh
```

生成 `apple/WatchMemory/WatchMemory.xcodeproj`（不进 git，随时可重新生成）。

## 3. 安装到设备

用 Xcode 打开 `apple/WatchMemory/WatchMemory.xcodeproj`：

1. 先让 Xcode 认到设备：Window → Devices and Simulators（Xcode 27 是独立的 Devices 窗口）里能看到 iPhone 和手表，且状态可用。看不到手表时按 [troubleshooting.md](troubleshooting.md#xcode-连不上手表) 处理，这是最常见的卡点。
2. 选 scheme `WatchMemoryWatch`，运行目标选你的手表，点 Run。签名用 Automatic，Xcode 会自动把这块手表注册进开发描述文件。
3. 需要 iPhone 伴侣 App 时，选 scheme `WatchMemoryPhone` + 你的 iPhone 再 Run 一次。
4. 在手表上打开“小鹿录音”，第一次点开始时允许麦克风。

安装不需要先下载 watchOS 模拟器运行时；作者的真机安装是在模拟器运行时下载完成之前成功的。

命令行方式（可选）：

```bash
xcodebuild -project apple/WatchMemory/WatchMemory.xcodeproj -scheme WatchMemoryPhone \
  -destination 'generic/platform=iOS' -allowProvisioningUpdates build
xcrun devicectl list devices
xcrun devicectl device install app --device <设备标识> <构建出的 .app 路径>
```

## 4. 首次配对：把手表连到你的接收端

前提：接收端的 receiver、ingress 服务和公网 HTTPS 入口都已在运行，`python3 scripts/doctor.py` 通过。

在接收端生成一次性配对码（10 分钟有效，只能被一块手表认领；服务器只保存它的哈希）：

```bash
python3 scripts/pair.py
```

然后任选一种方式交给手表：

**A. 零输入投递（推荐，需要装 Xcode 的 Mac）**

```bash
python3 scripts/deliver_onboarding.py
```

它用 Xcode 的 `devicectl` 把只含 HTTPS 地址和一次性码的 `onboarding.json` 放进手表 App 自己的数据目录，并启动 App。App 校验文件、把码存进钥匙串、删除文件，再通过 HTTPS 领取长期配置。请抬腕让 App 保持前台约 1 分钟，直到显示“处理电脑已连接”。

- 接收端就是这台 Mac 时，脚本自动读取 `data/receiver/private/onboarding.json`。
- 接收端是 Windows 时，把那台电脑上的 `data/receiver/private/onboarding.json` 拷过来用 `--file`，或打开配对卡（`watch-pairing-card.txt`）后用 `--endpoint https://... --code XXXX-XXXX-XXXX-XXXX`。
- 前提：手表 App 已经打开过一次，手表已解锁，且在 Xcode 里可见。
- 这个投递机制在作者设备上验证过；脚本本身未在干净环境实测。

**B. 在手表上输入**

手表“小鹿录音 → 连接处理电脑”，输入 HTTPS 根地址和 16 位配对码（可带连字符）。已实现并通过代码审查，未在真机上实测。

**C. iPhone 扫码（不推荐）**

iPhone App 支持扫描/粘贴完整连接配置再转发给手表，但作者测试时配置没有送达手表，所以没有提供生成二维码的脚本。

配对只需一次。接收电脑重启、换 IP，只要公网域名和配对密钥不变，就不用重新配对。手表的第一段录音拿到接收端的签名回执，才算真正连通（health 通了不算）。

## 日常使用

1. 打开“小鹿录音”，点“开始记录”。可以回到表盘、熄屏，App 在后台继续录，约 60 秒封一段。
2. 有网络时手表直接上传；离线时录音留在手表上，恢复后自动补传。蜂窝自动同步默认关闭，需要时在 App 里打开（要求手表本身有可用的蜂窝网络）。
3. “暂停/继续/结束”随时可点。系统音频中断后，只有系统允许、且你没手动暂停时才会自动恢复，否则提示你点继续。
4. 强制退出 App 或手表重启后不会自动续录，需要你重新点开始；之前的片段会恢复并继续上传。
5. 电量低于 5% 或手表剩余空间不足时会保存并停止。

## 运行 Swift 核心检查

```bash
apple/scripts/run-checks.sh
```

在 macOS 上编译并运行共享核心的检查：分段状态与原子 manifest、回执校验、密封协议（Swift CryptoKit 与 Python cryptography 用公开夹具互相加解密、互验回执）、后台重试调度。需要 Python 环境里装有 `cryptography`。这些检查不能替代真机上的后台录音、上传和耗电验证。
