# AGENTS.md — 给 AI 编程助手的安装手册

读者是 Claude Code、Codex 等替用户安装本项目的 Agent。用户的典型说法是“把仓库丢给你，帮我装好”。你的任务：先问清几个选择，再按选择把接收端、手表 App 和配对做完，最后用一段真实录音验收。和用户交流默认用简体中文，命令和路径保持英文。

## 基本规则

- 不提交、不上传、不打印 `data/` 里的任何内容：配对密钥（`data/receiver/private/config.json`）、配对码、原音、数据库、转写、日志。需要确认密钥时只说“格式正确”，不要显示内容。`scripts/pair.py` 默认不打印配对码，除非用户要求，不要加 `--show-code`。
- 不要求用户关闭手表 TLS 校验，不开放除 Tailscale Funnel/用户自己的反向代理之外的任何公网端口，不改路由器端口转发。
- 安装软件、加载后台服务、开启 Funnel、修改系统设置之前，先告诉用户要做什么并得到同意。
- Xcode 登录 Apple ID、手表信任电脑、打开开发者模式、在手表上点“开始”这些只能用户本人在设备上做。到这一步停下来，清楚说明点哪里，等用户回复再继续。
- 如实汇报：区分“配置写好了”“服务在跑”“公网 health 通了”“手表第一段录音拿到回执”“笔记出现在 Vault 里”，不要用前一个代替后一个。README 的“已验证与限制”是事实边界，不要替项目夸大。

## 第 0 步：先问用户这些问题

一次问完，给出选项和推荐值；用户说“都用推荐”就按推荐来。

1. **接收端用哪台电脑？** 这台电脑要常开。
   - 常开的 Windows 电脑（作者实测方案；有 NVIDIA 显卡更好）。笔记可以直接写进 Windows 上的文件夹/Vault，或先存在 Windows、之后由 Mac 拉进 Mac 的 Obsidian。
   - 常开的 Mac（同机接收、转写、写 Vault）。要告诉用户：这条路线代码与 Windows 相同，但**未经真机验收**，Mac 上只能 CPU 转写、较慢。
2. **笔记放哪？** Obsidian Vault（问 Vault 路径，向导会自动列出本机 Vault；子文件夹默认“手表录音”）或普通文件夹。
3. **要不要同时发飞书？** 默认不要。要的话用户需自己装 `lark-cli` 并 `lark-cli auth login`。
4. **网络入口？** 推荐 Tailscale Funnel（免费、自带证书，手机和手表都不用装 VPN；用户手机上已有其他 VPN 也没关系）。或用户自己的 HTTPS 域名 + 反向代理。
5. **Apple 开发团队 Team ID 和 Bundle ID**（只在要装 App 的 Mac 上需要）。没有的话先让用户在 Xcode → Settings → Accounts 登录 Apple ID。
6. 可选：录音主要语言（自动/中文/英文）、时区（默认用本机）、接收端最多用多少 GB 存原音。

语音识别设备和模型不用问：向导自动检测 CUDA/CPU 并推荐（显卡 → large-v3-turbo，CPU → small），只在用户有偏好时调整。

## 第 1 步：检查前置条件

在接收端电脑上：

- Python 3.10+：`python3 --version`（Windows 用 `py -3 --version`）。
- FFmpeg：`ffprobe -version`。缺少时，macOS `brew install ffmpeg`；Windows `winget install Gyan.FFmpeg` 后重开终端。
- Tailscale（选 Funnel 时）：已安装并登录，`tailscale status` 正常。Funnel 需要在 Tailscale 管理后台允许（首次运行 funnel 命令时会给出链接）。
- Windows + NVIDIA：显卡驱动正常（`nvidia-smi`），GPU 运行库见 [docs/deployment.md](docs/deployment.md#nvidia-显卡)。

## 第 2 步：生成配置

用户愿意自己回答时，直接让他运行交互向导 `python3 scripts/setup.py`。由你代为执行时，用非交互参数，把第 0 步的答案映射过去：

```bash
# 接收端（Mac 或 Windows 都是这条；在接收端电脑上运行）
python3 scripts/setup.py --non-interactive --role receiver \
  --notes-kind obsidian --notes-dir "<Vault>/手表录音" \
  --feishu off \
  --ingress tailscale --endpoint "https://<机器名>.<tailnet>.ts.net" \
  --asr-device auto --asr-model auto --language auto \
  --install-deps --download-model --no-install-services
```

- 笔记选项：`--notes-kind obsidian|folder` 配 `--notes-dir`；Windows 先存后拉用 `--notes-kind mac-pull`（不需要 `--notes-dir`）。飞书要开就用 `--feishu on`。
- 自定义域名用 `--ingress custom --endpoint https://你的域名`。
- 先用 `--dry-run` 跑一遍给用户看将写入什么，再去掉它正式执行。
- `--endpoint` 可以先用 `tailscale status --json` 里 `Self.DNSName`（去掉末尾的点）拼出 `https://...`。
- 重复运行是安全的：已有配对密钥会保留，已配对的手表不用重配。
- `--install-deps` 会创建 `.venv` 并安装 `requirements.txt`、`requirements-asr.txt`；`--download-model` 一次性下载模型到 `data/models/`。国内网络下载模型失败时，可设置 `HF_ENDPOINT` 为可用镜像后重跑 `python3 scripts/setup.py --download-model`。
- 之后运行项目脚本时优先用 `.venv` 里的 Python（`.venv/bin/python`，Windows 为 `.venv\Scripts\python.exe`）。

## 第 3 步：启动服务和公网入口（征得同意后）

向导结束时会打印准确命令：

- macOS：`launchctl bootstrap gui/$(id -u) data/services/<label>.plist`（receiver、ingress、pipeline 三个）。Mac 作接收端还要让它不睡眠：系统设置 → 电池/节能 → 防止自动睡眠（接电源时）。
- Windows：管理员 PowerShell 运行 `data/services/install-windows-tasks.ps1`（未在干净环境实测，失败时按报错修正，或在任务计划程序里手动建同样的三个任务）。
- Funnel（只需一次，重启后保持）：`tailscale funnel --bg --https=443 http://127.0.0.1:18438`。自定义域名则把 HTTPS 反向代理到 `http://127.0.0.1:18438`。
- 临时前台调试：`python3 scripts/service.py receiver --foreground`（ingress、pipeline 同理）。日志在 `data/logs/`。

然后运行 `python3 scripts/doctor.py`，把所有 `[!!]` 修到 `[OK]`。公网 health 通了只说明入口可达，不等于手表已经能上传。

## 第 4 步：编译并安装手表 App（需要 Mac + Xcode）

按 [docs/apple-build.md](docs/apple-build.md)：

1. `python3 scripts/setup.py --apple-only --team <TeamID> --bundle-id <唯一ID>`（第 2 步若在这台 Mac 上已填可跳过）。
2. `brew install xcodegen`，然后 `apple/scripts/generate-project.sh`。
3. 让用户在 Xcode 打开 `apple/WatchMemory/WatchMemory.xcodeproj`，选 `WatchMemoryWatch` scheme 和自己的手表，点 Run（或选 `WatchMemoryPhone` + iPhone 安装伴侣 App）。Xcode 看不到手表时，按 [docs/troubleshooting.md](docs/troubleshooting.md) 的“Xcode 连不上手表”一节逐步排查，这是最常见的卡点。
4. 让用户在手表上打开一次“小鹿录音”，允许麦克风。

## 第 5 步：配对

1. 在接收端运行 `python3 scripts/pair.py`（确认 ingress 和 Funnel 已在运行）。配对码 10 分钟有效、只能被一块手表认领。
2. 首选零输入：在装 Xcode 的 Mac 上运行 `python3 scripts/deliver_onboarding.py`。接收端是同一台 Mac 时它自动读取 `data/receiver/private/onboarding.json`；接收端是 Windows 时，让用户把那台电脑上的 `onboarding.json` 拷过来用 `--file`，或让用户打开配对卡后用 `--endpoint/--code` 传入（不要把码贴进聊天记录以外的公开地方）。该投递机制在作者设备上验证过，脚本本身未在干净环境实测。
3. 备选：用户在手表“小鹿录音 → 连接处理电脑”里输入 HTTPS 地址和配对码（已实现，未在真机实测）。
4. 让用户抬腕保持 App 前台约 1 分钟，直到显示“处理电脑已连接”。

## 第 6 步：真实验收

让用户点“开始”，说几句话，回到表盘等 2–3 分钟，再点“结束”。然后检查，并按实际结果汇报：

- `python3 scripts/doctor.py` 的处理统计里出现 `done`（有声）或 `no_speech`（无声）。
- 笔记目录 `<notes>/录音资料/` 里出现新的 `.md`。Windows 先存后拉方案：笔记先出现在 Windows 的 `data/notes-outbox/录音资料/`，Mac 端拉取配置好后再出现在 Mac Vault。
- `python3 processor/pipeline.py status` 可以看每段状态和错误（不含转写原文）。

## Windows 方案的第二步：在 Mac 上拉取笔记

在 Windows 上启用 OpenSSH 服务器并配置用户 Mac 的密钥登录（见 [docs/deployment.md](docs/deployment.md#windows-接收端--mac-拉取笔记)），然后在 Mac 上：

```bash
python3 scripts/setup.py --non-interactive --role mac-sync --ssh-host <ssh 别名> \
  --remote-notes-dir "<Windows 向导结束时打印的 notes-outbox 路径>" --notes-dir "<Vault>/手表录音"
launchctl bootstrap gui/$(id -u) data/services/io.github.xiaolu-ai26.xiaolu-watch.sync.plist
python3 scripts/doctor.py
```

## 代码约定（修改本项目时）

- 测试：`python3 -m unittest discover -s receiver/tests` 与 `-s processor/tests`（receiver 测试需要 `cryptography` 和 FFmpeg）；Swift 核心：`apple/scripts/run-checks.sh`。
- 协议以 [docs/wire-v2.md](docs/wire-v2.md) 为唯一合同，不要替换密码算法，也不要把 HTTP 200 当作原音已安全保存。
- 测试只用代码里标注为公开夹具的确定性测试密钥和 FFmpeg 现场合成的音频，不要提交任何真实录音、转写或个人路径。
