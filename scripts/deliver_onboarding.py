#!/usr/bin/env python3
"""Mac + Xcode: deliver the short-term pairing file to the watch app, no typing on the watch.

    python3 scripts/deliver_onboarding.py                       # uses data/receiver/private/onboarding.json
    python3 scripts/deliver_onboarding.py --file onboarding.json --device "My Apple Watch"
    python3 scripts/deliver_onboarding.py --endpoint https://... --code ABCD-EFGH-IJKL-MNOP

What it does: `xcrun devicectl device copy to` puts onboarding.json (HTTPS address + one-time
code only, never the long-term secret) into the watch app's own data container at
Library/Application Support/WatchMemory/onboarding.json, then launches the app. The app
validates the file, stores the code in its Keychain, deletes the file and claims its
long-term configuration over HTTPS. Keep the app in the foreground (wrist raised) for about
a minute so the claim can run.

Requirements: the watch app was installed from your Xcode, opened at least once, and the
watch is paired in Xcode (Window > Devices and Simulators). The mechanism was verified on
the author's devices; this script itself has not been tested on a clean machine.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
DESTINATION = 'Library/Application Support/WatchMemory/onboarding.json'


def bundle_id():
    local = REPO / 'apple/WatchMemory/Config/Local.xcconfig'
    if local.exists():
        m = re.search(r'^XIAOLU_WATCH_BUNDLE_ID\s*=\s*(\S+)', local.read_text(encoding='utf-8'), re.M)
        if m:
            return m.group(1)
    return None


def devicectl(*args, timeout=120):
    return subprocess.run(['xcrun', 'devicectl', *args], capture_output=True, text=True, timeout=timeout)


def watch_devices():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'devices.json'
        r = devicectl('list', 'devices', '--json-output', str(out), timeout=60)
        if r.returncode or not out.exists():
            raise SystemExit('devicectl 无法列出设备：' + (r.stderr or r.stdout)[-500:] +
                             '\n确认已安装完整 Xcode，并用 xcode-select 或 DEVELOPER_DIR 指向它。')
        data = json.loads(out.read_text(encoding='utf-8'))
    found = []
    for d in data.get('result', {}).get('devices', []):
        hw = d.get('hardwareProperties', {})
        if str(hw.get('platform', '')).lower() == 'watchos':
            found.append((d.get('identifier'), d.get('deviceProperties', {}).get('name', '?')))
    return found


def main():
    if sys.platform != 'darwin':
        raise SystemExit('需要在装有 Xcode 的 Mac 上运行。')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--file', type=Path, default=REPO / 'data/receiver/private/onboarding.json')
    p.add_argument('--endpoint')
    p.add_argument('--code')
    p.add_argument('--device', help='devicectl 设备标识或名称；只连了一块手表时可省略')
    p.add_argument('--bundle-id', help='iPhone App 的 Bundle ID（默认读 Local.xcconfig）；手表 App 会自动加 .watchkitapp')
    a = p.parse_args()

    if a.endpoint or a.code:
        if not (a.endpoint and a.code):
            raise SystemExit('--endpoint 和 --code 要一起给。')
        payload = {'endpoint': a.endpoint.rstrip('/'), 'code': a.code.replace('-', '').replace(' ', '').upper()}
    else:
        if not a.file.is_file():
            raise SystemExit(f'找不到 {a.file}。先在接收端运行 python3 scripts/pair.py，或改用 --endpoint/--code。')
        payload = json.loads(a.file.read_text(encoding='utf-8'))
    if set(payload) != {'endpoint', 'code'} or not payload['endpoint'].startswith('https://') or not re.fullmatch(r'[A-Z2-7]{16}', payload['code']):
        raise SystemExit('投递内容必须只有 HTTPS endpoint 和 16 位配对码。')

    phone_bundle = a.bundle_id or bundle_id()
    if not phone_bundle:
        raise SystemExit('不知道 App 的 Bundle ID：先运行 python3 scripts/setup.py --apple-only，或传 --bundle-id。')
    watch_bundle = phone_bundle + '.watchkitapp'

    device = a.device
    if not device:
        watches = watch_devices()
        if len(watches) != 1:
            listing = '\n'.join(f'  {i}  {n}' for i, n in watches) or '  （没有发现手表：先在 Xcode 的 Devices 里完成手表配对，见 docs/troubleshooting.md）'
            raise SystemExit('请用 --device 指定手表：\n' + listing)
        device, name = watches[0]
        print(f'目标手表：{name}')

    with tempfile.TemporaryDirectory() as td:
        os.chmod(td, 0o700)
        staged = Path(td) / 'onboarding.json'
        staged.write_text(json.dumps(payload), encoding='utf-8')
        os.chmod(staged, 0o600)
        r = devicectl('device', 'copy', 'to', '--device', device, '--domain-type', 'appDataContainer',
                      '--domain-identifier', watch_bundle, '--source', str(staged), '--destination', DESTINATION)
    if r.returncode:
        raise SystemExit('投递失败：' + (r.stderr or r.stdout)[-800:] +
                         '\n常见原因：手表 App 还没打开过一次；手表锁屏；Xcode 与手表的开发连接断开。')
    print('已投递到手表 App。正在启动 App…')
    r = devicectl('device', 'process', 'launch', '--device', device, watch_bundle)
    if r.returncode:
        print('自动启动没成功（手表锁屏时常见）。请在手表上解锁并手动打开 小鹿录音。')
    print('请抬腕让 App 保持前台约 1 分钟；显示“处理电脑已连接”即完成。可在接收端运行 python3 scripts/doctor.py 查看。')


if __name__ == '__main__':
    main()
