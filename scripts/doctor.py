#!/usr/bin/env python3
"""Check the whole chain and say what to fix. Read-only: it never changes configuration.

    python3 scripts/doctor.py
    python3 scripts/doctor.py --skip-public     # skip the public HTTPS check
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.request

REPO = Path(__file__).resolve().parent.parent
results = []


def report(ok, what, fix=''):
    results.append(ok)
    print(f'[{"OK" if ok else "!!"}] {what}' + ('' if ok or not fix else f'\n      -> {fix}'))


def port_open(port):
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(('127.0.0.1', port)) == 0


def get_json(url, timeout=10):
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'xiaolu-watch-doctor'}), timeout=timeout, context=ctx) as r:
        return r.status, json.loads(r.read(4096) or b'{}')


def check_receiver(settings, home, skip_public):
    root = Path(settings['receiver']['root'])
    config_path = root / 'private' / 'config.json'
    if not config_path.is_file():
        report(False, '接收端配置不存在', 'python3 scripts/setup.py')
        return
    config = json.loads(config_path.read_text(encoding='utf-8'))
    report(bool(re.fullmatch(r'[A-Za-z0-9_-]{43}', config.get('pairing_secret', ''))), '配对密钥格式正确（内容不显示）', '重跑 setup.py 生成')
    if os.name != 'nt':
        mode = config_path.stat().st_mode & 0o777
        report(mode == 0o600, f'密钥文件权限 {oct(mode)}', f'chmod 600 "{config_path}"')
    ffprobe = config.get('ffprobe_path')
    ok = bool(ffprobe) and Path(ffprobe).is_file() and subprocess.run([ffprobe, '-version'], capture_output=True).returncode == 0
    report(ok, f'ffprobe 可执行：{ffprobe}', '安装 ffmpeg 后重跑 setup.py（服务账户需要绝对路径）')
    try:
        import cryptography  # noqa: F401
        report(True, 'Python 依赖 cryptography 已安装')
    except ImportError:
        report(False, '缺少 cryptography', f'"{sys.executable}" -m pip install -r requirements.txt（或用 .venv 里的 python 运行 doctor）')
    try:
        import faster_whisper  # noqa: F401
        report(True, 'faster-whisper 已安装')
    except ImportError:
        report(False, '缺少 faster-whisper', 'pip install -r requirements-asr.txt')
    model = Path(settings['asr']['model'])
    report(all((model / f).is_file() for f in ('model.bin', 'config.json', 'tokenizer.json')),
           f'本地模型：{model}', 'python3 scripts/setup.py --download-model')
    notes = Path(settings['notes']['dir'])
    writable = notes.is_dir() and os.access(notes, os.W_OK) or (not notes.exists() and os.access(notes.parent, os.W_OK))
    report(writable, f'笔记目录可写：{notes}', '检查路径；macOS 后台服务写 iCloud/文稿目录可能需要在“隐私与安全性”里给 python 授权')
    if settings.get('feishu', {}).get('enabled'):
        report(bool(shutil.which('lark-cli')), '飞书：lark-cli 已安装', 'npm install -g @larksuite/cli && lark-cli auth login')
    report(port_open(settings['receiver']['port']), f'接收器在 127.0.0.1:{settings["receiver"]["port"]} 监听',
           '启动服务（见 setup.py 结束时打印的命令），或看 data/logs/receiver.log')
    port = settings['ingress']['port']
    try:
        status, body = get_json(f'http://127.0.0.1:{port}/health', 5)
        report(status == 200 and body.get('ok') is True, f'HTTPS 入口上游 127.0.0.1:{port}/health 正常')
    except Exception as exc:
        report(False, f'HTTPS 入口上游 127.0.0.1:{port} 不可用（{type(exc).__name__}）', '启动 ingress 服务，看 data/logs/ingress.log')
    if not skip_public:
        url = settings['public_endpoint'] + '/health'
        try:
            status, body = get_json(url, 15)
            report(status == 200 and body.get('ok') is True, f'公网 {url} 可达，证书受信任')
        except Exception as exc:
            fix = ('运行 tailscale funnel --bg --https=443 http://127.0.0.1:%d ，并在 Tailscale 管理后台允许 Funnel' % port
                   if settings['ingress']['type'] == 'tailscale' else '检查域名解析、证书和反向代理到 127.0.0.1:%d' % port)
            report(False, f'公网 {url} 不可达（{type(exc).__name__}: {str(exc)[:120]}）', fix)
    print('      注：health 正常只说明入口通了；手表第一段录音拿到加密回执才算真正配对成功。')
    db = home / 'state' / 'pipeline.sqlite3'
    if db.exists():
        import sqlite3
        con = sqlite3.connect(db)
        counts = {}
        for (payload,) in con.execute("SELECT payload FROM jobs WHERE id NOT LIKE 'daily:%'"):
            status = json.loads(payload).get('status', 'pending')
            counts[status] = counts.get(status, 0) + 1
        con.close()
        print(f'      处理统计：{counts or "还没有录音"}（done=已写笔记，no_speech=无语音不建笔记，failed=会自动重试）')


def check_sync(settings, home):
    cfg = settings['mac_sync']
    dest = Path(cfg['destination'])
    report(dest.is_dir(), f'本机目标文件夹：{dest}', '创建该文件夹或重跑 setup.py')
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', cfg['ssh_host'], 'echo', 'ok'],
                       capture_output=True, text=True, timeout=20)
    report(r.returncode == 0, f'SSH 免密登录 {cfg["ssh_host"]}', '在 Windows 启用 OpenSSH 服务器并配置密钥，见 docs/deployment.md')
    state = home / 'state' / 'mac-sync.json'
    if state.exists():
        s = json.loads(state.read_text(encoding='utf-8'))
        print(f'      上次拉取：{s.get("checked_at")} 状态 {s.get("status")}，本次新增 {len(s.get("copied", []))}，'
              f'冲突保留 {len(s.get("conflicts_preserved", []))}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--home', type=Path, default=Path(os.environ.get('XIAOLU_WATCH_HOME') or REPO / 'data'))
    p.add_argument('--skip-public', action='store_true')
    a = p.parse_args()
    home = a.home.resolve()
    path = home / 'settings.json'
    if not path.is_file():
        report(False, '还没有配置', 'python3 scripts/setup.py')
        return 1
    settings = json.loads(path.read_text(encoding='utf-8'))
    print(f'角色：{settings["role"]}  平台：{settings["platform"]}')
    if settings['role'] == 'receiver':
        check_receiver(settings, home, a.skip_public)
    else:
        check_sync(settings, home)
    print('\n全部通过。' if all(results) else '\n有需要处理的项目，按 -> 后的提示修复后重跑。')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
