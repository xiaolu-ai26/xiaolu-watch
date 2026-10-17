# 排障

先运行 `python3 scripts/doctor.py`，它会逐项检查并给出修复提示。下面是作者实际踩过的坑和常见问题。

## Xcode 连不上手表

**症状**

- Xcode 的设备窗口和运行目标里只有 iPhone，没有手表；`xcrun devicectl list devices` 也看不到手表。
- 手表上找不到“开发者模式”开关。
- 重启手表、重插 iPhone 数据线、在手表上“信任这台电脑”都试过，仍然不行。
- 如果之前用别的方式装过，手表可能拒绝安装并提示“没有有效的预置描述文件”（`MIInstallerErrorDomain 13 / 0xe8008015`）：签名描述文件里没有注册这块手表，根源还是 Xcode 没有和手表建立开发连接。

**作者遇到的原因**

手表升级到 watchOS 27 后，旧版 Xcode（26.x）始终发现不了手表。Xcode 27 的设备窗口新增了 iOS 27 / watchOS 27 的网络配对流程（Pair Nearby Device），配对手表更可靠；但 Xcode 27 正式版要求 macOS 26.6 或更高，作者的 Mac 当时是 macOS 26.2，装不了。

**解决步骤（作者实际走通的顺序）**

1. 升级 macOS 到满足 Xcode 27 要求的版本（26.6 或更高，以 [Apple 官方要求](https://developer.apple.com/xcode/system-requirements/)为准）。稳定版即可，不需要装 beta 系统。
2. 安装 Xcode 27.x 正式版，首次启动时装好必需组件。Xcode 解包后约 10 GB 以上，设备符号缓存还可能再占十几 GB，提前留足空间。
3. iPhone 用数据线连 Mac，解锁并信任。
4. 在 iPhone 的开发者模式（设置 → 隐私与安全性 → 开发者模式，及相关开发者设置）里删除这台 Mac 的旧开发记录，然后在 Xcode 27 的设备窗口里对 iPhone 重新点 Pair 完成配对。中途验证码弹窗可能自己消失，以最终显示已配对为准。
5. 这之后，手表的 设置 → 隐私与安全性 → **开发者模式** 开关才出现。打开它，按提示重启手表，重启后确认开启。
6. 在 Xcode 设备窗口点 “+” → **Pair Nearby Device**，按提示完成手表配对。
7. 选手表作为运行目标构建安装。Xcode 自动签名会把手表注册进描述文件（免费 Personal Team 也可以）。

**不需要做的事**：不用抹掉手表，不用解除 iPhone 与手表的配对，不用为真机安装下载 watchOS 模拟器运行时。

## iPhone 上已有 VPN，和 Tailscale 冲突

iPhone 同一时间只能开一个 VPN。作者手机上一直开着别的 VPN 客户端，没法同时开 Tailscale，所以最初“手机/手表经 Tailscale 私网连电脑”的方案行不通。

解决：在接收端电脑上用 **Tailscale Funnel** 提供公网 HTTPS（`tailscale funnel --bg --https=443 http://127.0.0.1:18438`）。手表和 iPhone 走普通 HTTPS，都不用装或开 Tailscale，手机继续用原来的 VPN。安全性不靠私网：音频在 HTTPS 之外还有一层 AES-GCM 加密，回执有签名，入口只开放上传和配对两个接口。

## 手表一直显示待传 / 上传失败

| 现象 | 查什么 | 原则 |
| --- | --- | --- |
| 显示未连接处理电脑 | 手表是否完成配对（App 里“连接处理电脑”状态） | 不要反复配对或删 App，删 App 会丢待传录音 |
| 上传返回 503 / 媒体检查失败 | 接收器能否执行 `config.json` 里的 ffprobe 绝对路径；Windows 上接收器以 SYSTEM 运行，PATH 和你不同 | 作者踩过：SYSTEM 找不到 ffprobe，修好后手表自动补传，原音没丢 |
| 返回 507 | 接收端原音预算或磁盘剩余不足 | 腾出空间或调大 `max_local_bytes` 后重启接收器 |
| 公网 health 正常但录音没到 | `data/logs/receiver.log`、`ingress.log`；手表网络 | health 只说明入口通，不证明配对和保存 |
| 有网但很久才传 | 手表后台网络由系统调度 | 录音进行中会尽快直传；停止后靠系统后台任务补传，可能延迟 |
| 只用蜂窝时不传 | App 里“允许蜂窝自动同步”默认关闭 | 需要手表本身有可用蜂窝网络 |

## 配对失败

- 配对码 10 分钟过期，且只能被第一块认领的手表使用。重新运行 `python3 scripts/pair.py` 生成新码。
- 认领走的是 `POST /v2/pair/claim`，需要 ingress 服务在运行（`service.py ingress` 默认启用该接口）和公网 HTTPS 可达。
- 频繁失败会触发限速（429），等一两分钟再试。
- 零输入投递失败：确认手表 App 打开过一次、手表已解锁、Xcode 设备窗口里手表可用；Bundle ID 要和 `Local.xcconfig` 一致。

## 已送达但没有转写

- `data/logs/pipeline.log` 和 `python3 processor/pipeline.py status`（不含转写原文）。
- 模型：`data/models/<模型>/` 里要有 `model.bin`、`config.json`、`tokenizer.json`，没有就 `python3 scripts/setup.py --download-model`。
- `faster-whisper` 没装：`pip install -r requirements-asr.txt`（用 `.venv` 里的 pip）。
- 显卡报 cuBLAS/cuDNN 找不到：见 [deployment.md](deployment.md#nvidia-显卡)，或先改成 CPU。
- 单段转写超过 1 小时会被强制结束（防止卡死），服务会自动重启后重试。
- 新版 PyAV 改过 faster-whisper 解码用到的参数；本项目已改为由 FFmpeg 解码后直接把 PCM 交给模型，不依赖 PyAV 解码。

## 转写有错字或是繁体

机器转写可能有同音字、专名错误。小模型（tiny/base/small）更明显，Whisper 对中文有时会输出繁体。可以换更大的模型，或在 `data/settings.json` 把 `asr.language` 设为 `zh`，然后重启 pipeline。笔记开头会标明“机器转写待核对”，不识别说话人。

## 模型下载失败

- 需要能访问 Hugging Face。国内网络可以先设置 `HF_ENDPOINT` 为可用镜像再运行 `python3 scripts/setup.py --download-model`。
- 报 `Permission denied` 或 `File exists`，并且路径指向一个不存在或只读的磁盘：你的 Hugging Face 缓存目录（`HF_HOME` 或 `~/.cache/huggingface`）指到了未挂载的位置。临时指定一个可写目录再下载，例如 `HF_HOME=/tmp/hf python3 scripts/setup.py --download-model`。

## Windows 接收端写好了笔记，Mac 的 Obsidian 里没有

- Mac 要开机、联网，并已加载 `...sync.plist`；拉取间隔约 120 秒。Mac 关机时笔记留在 Windows，开机后补齐。
- `ssh <别名> echo ok` 要不需要密码。Windows 管理员账户的公钥必须放在 `C:\ProgramData\ssh\administrators_authorized_keys`。
- outbox 路径要和 Windows 向导打印的一致。查看 `data/state/mac-sync.json`：`conflicts_preserved` 里是你在 Mac 上改过、和 Windows 版本不同的笔记，脚本会保留你的版本，不覆盖。

## macOS 后台服务写不进 Vault

Vault 在 iCloud Drive 或“文稿”里时，launchd 启动的 python 可能被系统拦截（日志 `Operation not permitted`）。到 系统设置 → 隐私与安全性 → 文件与文件夹 / 完全磁盘访问权限，给该 python 授权后重启服务。

## 接收电脑重启后手表连不上

检查服务是否自启（`doctor.py`）、Funnel 或反向代理是否恢复、证书是否有效。域名和配对密钥没变时不需要重新配对。不要通过重新生成密钥来排障，那会让手表必须重新配对。

## 飞书没有写入

- `lark-cli` 要以运行 pipeline 的同一个用户登录；Windows 上 pipeline 不能以 SYSTEM 运行。
- 提示“outcome unknown”时，是网络中断导致不确定之前那次创建/追加有没有成功，为避免重复写入而停下。先去飞书核对，再用 `python3 processor/pipeline.py recover-doc <job> <document_id>` 关联已有文档。
