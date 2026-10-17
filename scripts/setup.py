#!/usr/bin/env python3
"""小鹿录音 / xiaolu-watch 配置向导（只用 Python 标准库，装依赖之前就能跑）。

交互模式（推荐）:
    python3 scripts/setup.py

非交互模式（给 Agent 或脚本用）:
    python3 scripts/setup.py --non-interactive --role receiver \\
        --notes-kind obsidian --notes-dir "/path/to/Vault/手表录音" \\
        --ingress tailscale --endpoint https://your-machine.your-tailnet.ts.net
    python3 scripts/setup.py --non-interactive --role mac-sync --ssh-host my-windows \\
        --remote-notes-dir C:/xiaolu-watch/data/notes-outbox --notes-dir "/path/to/Vault/手表录音"
    python3 scripts/setup.py --apple-only --team ABCDE12345 --bundle-id com.yourname.xiaoluwatch

加 --dry-run 只打印将写入的内容，不写任何文件。所有秘密只生成在本机 data/ 目录（已被 .gitignore 忽略）。
"""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
DEFAULT_HOME = REPO / 'data'
LABEL = 'io.github.xiaolu-ai26.xiaolu-watch'
RECEIVER_PORT, INGRESS_PORT = 18437, 18438
GiB = 1024 ** 3
MODELS = {
    'tiny': '最小最快，准确率最低，适合只想先跑通',
    'base': '很快，中文准确率一般',
    'small': 'CPU 推荐起点：速度与准确率平衡',
    'medium': 'CPU 可用但较慢，准确率更好',
    'large-v3-turbo': 'NVIDIA 显卡推荐（作者实测：RTX 3060 6GB + int8_float16）',
}

# ------------------------------------------------------------------ helpers


def system():
    return {'darwin': 'mac', 'win32': 'windows'}.get(sys.platform, 'linux')


class Asker:
    def __init__(self, interactive):
        self.interactive = interactive

    def text(self, prompt, default=None, preset=None, validate=None):
        if preset is not None:
            value = preset
        elif not self.interactive:
            if default is None:
                raise SystemExit(f'非交互模式缺少参数：{prompt}')
            value = default
        else:
            suffix = f' [{default}]' if default not in (None, '') else ''
            value = input(f'{prompt}{suffix}: ').strip() or (default or '')
        if validate:
            error = validate(value)
            if error:
                if self.interactive and preset is None:
                    print('  ' + error)
                    return self.text(prompt, default, None, validate)
                raise SystemExit(error)
        return value

    def choice(self, prompt, options, default_key, preset=None):
        """options: list of (key, label). Returns key."""
        keys = [k for k, _ in options]
        if preset is not None:
            if preset not in keys:
                raise SystemExit(f'{prompt}: 无效选项 {preset}，可选 {keys}')
            return preset
        if not self.interactive:
            return default_key
        print(f'\n{prompt}')
        for i, (key, label) in enumerate(options, 1):
            mark = '（推荐）' if key == default_key else ''
            print(f'  {i}. {label}{mark}')
        while True:
            raw = input(f'选择 1-{len(options)} [默认 {keys.index(default_key) + 1}]: ').strip()
            if not raw:
                return default_key
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return keys[int(raw) - 1]
            if raw in keys:
                return raw
            print('  请输入列表里的编号。')

    def yes(self, prompt, default=False, preset=None):
        if preset is not None:
            return preset
        if not self.interactive:
            return default
        raw = input(f'{prompt} [{"Y/n" if default else "y/N"}]: ').strip().lower()
        return default if not raw else raw in ('y', 'yes', '是', '好')


def run(argv, timeout=15):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, '', str(exc)


def find_tool(name):
    candidates = [shutil.which(name)]
    if system() == 'mac':
        candidates += [f'/opt/homebrew/bin/{name}', f'/usr/local/bin/{name}']
    if system() == 'windows':
        exe = name + '.exe'
        candidates += [shutil.which(exe), f'C:/ffmpeg/bin/{exe}', f'C:/tools/ffmpeg/bin/{exe}',
                       str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'ffmpeg' / 'bin' / exe)]
        # winget (Gyan.FFmpeg) puts ffmpeg under %LOCALAPPDATA%\Microsoft\WinGet\Packages\...
        winget = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'WinGet' / 'Packages'
        if winget.is_dir():
            candidates += [str(p) for p in winget.glob(f'*FFmpeg*/**/bin/{exe}')]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).absolute())  # keep stable symlinks such as /opt/homebrew/bin
    return None


def tailscale_cli():
    for c in [shutil.which('tailscale'), '/Applications/Tailscale.app/Contents/MacOS/Tailscale',
              'C:/Program Files/Tailscale/tailscale.exe']:
        if c and Path(c).is_file():
            return c
    return None


def tailscale_dns_name():
    cli = tailscale_cli()
    if not cli:
        return None, None
    code, out, _ = run([cli, 'status', '--json'])
    if code:
        return cli, None
    try:
        name = json.loads(out).get('Self', {}).get('DNSName', '').rstrip('.')
    except ValueError:
        name = ''
    return cli, (name or None)


def local_timezone():
    if os.environ.get('TZ') and '/' in os.environ['TZ']:
        return os.environ['TZ']
    try:
        link = os.path.realpath('/etc/localtime')
        if 'zoneinfo/' in link:
            return link.split('zoneinfo/', 1)[1]
    except OSError:
        pass
    if system() == 'windows':
        code, out, _ = run(['tzutil', '/g'])
        mapping = {'China Standard Time': 'Asia/Shanghai', 'Taipei Standard Time': 'Asia/Taipei',
                   'Tokyo Standard Time': 'Asia/Tokyo', 'Singapore Standard Time': 'Asia/Singapore',
                   'Pacific Standard Time': 'America/Los_Angeles', 'Eastern Standard Time': 'America/New_York',
                   'GMT Standard Time': 'Europe/London', 'W. Europe Standard Time': 'Europe/Berlin'}
        if not code and out.strip() in mapping:
            return mapping[out.strip()]
    return 'Asia/Shanghai'


def obsidian_vaults():
    if system() == 'mac':
        path = Path.home() / 'Library/Application Support/obsidian/obsidian.json'
    elif system() == 'windows':
        path = Path(os.environ.get('APPDATA', '')) / 'obsidian' / 'obsidian.json'
    else:
        path = Path.home() / '.config/obsidian/obsidian.json'
    try:
        vaults = json.loads(path.read_text(encoding='utf-8')).get('vaults', {})
        return [v['path'] for v in vaults.values() if v.get('path') and Path(v['path']).is_dir()]
    except (OSError, ValueError, AttributeError):
        return []


def detect_cuda():
    """Return (device, reason). Never installs anything."""
    try:
        import ctranslate2  # noqa: F401  (present only after requirements-asr.txt is installed)
        count = ctranslate2.get_cuda_device_count()
        if count > 0:
            return 'cuda', f'CTranslate2 检测到 {count} 块 CUDA 显卡'
        return 'cpu', 'CTranslate2 未检测到可用 CUDA 显卡'
    except Exception:
        pass
    if system() != 'mac' and shutil.which('nvidia-smi'):
        code, out, _ = run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'])
        if not code and out.strip():
            return 'cuda', 'nvidia-smi 检测到显卡：' + out.strip().splitlines()[0] + '（GPU 运行库仍需按文档安装）'
    if system() == 'mac':
        return 'cpu', 'macOS：faster-whisper 只能用 CPU（Apple Silicon 也是 CPU 推理）'
    return 'cpu', '未检测到 NVIDIA 显卡'


def memory_gib():
    try:
        if system() == 'mac':
            code, out, _ = run(['sysctl', '-n', 'hw.memsize'])
            return int(out) / GiB if not code else None
        if system() == 'windows':
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [('l', ctypes.c_ulong), ('m', ctypes.c_ulong), ('t', ctypes.c_ulonglong), ('a', ctypes.c_ulonglong),
                            ('tp', ctypes.c_ulonglong), ('ap', ctypes.c_ulonglong), ('tv', ctypes.c_ulonglong),
                            ('av', ctypes.c_ulonglong), ('ae', ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.t / GiB
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / GiB
    except Exception:
        return None


def validate_https_origin(value):
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+(:\d{1,5})?/?', value or ''):
        return '需要形如 https://example.com 的 HTTPS 根地址（不带路径）。'
    return None


def venv_python():
    candidate = REPO / '.venv' / ('Scripts/python.exe' if system() == 'windows' else 'bin/python')
    return candidate if candidate.is_file() else Path(sys.executable)


class Writer:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.written = []

    def write(self, path, text, secret=False):
        path = Path(path)
        self.written.append(str(path))
        if self.dry_run:
            shown = '<含秘密，dry-run 不显示>' if secret else text
            print(f'\n--- [dry-run] 将写入 {path}\n{shown}')
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if secret and os.name != 'nt':
            os.chmod(path.parent, 0o700)
        temp = path.with_name(path.name + '.pending')
        # Windows PowerShell 5.1 needs a BOM to read UTF-8 (Chinese comments/messages) correctly.
        encoding = 'utf-8-sig' if path.suffix == '.ps1' else 'utf-8'
        with open(temp, 'w', encoding=encoding, newline='\n') as f:
            if secret and os.name != 'nt':
                os.chmod(temp, 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)

    def json(self, path, value, secret=False):
        self.write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n', secret)


# ------------------------------------------------------------------ service files


def launchd_plist(label, argv, home, interval=None):
    env_path = os.pathsep.join(['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'])
    items = ''.join(f'\n        <string>{a}</string>' for a in argv)
    schedule = (f'\n    <key>StartInterval</key>\n    <integer>{interval}</integer>' if interval
                else '\n    <key>KeepAlive</key>\n    <true/>\n    <key>ThrottleInterval</key>\n    <integer>30</integer>')
    name = label.rsplit('.', 1)[1]
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>{items}
    </array>
    <key>WorkingDirectory</key>
    <string>{REPO}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{env_path}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>{schedule}
    <key>StandardOutPath</key>
    <string>{home}/logs/{name}.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/logs/{name}.log</string>
</dict>
</plist>
'''


def windows_tasks_script(python, home, components):
    lines = [
        '# 由 scripts/setup.py 生成。用“以管理员身份运行”的 PowerShell 执行：',
        '#   powershell -ExecutionPolicy Bypass -File install-windows-tasks.ps1',
        '# 注意：本脚本未在干净的 Windows 环境中实测；作者实测环境的任务是手工建立的。',
        '$ErrorActionPreference = "Stop"',
        f'$Python = "{python}"',
        f'$Repo = "{REPO}"',
        f'$DataHome = "{home}"',
        '$Me = "$env:USERDOMAIN\\$env:USERNAME"',
        '$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) '
        '-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable '
        '-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries',
        '# 接收器和 HTTPS 入口不需要用户凭据：开机即以 SYSTEM 运行（ffprobe 已在配置里写成绝对路径）。',
        '$System = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest',
        '# 转写/写笔记以你本人身份运行（模型缓存、可选的 lark-cli 登录都在你的用户目录）。',
        '# 默认在你登录后启动；若希望重启后无人登录也运行，可在任务计划程序里改为“不管用户是否登录都要运行”。',
        '$User = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive',
        '$Boot = New-ScheduledTaskTrigger -AtStartup',
        '$Logon = New-ScheduledTaskTrigger -AtLogOn -User $Me',
    ]
    for name, principal, trigger in components:
        lines.append(f'$A = New-ScheduledTaskAction -Execute $Python -Argument "-B `"$Repo\\scripts\\service.py`" {name} --home `"$DataHome`"" -WorkingDirectory $Repo')
        lines.append(f'Register-ScheduledTask -TaskName "XiaoluWatch-{name}" -Action $A -Principal {principal} -Trigger {trigger} -Settings $Settings -Force | Out-Null')
        lines.append(f'Start-ScheduledTask -TaskName "XiaoluWatch-{name}"')
        lines.append(f'Write-Host "已注册并启动 XiaoluWatch-{name}"')
    return '\r\n'.join(lines) + '\r\n'


def windows_uninstall_script(components):
    lines = ['# 停止并删除本项目创建的计划任务（不删除 data/ 里的录音、数据库和笔记）。']
    for name, _, _ in components:
        lines.append(f'Unregister-ScheduledTask -TaskName "XiaoluWatch-{name}" -Confirm:$false -ErrorAction SilentlyContinue')
    return '\r\n'.join(lines) + '\r\n'


# ------------------------------------------------------------------ flows


def install_deps(asker, args, role):
    if not asker.yes('现在创建 .venv 并安装 Python 依赖吗？（会联网下载 pip 包）', default=True, preset=args.install_deps):
        return
    if args.dry_run:
        print('[dry-run] 将创建 .venv 并安装 requirements.txt' + (' 与 requirements-asr.txt' if role == 'receiver' else ''))
        return
    venv = REPO / '.venv'
    if not venv.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    py = str(venv_python())
    subprocess.run([py, '-m', 'pip', 'install', '--upgrade', 'pip'], check=False)
    subprocess.run([py, '-m', 'pip', 'install', '-r', str(REPO / 'requirements.txt')], check=True)
    if role == 'receiver':
        subprocess.run([py, '-m', 'pip', 'install', '-r', str(REPO / 'requirements-asr.txt')], check=True)


def download_model(settings, dry_run):
    asr = settings['asr']
    target = Path(asr['model'])
    if (target / 'model.bin').is_file():
        print(f'模型已存在：{target}')
        return
    print(f'下载模型 {asr["model_name"]} 到 {target}（一次性联网下载，之后转写全程离线）…')
    if dry_run:
        return
    code = ('import sys; from faster_whisper import download_model; '
            'print(download_model(sys.argv[1], output_dir=sys.argv[2]))')
    result = subprocess.run([str(venv_python()), '-c', code, asr['model_name'], str(target)])
    if result.returncode:
        raise SystemExit('模型下载失败。确认已安装 requirements-asr.txt 且能访问 Hugging Face；'
                         '国内网络可先设置 HF_ENDPOINT 镜像后重试：python3 scripts/setup.py --download-model')


def apple_flow(asker, args, writer):
    local = REPO / 'apple/WatchMemory/Config/Local.xcconfig'
    if system() != 'mac' and not args.apple_only:
        return
    if not args.apple_only and not asker.yes('\n要现在填写 Apple 签名信息（用于把 App 装到你自己的手表）吗？', default=asker.interactive,
                                              preset=True if args.team else None):
        print(f'  跳过。之后可运行：python3 scripts/setup.py --apple-only')
        return
    existing = {}
    if local.exists():
        for line in local.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.strip().startswith('//'):
                k, v = line.split('=', 1)
                existing[k.strip()] = v.strip()
    print('\nTeam ID 在 Xcode → Settings → Accounts → 选中你的 Apple ID → Team 里看到（10 位字母数字）。'
          '\n免费 Apple ID 的 Personal Team 也可以签名装到自己的设备（有效期和设备数以 Xcode 显示为准）。')
    team = asker.text('Apple 开发团队 Team ID', existing.get('WATCH_MEMORY_DEVELOPMENT_TEAM'), args.team,
                      lambda v: None if re.fullmatch(r'[A-Z0-9]{10}', v or '') else 'Team ID 是 10 位大写字母或数字。')
    default_bundle = existing.get('XIAOLU_WATCH_BUNDLE_ID') or f'com.xiaoluwatch.u{secrets.token_hex(3)}'
    bundle = asker.text('App 的 Bundle ID（要在你的团队里唯一）', default_bundle, args.bundle_id,
                        lambda v: None if re.fullmatch(r'[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+', v or '') else 'Bundle ID 形如 com.yourname.xiaoluwatch')
    writer.write(local, '// 由 scripts/setup.py 生成；已被 .gitignore 忽略，不要提交。\n'
                 f'WATCH_MEMORY_DEVELOPMENT_TEAM = {team}\nXIAOLU_WATCH_BUNDLE_ID = {bundle}\n')
    print('  手表 App 的 Bundle ID 将是 ' + bundle + '.watchkitapp')


def receiver_flow(asker, args, writer, home, previous):
    host = system()
    if host == 'linux':
        print('提示：Linux 接收端理论上可用（与 macOS 同为 POSIX），但作者没有测试过，也不生成服务文件。')
    print(f'\n这台电脑：{platform.system()} {platform.release()}，将作为接收端（需要常开）。')

    ffprobe = args.ffprobe or find_tool('ffprobe')
    ffmpeg = find_tool('ffmpeg')
    if not ffprobe or not ffmpeg:
        hint = {'mac': 'brew install ffmpeg', 'windows': 'winget install Gyan.FFmpeg（装完重新打开终端）'}.get(host, '用包管理器安装 ffmpeg')
        print(f'没找到 ffmpeg/ffprobe。安装方法：{hint}')
        if not asker.yes('先不管、继续生成配置（稍后装好再重跑本向导）？', default=False, preset=True if args.dry_run else None):
            raise SystemExit('请先安装 ffmpeg 后重跑。')
    ffmpeg_dir = str(Path(ffprobe).parent) if ffprobe else None

    # ---- notes
    vaults = obsidian_vaults()
    if host == 'windows':
        kind_options = [('obsidian', '写进这台 Windows 上的 Obsidian Vault'), ('folder', '写进这台 Windows 上的普通文件夹'),
                        ('mac-pull', '先存在接收端，Mac 开机后再拉进 Mac 上的 Obsidian（作者实测方案）')]
        kind_default = 'mac-pull'
    else:
        kind_options = [('obsidian', '写进 Obsidian Vault'), ('folder', '写进普通文件夹')]
        kind_default = 'obsidian' if vaults else 'folder'
    prev_notes = previous.get('notes', {})
    kind = asker.choice('转写结果（Markdown 笔记）放哪里？', kind_options, prev_notes.get('kind', kind_default), args.notes_kind)
    if kind == 'mac-pull':
        notes_dir = home / 'notes-outbox'
    elif kind == 'obsidian':
        if args.notes_dir:
            notes_dir = Path(args.notes_dir)
        else:
            vault = None
            if vaults and asker.interactive:
                pick = asker.choice('检测到这些 Obsidian Vault：', [(v, v) for v in vaults] + [('other', '其他路径')], vaults[0])
                vault = None if pick == 'other' else pick
            vault = vault or asker.text('Obsidian Vault 的完整路径', vaults[0] if vaults else None,
                                        validate=lambda v: None if Path(v).expanduser().is_dir() else '这个文件夹不存在。')
            sub = asker.text('在 Vault 里用哪个子文件夹放录音笔记', '手表录音')
            notes_dir = Path(vault).expanduser() / sub
    else:
        notes_dir = Path(asker.text('笔记文件夹的完整路径', prev_notes.get('dir') or str(Path.home() / '手表录音'), args.notes_dir))
    notes_dir = notes_dir.expanduser().resolve()

    # ---- feishu
    feishu = asker.yes('\n要同时发到飞书文档吗？（可选，默认关闭；需要自己装好 lark-cli 并登录）',
                       default=bool(previous.get('feishu', {}).get('enabled', False)),
                       preset=None if args.feishu is None else args.feishu == 'on')
    if feishu and not shutil.which('lark-cli'):
        print('  提醒：没找到 lark-cli。安装：npm install -g @larksuite/cli，然后 lark-cli auth login。'
              '飞书失败只会让该段停在重试，不影响本地笔记以外的部分。')

    # ---- ingress
    cli, dns = tailscale_dns_name()
    ingress = asker.choice('\n手表从外网怎么连到这台电脑？（手表只走 HTTPS）', [
        ('tailscale', 'Tailscale Funnel：免费、自带证书，手机/手表都不用装 VPN' + ('（已检测到 Tailscale）' if cli else '')),
        ('custom', '我自己的 HTTPS 域名 + 反向代理（如 Caddy/Nginx/Cloudflare Tunnel）')],
        previous.get('ingress', {}).get('type', 'tailscale'), args.ingress)
    if ingress == 'tailscale':
        guess = f'https://{dns}' if dns else previous.get('public_endpoint')
        if not cli:
            print('  没检测到 Tailscale。先安装并登录：https://tailscale.com/download ，然后重跑或手动填地址。')
        endpoint = asker.text('Funnel 的 HTTPS 地址（形如 https://机器名.你的tailnet.ts.net）', guess, args.endpoint, validate_https_origin)
    else:
        endpoint = asker.text('你的 HTTPS 根地址', previous.get('public_endpoint'), args.endpoint, validate_https_origin)
    endpoint = endpoint.rstrip('/')

    # ---- ASR
    device, reason = detect_cuda() if args.asr_device in (None, 'auto') else (args.asr_device, '按参数指定')
    mem = memory_gib()
    if device == 'cuda':
        recommended = 'large-v3-turbo'
    else:
        recommended = 'small'
    print(f'\n语音识别（本地 faster-whisper）：{reason}；内存约 {mem:.0f} GB。' if mem else f'\n语音识别（本地 faster-whisper）：{reason}。')
    model_name = asker.choice('选择模型大小', [(k, f'{k}：{v}') for k, v in MODELS.items()],
                              previous.get('asr', {}).get('model_name', recommended),
                              None if args.asr_model in (None, 'auto') else args.asr_model)
    compute_type = 'int8_float16' if device == 'cuda' else 'int8'
    language = asker.choice('录音主要语言', [('auto', '自动识别（中英混说也行）'), ('zh', '固定中文'), ('en', '固定英文')],
                            previous.get('asr', {}).get('language', 'auto'), args.language)

    # ---- timezone + storage
    tz = asker.text('\n时区（决定一段录音算哪一天）', previous.get('timezone') or local_timezone(), args.timezone,
                    lambda v: None if re.fullmatch(r'[A-Za-z_]+(/[A-Za-z0-9_+-]+)+|UTC', v or '') else '请用 IANA 时区名，如 Asia/Shanghai')
    free = shutil.disk_usage(home.parent if not home.exists() else home).free
    budget_default = max(4, min(64, int(free * 0.4 / GiB)))
    budget = int(asker.text(f'接收端最多用多少 GB 存原始录音（当前磁盘剩余约 {free / GiB:.0f} GB）', str(budget_default),
                            None if args.storage_gb is None else str(args.storage_gb),
                            lambda v: None if v.isdigit() and int(v) >= 1 else '请输入整数 GB'))

    # ---- write receiver config (secret) and settings
    config_path = home / 'receiver' / 'private' / 'config.json'
    old_secret = None
    if config_path.exists():
        try:
            old_secret = json.loads(config_path.read_text(encoding='utf-8')).get('pairing_secret')
        except ValueError:
            pass
    secret = old_secret if old_secret and re.fullmatch(r'[A-Za-z0-9_-]{43}', old_secret) else secrets.token_urlsafe(32)
    assert len(base64.urlsafe_b64decode(secret + '=')) == 32
    if old_secret == secret:
        print('\n保留现有配对密钥（已配对的手表不用重配）。')
    writer.json(config_path, {
        'version': 2, 'protocol_version': 2, 'bind_addresses': ['127.0.0.1'], 'port': RECEIVER_PORT,
        'ffprobe_path': ffprobe, 'pairing_secret': secret, 'max_local_bytes': budget * GiB,
        'minimum_free_bytes': 2 * GiB, 'gc_enabled': False, 'timezone': tz}, secret=True)
    settings = {
        'version': 1, 'role': 'receiver', 'platform': host, 'created_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        'timezone': tz, 'public_endpoint': endpoint, 'ingress': {'type': ingress, 'port': INGRESS_PORT},
        'receiver': {'root': str(home / 'receiver'), 'port': RECEIVER_PORT},
        'asr': {'model_name': model_name, 'model': str(home / 'models' / model_name), 'device': device,
                'compute_type': compute_type, 'language': language, 'multilingual': True},
        'notes': {'kind': kind, 'dir': str(notes_dir), 'manifest': kind == 'mac-pull'},
        'feishu': {'enabled': feishu}, 'ffmpeg_dir': ffmpeg_dir,
    }
    writer.json(home / 'settings.json', settings)
    return settings


def mac_sync_flow(asker, args, writer, home, previous):
    if system() != 'mac':
        print('提示：拉取笔记这一步设计在 Mac 上运行。')
    prev = previous.get('mac_sync', {})
    print('\n这台 Mac 只负责：Windows 接收端写好的笔记 → 拉进本机 Obsidian。需要 Mac 能用 SSH 免密登录那台 Windows（OpenSSH 服务器）。')
    host = asker.text('SSH 主机（~/.ssh/config 里的别名，或 user@主机名）', prev.get('ssh_host'), args.ssh_host,
                      lambda v: None if re.fullmatch(r'[A-Za-z0-9_.@-]+', v or '') else '只填别名或 user@host')
    remote = asker.text('Windows 上的笔记 outbox 路径（Windows 向导结束时会打印，形如 C:/xiaolu-watch/data/notes-outbox）',
                        prev.get('remote_notes_dir'), args.remote_notes_dir)
    vaults = obsidian_vaults()
    if args.notes_dir:
        dest = Path(args.notes_dir)
    else:
        vault = asker.text('本机 Obsidian Vault 路径', vaults[0] if vaults else prev.get('destination'),
                           validate=lambda v: None if Path(v).expanduser().is_dir() else '这个文件夹不存在。')
        dest = Path(vault).expanduser() / asker.text('Vault 里的子文件夹', '手表录音')
    dest = dest.expanduser().resolve()
    if asker.yes('现在测试一次 SSH 连接？', default=True, preset=False if args.dry_run or not asker.interactive else None):
        code, _, err = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, 'echo', 'ok'], timeout=20)
        print('  SSH 可用。' if code == 0 else f'  SSH 未通：{err.strip()[:200]}（先配置 SSH 密钥登录，见 docs/deployment.md）')
    settings = {'version': 1, 'role': 'mac-sync', 'platform': system(),
                'created_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                'mac_sync': {'enabled': True, 'ssh_host': host, 'remote_notes_dir': remote, 'destination': str(dest),
                             'interval_seconds': 120}}
    writer.json(home / 'settings.json', settings)
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    return settings


def write_services(settings, home, writer):
    py = str(venv_python())
    service = str(REPO / 'scripts' / 'service.py')
    out = home / 'services'
    commands = []
    if settings['platform'] == 'mac':
        names = ['receiver', 'ingress', 'pipeline'] if settings['role'] == 'receiver' else ['sync']
        for name in names:
            interval = settings['mac_sync']['interval_seconds'] if name == 'sync' else None
            label = f'{LABEL}.{name}'
            path = out / f'{label}.plist'
            writer.write(path, launchd_plist(label, [py, '-B', service, name, '--home', str(home)], home, interval))
            commands.append(f'launchctl bootstrap gui/$(id -u) "{path}"')
        stop = [f'launchctl bootout gui/$(id -u)/{LABEL}.{n}' for n in names]
        return commands, stop
    if settings['platform'] == 'windows' and settings['role'] == 'receiver':
        components = [('receiver', '$System', '$Boot'), ('ingress', '$System', '$Boot'), ('pipeline', '$User', '$Logon')]
        writer.write(out / 'install-windows-tasks.ps1', windows_tasks_script(py, home, components))
        writer.write(out / 'uninstall-windows-tasks.ps1', windows_uninstall_script(components))
        return ([f'powershell -ExecutionPolicy Bypass -File "{out / "install-windows-tasks.ps1"}"  （管理员 PowerShell）'],
                [f'powershell -ExecutionPolicy Bypass -File "{out / "uninstall-windows-tasks.ps1"}"'])
    return [f'{py} -B {service} receiver --home "{home}"', f'{py} -B {service} ingress --home "{home}"',
            f'{py} -B {service} pipeline --home "{home}"'], []


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--home', type=Path, default=Path(os.environ.get('XIAOLU_WATCH_HOME') or DEFAULT_HOME), help='运行数据目录（默认 data/）')
    p.add_argument('--non-interactive', action='store_true', help='不提问，缺省值或参数决定一切')
    p.add_argument('--dry-run', action='store_true', help='只打印，不写文件、不装依赖、不下载')
    p.add_argument('--role', choices=['receiver', 'mac-sync'])
    p.add_argument('--notes-kind', choices=['obsidian', 'folder', 'mac-pull'])
    p.add_argument('--notes-dir')
    p.add_argument('--feishu', choices=['on', 'off'])
    p.add_argument('--ingress', choices=['tailscale', 'custom'])
    p.add_argument('--endpoint', help='公网 HTTPS 根地址')
    p.add_argument('--asr-device', choices=['auto', 'cuda', 'cpu'])
    p.add_argument('--asr-model', choices=['auto', *MODELS])
    p.add_argument('--language', choices=['auto', 'zh', 'en'])
    p.add_argument('--timezone')
    p.add_argument('--storage-gb', type=int)
    p.add_argument('--ffprobe', help='ffprobe 绝对路径（自动检测失败时用）')
    p.add_argument('--ssh-host')
    p.add_argument('--remote-notes-dir')
    p.add_argument('--team', help='Apple Team ID')
    p.add_argument('--bundle-id')
    p.add_argument('--apple-only', action='store_true', help='只写 Apple 签名配置')
    p.add_argument('--install-deps', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--download-model', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--install-services', action=argparse.BooleanOptionalAction, default=None, help='macOS：立即用 launchctl 加载服务')
    args = p.parse_args()

    if sys.version_info < (3, 10):
        raise SystemExit('需要 Python 3.10 或更高版本。')
    asker = Asker(interactive=not args.non_interactive and sys.stdin.isatty())
    writer = Writer(args.dry_run)
    home = args.home.expanduser().resolve()
    previous = {}
    if (home / 'settings.json').exists():
        try:
            previous = json.loads((home / 'settings.json').read_text(encoding='utf-8'))
        except ValueError:
            pass

    if args.apple_only:
        apple_flow(asker, args, writer)
        return
    if args.download_model and not any([args.role, args.non_interactive]) and previous.get('role') == 'receiver':
        download_model(previous, args.dry_run)
        return

    print('小鹿录音 配置向导。每一步都有推荐值，直接回车即可。')
    role = asker.choice('这台电脑要做什么？', [
        ('receiver', '当接收端：常开，接收手表录音、本地转写、写笔记（Mac 或 Windows）'),
        ('mac-sync', '只在这台 Mac 上把 Windows 接收端的笔记拉进 Obsidian（Windows 方案的第二步）')],
        previous.get('role', 'receiver'), args.role)
    home.mkdir(parents=True, exist_ok=True) if not args.dry_run else None
    settings = receiver_flow(asker, args, writer, home, previous) if role == 'receiver' else mac_sync_flow(asker, args, writer, home, previous)
    if role == 'receiver' or system() == 'mac':
        apple_flow(asker, args, writer)
    install_deps(asker, args, role)
    if role == 'receiver' and asker.yes('现在下载语音识别模型吗？（一次性，large-v3-turbo 约 1.6 GB，small 约 0.5 GB）',
                                        default=True, preset=args.download_model):
        download_model(settings, args.dry_run)
    start, stop = write_services(settings, home, writer)

    if settings['platform'] == 'mac' and asker.yes('现在加载后台服务（launchd）吗？', default=False, preset=args.install_services):
        for c in start:
            if args.dry_run:
                print('[dry-run] ' + c)
            else:
                subprocess.run(c, shell=True, check=False)

    print('\n=========== 完成 ===========')
    print('写入/将写入：\n  ' + '\n  '.join(writer.written))
    print('\n启动后台服务：\n  ' + '\n  '.join(start))
    if stop:
        print('停止服务：\n  ' + '\n  '.join(stop))
    if role == 'receiver':
        if settings['ingress']['type'] == 'tailscale':
            cli = tailscale_cli() or 'tailscale'
            print(f'\n打开公网 HTTPS 入口（只需一次，重启后保持）：\n  "{cli}" funnel --bg --https=443 http://127.0.0.1:{INGRESS_PORT}')
        else:
            print(f'\n把你的 HTTPS 域名反向代理到 http://127.0.0.1:{INGRESS_PORT}（示例见 docs/deployment.md）。')
        print('\n检查整条链路：  python3 scripts/doctor.py')
        print('给手表配对：    python3 scripts/pair.py   （见 docs/apple-build.md）')
        if settings['notes']['manifest']:
            print(f'\nMac 上运行向导时，“Windows 上的笔记 outbox 路径”填：{Path(settings["notes"]["dir"]).as_posix()}')
    else:
        print('\n检查：python3 scripts/doctor.py')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已取消，没有写入未完成的配置。')
