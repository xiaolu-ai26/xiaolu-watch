# 手表 App 设计说明

原生 watchOS 录音 App + iPhone 伴侣 App。手表拥有录音文件和上传队列，自己直接上传到你配置的 HTTPS 接收端；iPhone 只负责安装和可选的配置/备用中转（默认关闭）。编译安装见 [docs/apple-build.md](../../docs/apple-build.md)。

## 结构

- `WatchApp/`：SwiftUI 界面、`WatchRecorder`（AVAudioEngine 采集、分段、编码、中断恢复）、`WatchDirectUploader`（手表直传、后台任务、回执校验）、`WatchPairingSetup`（一次性码认领配置）。
- `PhoneApp/`：伴侣 App，扫码/粘贴配置、可选的手机备用中转（默认关闭）。`BackgroundUploadScheduling.swift` 与 `KeychainConfiguration.swift` 也编译进手表 App。
- `Sources/WatchMemoryCore/`：无录音/网络副作用的共享核心：原子 manifest 与状态机、v2 密封与回执校验、上传尝试日志、配对策略、诊断。
- `Tests/`：`CoreChecks`、`WireV2Checks`、`SchedulingChecks` 可在 macOS 上直接 `swift run`；`WatchMemoryCoreTests` 为 XCTest 版本；`Fixtures/` 是明确标注的公开确定性测试夹具（含公开测试密钥，绝不能用于真实配对）。
- `project.yml`：XcodeGen 工程定义；签名信息来自 `Config/Local.xcconfig`（不进 git）。

## 录音与分段

- 用户在前台点开始并允许麦克风后，App 使用 audio 后台模式。音频会话和输入 tap 在约 60 秒换段时保持运行，只轮换文件，不每分钟重启麦克风。
- 采集为 16 kHz、单声道、16-bit PCM CAF；每个封闭片段在独立队列里用系统 AAC 编码成约 32 kbps 的 M4A（约 14.4 MB/小时），校验并写入持久 manifest 后再上传。编码失败时保留并上传 CAF（约 115 MB/小时），界面会提示占用较多。
- 原始 CAF 保留到收到接收端确认；崩溃后重新打开会尝试恢复片段，无法解析的片段保留原文件并显示待恢复，不会启动时删除。
- 音频缓冲有上限（32 个待写 buffer），落盘跟不上时停止并提示末尾可能有缺口；剩余空间低于 64 MiB 或电量低于 5% 时保存并停止。
- 系统中断后，只有系统建议恢复、用户想继续且没有手动暂停时才自动恢复；否则提示用户点继续。
- 强制退出或重启后不会自动开始录音；已有片段会恢复并继续上传，新录音需要用户再点开始。没有每天定时开录。
- 请求 `mixWithOthers` 与 `setAllowHapticsAndSystemSoundsDuringRecording(true)`，尽量保留系统声音和震动；电话、Siri、AirPods 场景的实际表现未经真机验证。

## 上传与确认

- 每段带稳定的 `chunk_id`、`session_id`、单调 `sequence`、采集时间（UTC）、时长、字节数和 SHA-256。
- 上传使用 [v2 密封协议](../../docs/wire-v2.md)：`PUT /v2/chunks/<chunk UUID>`，body 为 `nonce || AES-256-GCM 密文 || tag`，元数据和音频都在密文内；每次新尝试换新的 challenge 和 nonce。
- 录音进行中，对新封好的片段开一个普通 URLSession 上传槽，尽快送出；后台 URLSession 任务作为兜底，由系统调度（可能延迟）。停止或中断时先建立后台接续任务再取消快路。
- 只有收到接收端回执、常数时间验证 HMAC（绑定 challenge、完整密文摘要、HTTP 状态和原始响应字节）、并核对事件身份和 SHA 后，片段才算“已送达电脑”。缺 MAC、错 MAC、旧回执、改过的 status/body 都不会转移保管状态。
- “已送达”只代表接收端已持久保存，不代表已转写或写入笔记。

## 保留策略

- 手表：只有经过验证的接收端确认的片段，才在确认 24 小时后或剩余空间低于 128 MiB 时回收编码文件和原始 CAF；未确认的片段永不自动删除。
- manifest 活动条目上限 8192；已确认且已回收的元数据先追加到归档文件再移出活动列表。
- 删除 App 会删除手表上所有本地数据，包括待传录音。

## 首次配置

- 长期配置（HTTPS endpoint + 配对密钥）只保存在手表钥匙串（`AfterFirstUnlockThisDeviceOnly`），不写 UserDefaults、URL、日志或源码。
- 获取方式：接收端生成 80 bit 一次性码（10 分钟）；手表用 `POST /v2/pair/claim` 认领。码可以由 Mac 上的 Xcode 工具以 `onboarding.json` 投递进 App 数据目录（零输入），也可以在手表上输入。App 先把码和随机 attempt ID 存进钥匙串再发请求，响应丢失可用同一 ID 重取；长期配置保存成功后才删掉短期码。
- 认领请求：禁止重定向、缓存、cookie；响应上限 8 KiB；返回的 endpoint 必须与请求的主机和端口一致。
- 第一个 MAC 正确的录音回执才证明接收端真的持有密钥。
