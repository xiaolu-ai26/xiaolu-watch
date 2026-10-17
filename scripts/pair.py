#!/usr/bin/env python3
"""Issue a one-time watch pairing code (80-bit, valid 10 minutes) on the receiver computer.

    python3 scripts/pair.py               # writes a private pairing card + onboarding.json
    python3 scripts/pair.py --show-code   # also prints the code in this terminal

The long-term secret never leaves the receiver until the watch claims the code over HTTPS
(`POST /v2/pair/claim`, served by the ingress). Only the code's SHA-256 is stored.
Then deliver it to the watch (see docs/apple-build.md):
  * Mac with Xcode, zero typing:  python3 scripts/deliver_onboarding.py
  * or type the HTTPS address + code on the watch: 小鹿录音 > 连接处理电脑
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'receiver'))
from bootstrap_pairing import Bootstrap  # noqa: E402
from platform_store import replace, sync_directory  # noqa: E402


def private_write(path, text):
    temp = path.with_name(path.name + '.pending')
    with temp.open('w', encoding='utf-8') as f:
        if os.name != 'nt':
            os.chmod(temp, 0o600)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    replace(temp, path)
    sync_directory(path.parent)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--home', type=Path, default=Path(os.environ.get('XIAOLU_WATCH_HOME') or REPO / 'data'))
    p.add_argument('--endpoint', help='override the public HTTPS endpoint from settings.json')
    p.add_argument('--show-code', action='store_true')
    a = p.parse_args()
    settings = json.loads((a.home / 'settings.json').read_text(encoding='utf-8'))
    if settings.get('role') != 'receiver':
        raise SystemExit('Run this on the receiver computer.')
    root = Path(settings['receiver']['root'])
    endpoint = (a.endpoint or settings['public_endpoint']).rstrip('/')
    code, expires = Bootstrap(root).issue(endpoint)
    grouped = '-'.join(code[i:i + 4] for i in range(0, 16, 4))
    until = dt.datetime.fromtimestamp(expires).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    private = root / 'private'
    card = private / 'watch-pairing-card.txt'
    onboarding = private / 'onboarding.json'
    private_write(card, f'小鹿录音：手表首次连接\n\nHTTPS 地址：{endpoint}\n\n一次性配对码：{grouped}\n\n'
                        f'到期时间：{until}\n\n仅用于本次手表初始化，勿公开、勿截图分享。\n')
    private_write(onboarding, json.dumps({'endpoint': endpoint, 'code': code}) + '\n')
    print(f'已生成一次性配对码，{until} 前有效（10 分钟，只能被一块手表认领）。')
    print(f'  私有配对卡：{card}')
    print(f'  自动投递文件：{onboarding}')
    if a.show_code:
        print(f'  HTTPS 地址：{endpoint}\n  配对码：{grouped}')
    print('\n下一步（二选一）：')
    print('  A. 在装了 Xcode 的 Mac 上零输入投递：python3 scripts/deliver_onboarding.py --file <上面的 onboarding.json>')
    print('  B. 在手表 小鹿录音 > 连接处理电脑 里输入 HTTPS 地址和配对码')
    print('确认 HTTPS 入口（ingress + Funnel/反向代理）正在运行，否则手表无法认领。')


if __name__ == '__main__':
    main()
