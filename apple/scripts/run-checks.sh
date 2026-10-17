#!/bin/bash
# Platform-independent checks of the shared Swift core (runs with Xcode or Command Line Tools).
set -euo pipefail
PKG="$(cd "$(dirname "$0")/../WatchMemory" && pwd)"
swift run --package-path "$PKG" CoreChecks
swift run --package-path "$PKG" SchedulingChecks
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Swift CryptoKit -> Python cryptography -> Swift verifier, using the PUBLIC fixture secret only.
swift run --package-path "$PKG" WireV2Checks emit-fixture "$TMP/wire-v2-public.json"
python3 "$PKG/Tests/verify_wire_v2.py" "$TMP/wire-v2-public.json" "$TMP/wire-v2-python-receipt.json"
swift run --package-path "$PKG" WireV2Checks verify-fixture "$TMP/wire-v2-python-receipt.json"
