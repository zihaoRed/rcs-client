#!/usr/bin/env bash
set -euo pipefail
ENV_FILE=/home/pi/rcsclient/.env
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
FRIDA_SERVER=${FRIDA_SERVER:-/data/local/tmp/frida-server17}
MESSAGES_PKG=com.google.android.apps.messaging

echo "waiting for android device..."
found=0
for _ in $(seq 1 90); do
  if adb devices | awk '$2 == "device" { found=1 } END { exit found ? 0 : 1 }'; then
    found=1
    break
  fi
  sleep 2
done
if [ "$found" != "1" ]; then
  echo "ERROR: no authorized Android device after 180s" >&2
  exit 1
fi
adb wait-for-device

# Root is required by the harvester for Messages process access.
if ! adb shell id 2>/dev/null | grep -q 'uid=0'; then
  adb root >/dev/null 2>&1 || true
  sleep 1
  adb wait-for-device
fi

# Start frida-server if it is not already running.
if ! adb shell "ps -A 2>/dev/null | grep -q '[f]rida-server'" >/dev/null 2>&1; then
  echo "starting $FRIDA_SERVER"
  adb shell "nohup $FRIDA_SERVER >/data/local/tmp/frida.log 2>&1 &" || true
  sleep 1
fi

# Wake/foreground the phone if possible (a secure PIN still requires manual unlock).
adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true
adb shell wm dismiss-keyguard >/dev/null 2>&1 || true

# Bring Messages to the foreground. This is a no-op if it is already there;
# it does not force-stop or restart the app.
adb shell am start -n "$MESSAGES_PKG/.ui.ConversationListActivity" >/dev/null 2>&1 || true
sleep 2

echo "android device ready"
