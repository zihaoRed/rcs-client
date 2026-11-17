#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
set -a; [ -f .env ] && . ./.env; set +a
: "${PI_HOST:?PI_HOST is required}"
PORT="${WEB_PORT:-8090}"
echo "== phone =="
adb shell pidof com.google.android.apps.messaging || true
adb shell pidof com.google.android.apps.messaging:rcs || true
echo "== recent registration =="
adb logcat -d -b main -v time 2>/dev/null | grep -iE "RegisterRefresh|PERMISSION_DENIED|onTachyonRegistration" | tail -10 || true
echo "== web =="
PYTHON=${PYTHON:-python3}
if ! "$PYTHON" -V >/dev/null 2>&1; then PYTHON=python; fi
curl -sS "http://${PI_HOST}:${PORT}/api/state" | "$PYTHON" -m json.tool | head -60
