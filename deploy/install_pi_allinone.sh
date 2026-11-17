#!/usr/bin/env bash
set -euo pipefail

# Run this ON THE PI from the repo root.
# Example:
#   SENDER=+1XXXXXXXXXX ./deploy/install_pi_allinone.sh

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TARGET=/home/pi/rcsclient
PROFILE=${PROFILE:-default}
SENDER=${SENDER:-}
FRIDA_SERVER=${FRIDA_SERVER:-/data/local/tmp/frida-server17}
WEB_PORT=${WEB_PORT:-8090}
MLS_PROBE=${MLS_PROBE:-/home/pi/mls_probe/target/release/mls_probe}

if [ -z "$SENDER" ]; then
  echo "ERROR: set SENDER to your RCS account number, e.g. SENDER=+1XXXXXXXXXX $0" >&2
  exit 1
fi

echo "== installing system packages =="
sudo apt-get update
sudo apt-get install -y adb python3-venv python3-pip git libssl-dev pkg-config || true
sudo apt-get install -y android-sdk-platform-tools-common || true
if ! command -v npm >/dev/null 2>&1; then
  sudo apt-get install -y nodejs npm
fi
export PATH="$HOME/.cargo/bin:$PATH"
if ! command -v cargo >/dev/null 2>&1; then
  echo "ERROR: cargo not found. Install Rust (https://rustup.rs) and rerun." >&2
  exit 1
fi

echo "== building Frida agent bundle =="
if [ ! -f "$ROOT/pc/frida/token_harvester_bundle.js" ]; then
  (cd "$ROOT/pc/agent" && npm ci && npm run build)
fi

echo "== building mls_probe (if missing) =="
if [ ! -x "$MLS_PROBE" ]; then
  (cd "$ROOT" && ./mls/build_mls_probe.sh)
fi

echo "== installing runtime into $TARGET =="
mkdir -p "$TARGET/tools/harvester/frida" "$TARGET/tools/harvester/agent" "$TARGET/work/$PROFILE"
cp -a "$ROOT/pi/." "$TARGET/tools/"
cp "$ROOT"/pc/*.py "$TARGET/tools/harvester/"
cp "$ROOT/pc/frida/token_harvester_bundle.js" "$TARGET/tools/harvester/frida/"
cp "$ROOT/pc/agent/token_harvester.js" "$ROOT/pc/agent/package.json" "$ROOT/pc/agent/package-lock.json" \
   "$TARGET/tools/harvester/agent/"
cp "$ROOT/deploy/wait_for_adb.sh" "$TARGET/tools/harvester/"
cp "$ROOT/requirements.txt" "$TARGET/tools/"
chmod +x "$TARGET/tools/harvester/wait_for_adb.sh"

echo "== creating venv =="
python3 -m venv "$TARGET/venv"
"$TARGET/venv/bin/pip" install --upgrade pip
"$TARGET/venv/bin/pip" install -r "$TARGET/tools/requirements.txt"

echo "== writing config =="
cat > "$TARGET/.env" <<ENV
PROFILE=$PROFILE
SENDER=$SENDER
MLS_PROBE=$MLS_PROBE
FRIDA_SERVER=$FRIDA_SERVER
WEB_PORT=$WEB_PORT
PI_HOST=127.0.0.1
PI_USER=pi
ENV
chmod 600 "$TARGET/.env"

"$TARGET/venv/bin/python" - "$TARGET" "$PROFILE" "$SENDER" <<'PY'
import json, sys
from pathlib import Path
target, profile, sender = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(target) / "work" / profile
p.mkdir(parents=True, exist_ok=True)
(p / "meta.json").write_text(
    json.dumps({"profile": profile, "sender": sender}, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
print("wrote", p / "meta.json")
PY

echo "== installing udev + systemd =="
sudo cp "$ROOT/deploy/99-android-adb.rules" /etc/udev/rules.d/99-android-adb.rules
sudo udevadm control --reload-rules
sudo udevadm trigger || true
sudo cp "$ROOT/deploy/rcs-web.service" "$ROOT/deploy/rcs-harvester.service" \
        "$ROOT/deploy/rcs-mls-sync.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rcs-web rcs-harvester >/dev/null 2>&1
sudo systemctl restart rcs-web rcs-harvester

cat <<EOF

All-in-one install complete.

Next:
  1. Connect the rooted phone to the Pi by USB (use a powered hub if needed).
  2. Accept the USB debugging prompt on the phone.
  3. If frida-server is not at $FRIDA_SERVER, set FRIDA_SERVER in $TARGET/.env
     and run: sudo systemctl restart rcs-harvester
  4. Export the MLS snapshot after the phone is registered:
       sudo systemctl start rcs-mls-sync
  5. Open http://<PI_LAN_IP>:$WEB_PORT
  6. Follow docs/AI_RUNBOOK.md for recovery and troubleshooting.
EOF
