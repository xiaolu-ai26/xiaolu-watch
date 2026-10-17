# 接收端部署

本文说明两种接收端的部署细节。日常安装用 `python3 scripts/setup.py` 即可，它会生成这里提到的所有配置和服务文件；本文用于理解、排障和手动调整。

## 组成和端口

| 组件 | 进程入口 | 监听 | 作用 |
| --- | --- | --- | --- |
| 接收器 | `receiver/receiver.py serve` | `127.0.0.1:18437` | 解密验证 v2 上传、检查音频、持久写盘、回签名回执 |
| HTTPS 入口上游 | `receiver/https_ingress.py` | `127.0.0.1:18438` | 只转发 `PUT /v2/chunks/*`、`POST /v2/pair/claim`、`GET /health`，有并发、长度、速率和超时限制 |
| 公网 HTTPS | Tailscale Funnel 或你的反向代理 | 443 | 终止 TLS，转发到 `127.0.0.1:18438` |
| 处理流水线 | `processor/pipeline.py` | 无 | 本地转写、写 Markdown、可选飞书 |
| Mac 拉取（仅 Windows 方案） | `processor/sync_notes.py` | 无 | Mac 定时经 SSH 拉取 Windows 上的笔记 |

后台服务统一通过 `scripts/service.py <receiver|ingress|pipeline|sync>` 启动，日志写在 `data/logs/<组件>.log`。日志不包含转写原文、密钥或配对码。

接收器和入口都只绑定 loopback，不要把 18437/18438 直接暴露到公网或局域网。

## 运行数据（`data/`，已被 git 忽略）

```text
data/settings.json                 setup 生成的非秘密设置（平台、笔记目录、模型、时区等）
data/receiver/private/config.json  接收器配置，含配对密钥（权限 600）
data/receiver/private/             配对码哈希库、事件库、私有配对卡、onboarding.json
data/receiver/objects|inbox|sessions|receipts  原音、待处理队列、每段事件与回执
data/asr/                          转写队列、结果（outbox）和已转写原音归档（archive）
data/receipts/<日期>/<sha>/         每段的处理回执（转写、飞书、笔记状态）
data/state/pipeline.sqlite3        流水线任务库
data/notes-outbox/                 Windows“先存后拉”方案的笔记和 manifest.json
data/models/<模型名>/               本地 faster-whisper 模型
data/services/                     生成的 launchd plist 或 Windows 计划任务脚本
data/logs/                         服务日志
```

想把数据放到别的盘：所有脚本都接受 `--home <目录>`，也可以设置环境变量 `XIAOLU_WATCH_HOME`。

## 方案 A：常开的 Windows 电脑（作者实测方案）

1. 安装 [Python 3.10+](https://www.python.org/downloads/windows/)（勾选 Add to PATH）、FFmpeg（`winget install Gyan.FFmpeg`）、Git、[Tailscale](https://tailscale.com/download)（选 Funnel 时）。
2. 有 NVIDIA 显卡时，先装好驱动并按下文准备 GPU 运行库。
3. `git clone https://github.com/xiaolu-ai26/xiaolu-watch.git`，进入目录运行 `py -3 scripts\setup.py`。
4. 笔记位置三选一：写进这台电脑上的 Obsidian Vault、写进普通文件夹，或“先存在接收端，Mac 开机后再拉进 Mac 的 Obsidian”（作者实测方案）。
5. 以管理员身份打开 PowerShell，运行向导打印的 `install-windows-tasks.ps1`。它创建三个计划任务：
   - `XiaoluWatch-receiver`、`XiaoluWatch-ingress`：开机时以 SYSTEM 运行，不需要登录，也不需要你的凭据。
   - `XiaoluWatch-pipeline`：以你本人身份在登录后运行，因为模型缓存和可选的 `lark-cli` 登录都在你的用户目录。如果希望重启后无人登录也能转写，可在“任务计划程序”里把它改为“不管用户是否登录都要运行”。
   - 该脚本未在干净的 Windows 上实测。作者实测环境中的任务是手工建立的，账户分配与上面相同。
6. 运行一次 Funnel 命令（见下文），然后 `py -3 scripts\doctor.py`。

为什么配置里写 ffprobe 的绝对路径：SYSTEM 账户的 PATH 和你的不同。作者部署时接收器曾因 SYSTEM 找不到 ffprobe，对每次上传返回 503（手表会一直重试、原音不丢）。向导会把检测到的绝对路径写进 `config.json`，接收器启动时先检查它能否运行。

### Windows 接收端 + Mac 拉取笔记

Windows 上的流水线把笔记写进 `data/notes-outbox/`，并维护一份 `manifest.json`（相对路径、SHA-256、字节数）。Mac 开机联网后，每 120 秒拉取一次新笔记：校验哈希和长度，只写入你指定的 Vault 子文件夹，不跟随符号链接，不覆盖你改过的笔记（会记为冲突并保留你的版本）。Mac 关机期间笔记一直留在 Windows 上，下次开机自动补齐。

在 Windows 上启用 OpenSSH 服务器（管理员 PowerShell）：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

把 Mac 的公钥（`~/.ssh/id_ed25519.pub`）加入 Windows：普通用户放 `C:\Users\<你>\.ssh\authorized_keys`；**如果该账户是管理员，Windows 只读取 `C:\ProgramData\ssh\administrators_authorized_keys`**（并要求该文件只有 Administrators 和 SYSTEM 有权限），这是最常见的“密钥明明加了还要密码”的原因。

在 Mac 的 `~/.ssh/config` 加一个别名，确认 `ssh <别名> echo ok` 不再要密码：

```text
Host my-windows
    HostName <Windows 的局域网地址或 Tailscale 名称>
    User <Windows 用户名>
    IdentityFile ~/.ssh/id_ed25519
```

然后在 Mac 上 `python3 scripts/setup.py`，选“只在这台 Mac 上拉取笔记”，SSH 主机填别名，outbox 路径填 Windows 向导结束时打印的路径（形如 `C:/Users/<你>/xiaolu-watch/data/notes-outbox`）。按提示加载 `...sync.plist`。

## 方案 B：常开的 Mac

**未经真机验收**：接收器、入口和流水线代码与 Windows 方案相同，作者在 Mac 上用模拟的加密上传跑通了“接收 → 本地转写 → 写入 Vault”，但没有用真手表直传 Mac 做过验收。

1. `brew install ffmpeg python`，安装 Tailscale（选 Funnel 时）。
2. `python3 scripts/setup.py`，笔记位置选你的 Obsidian Vault。
3. 按提示 `launchctl bootstrap gui/$(id -u) ...` 加载 receiver、ingress、pipeline 三个 LaunchAgent。它们在你登录后运行，退出会自动重启。
4. 防止睡眠：系统设置 → 电池（或节能）→ 接电源时防止自动睡眠。合上盖子的笔记本会睡眠，不适合当接收端。
5. 如果 Vault 在 iCloud Drive 或“文稿”里，后台的 python 第一次写入时 macOS 可能拒绝（日志里是 `Operation not permitted`）。到 系统设置 → 隐私与安全性 → 文件与文件夹（或完全磁盘访问权限）给对应的 python 授权。
6. faster-whisper 在 Mac 上只用 CPU（包括 Apple Silicon），向导默认推荐 small 模型。录音积压时转写会排队，原音不受影响。

## 公网 HTTPS 入口

### Tailscale Funnel（推荐）

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:18438
```

- 只需运行一次，`--bg` 会让配置在重启后保持。首次运行可能提示你去 Tailscale 管理后台允许 Funnel。
- 地址是 `https://<机器名>.<tailnet>.ts.net`，证书由 Tailscale 自动签发，手表用系统默认证书校验即可。
- 手表和 iPhone 都**不需要**装或开 Tailscale。iPhone 同一时间只能开一个 VPN，若你已经在用别的 VPN 客户端，正好用 Funnel 避开冲突（见 [troubleshooting.md](troubleshooting.md)）。
- 查看：`tailscale funnel status`。只关掉本项目的入口：`tailscale funnel --https=443 off`（不要用 `reset`，它会清掉你所有的 serve/funnel 配置）。

### 自己的域名

任何能提供受信任证书的反向代理都可以，只要把 HTTPS 转发到 `http://127.0.0.1:18438`。Caddy 示例：

```text
rec.example.com {
    reverse_proxy 127.0.0.1:18438
}
```

不要让代理直接指向 18437，不要开目录浏览，不要要求手表关闭证书校验。入口的速率限制按连接对端计算，经过代理后对端都是本机，所以限制是全局聚合的（更严格），不会信任 `X-Forwarded-For`。

接收电脑重启后，只要域名和配对密钥不变，手表不需要重新配对。用局域网裸 IP 当地址不推荐：DHCP 会变，手表出门也连不上。

## NVIDIA 显卡

faster-whisper 在 GPU 上需要 CUDA 12 的 cuBLAS 和 cuDNN 9 运行库，具体安装方式以 [faster-whisper 官方说明](https://github.com/SYSTRAN/faster-whisper#gpu) 为准。作者的 Windows 环境里，这些 DLL 来自已安装的带 CUDA 的 PyTorch；worker 在 Windows + CUDA 时会自动把 `torch\lib` 加入 DLL 搜索路径。作者实测：RTX 3060 6GB 笔记本显卡，`large-v3-turbo` + `int8_float16`。

显存较小或报错时，可以在 `data/settings.json` 的 `asr` 里改 `compute_type`（如 `int8`）或换小模型，再重启 pipeline 服务。没有显卡时设 `"device": "cpu", "compute_type": "int8"`。

## 飞书（可选，默认关闭）

在 `setup.py` 里选开启，或把 `data/settings.json` 的 `feishu.enabled` 改为 `true` 后重启 pipeline。需要：

1. `npm install -g @larksuite/cli`，然后以运行 pipeline 的同一个用户执行 `lark-cli auth login`。不要跨机器、跨用户复制 token。
2. 每天一篇“日期_录音日汇总”文档，每个有声片段追加一段，带 `record_end: <sha>` 标记；写入后会回读核对。
3. 飞书修订会单向保存为 `<笔记目录>/飞书修订快照/` 下的 Markdown，不覆盖你的笔记。
4. 网络中断导致“不知道创建/追加是否成功”时，该段会停在失败状态并提示先核对，避免重复写入。确认后可用 `python3 processor/pipeline.py recover-doc <job> <document_id>` 关联已有文档。

飞书失败只影响该段的飞书和之后的笔记步骤，原音和转写结果都保留，修好后自动重试。

## 存储与保留

- 每个片段在接收端保留原音（`objects/` 和待处理队列 `inbox/`），转写后在 `data/asr/archive/` 再留一份。按手表 AAC 约 14 MB/小时估算，接收端约 30–45 MB/小时。
- `config.json` 的 `max_local_bytes`（向导里填的 GB）限制 `objects`、`inbox`、`tmp` 的总量；磁盘剩余低于 2 GB 也会拒收。超出时返回 507、不确认上传，录音留在手表上，直到你腾出空间。
- 公开版不做任何自动删除（`gc_enabled` 必须为 `false`），也暂未提供清理工具。需要清理时先停服务、做好备份。
- 手表上：只有拿到接收端签名回执的片段，才会在 24 小时后或空间紧张时回收；未确认的片段永不自动删除。删除手表 App 会删掉手表上所有待传录音。

## 升级、回滚和卸载

- 升级：停服务 → `git pull` → `pip install -r requirements*.txt` → 重跑 `python3 scripts/setup.py`（会保留配对密钥）→ 启服务 → `doctor.py`。
- 停服务：macOS `launchctl bootout gui/$(id -u)/io.github.xiaolu-ai26.xiaolu-watch.<组件>`；Windows 运行 `data/services/uninstall-windows-tasks.ps1`。
- 停止公网入口：`tailscale funnel --https=443 off`，或删除你的反向代理配置。
- `data/` 里的原音、数据库和待同步笔记不会因停服务丢失。不要靠删除队列或重新生成密钥来“修复”连接问题：换密钥后手表必须重新配对。
