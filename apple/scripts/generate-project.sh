#!/bin/bash
# Generate apple/WatchMemory/WatchMemory.xcodeproj from project.yml (XcodeGen).
# Signing values come from apple/WatchMemory/Config/Local.xcconfig (python3 scripts/setup.py --apple-only).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../WatchMemory" && pwd)"
command -v xcodegen >/dev/null || { echo "需要 XcodeGen：brew install xcodegen" >&2; exit 2; }
if [ ! -f "$HERE/Config/Local.xcconfig" ]; then
  echo "还没有签名配置：先运行 python3 scripts/setup.py --apple-only（或复制 Config/Local.xcconfig.example）" >&2
  exit 2
fi
cd "$HERE"
xcodegen generate --spec project.yml
echo "已生成：$HERE/WatchMemory.xcodeproj  —— 用 Xcode 打开，按 docs/apple-build.md 安装到手表。"
