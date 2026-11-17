# AI runbook

Step-by-step checklist for an AI agent bringing the system up from a clean clone.

## 0. Preconditions

Confirm with the user:

- They own/are authorized to test the phone number(s).
- The Android phone is rooted and `adb root` works.
- A Pi is reachable over SSH.
- They have a working RCS-registered Google Messages account.

Do not proceed if authorization is unclear.

## 1. Clone and configure

```bash
git clone <repo>
cd pi-rcs-client
cp .env.example .env
```

Ask the user for:

- `PI_HOST`
- `PI_USER` (usually `pi`)
- `PI_KEY`
- `SENDER` (their RCS number, E.164)
- `RECIPIENT` (test recipient, E.164)

Never echo `.env` contents.

Initialize the profile:

```bash
./scripts/init_profile.sh
```

This creates `pc/work/<profile>/meta.json` with `SENDER`.

## 2. PC setup

```bash
./scripts/setup_pc.sh
```

Expected: `.venv`, Python dependencies, `pc/agent/node_modules`, `pc/frida/token_harvester_bundle.js`.

## 3. Phone preparation

```bash
adb devices
adb root
adb shell "ps -A | grep -i frida || nohup /data/local/tmp/frida-server >/data/local/tmp/frida.log 2>&1 &"
adb shell am start -n com.google.android.apps.messaging/.ui.ConversationListActivity
```

Check:

```bash
adb shell pidof com.google.android.apps.messaging
adb shell pidof com.google.android.apps.messaging:rcs
adb logcat -d -b main -v time | grep -iE "RegisterRefresh|PERMISSION_DENIED" | tail
```

If `PERMISSION_DENIED`, stop and report. Do not restart the app repeatedly.

## 4. Start harvester

```bash
./scripts/start_harvester.sh
```

Watch for:

```text
token updated ...
client_info updated ...
send_template.pb captured ...
recv headers updated ...
recv frame updated ...
```

If only E2EE messages are sent by the phone, `send_template.pb` may not be captured; the Pi web app falls back to a sanitized example template and `SENDER`.

Ask the user to send one plaintext RCS message from the phone to the test recipient. This also generates a fresh SendMessage `client_info`.

## 5. MLS snapshot

```bash
python3 pc/mls_state.py export --profile default --push
```

Expected files:

```
pc/work/default/mls_snapshot.bin
pc/work/default/mls_group.json
```

If `mls_group_states` has no row, verify `pc/work/default/meta.json` has the correct `sender`.

## 6. Build `mls_probe` on the Pi

On the Pi:

```bash
./mls/build_mls_probe.sh
```

Expected: `/home/pi/mls_probe/target/release/mls_probe`.

If the build fails:

- `cargo` missing: install Rust.
- OpenSSL headers missing: `sudo apt install libssl-dev pkg-config`.
- Patch already applied: the script detects it.

## 7. Deploy Pi web app

From the PC:

```bash
./deploy/install_pi.sh
```

Check:

```bash
ssh pi@$PI_HOST 'sudo systemctl status rcs-web --no-pager | tail'
curl -sS http://$PI_HOST:8090/api/state
```

## 8. End-to-end verification

1. Open `http://<PI_HOST>:8090`.
2. Start a conversation with the test recipient.
3. Send a message.
4. Expected: API `status: 0`; if the recipient replies, the Pi decrypts and displays it.
5. Check the receiver log:

```bash
ssh pi@$PI_HOST 'tail -n 30 ~/rcsclient/work/default/web_receiver.log'
```

## 9. If the phone registration is blocked

Symptoms:

```text
PERMISSION_DENIED
recoveryType=ERROR_NO_RETRY
```

Actions:

- Stop restarting the app/network.
- Wait (often hours).
- Check Google Messages RCS settings.
- Ask the user before trying official disable/re-enable or a different SIM/device.

## 10. Cleanup

- Stop the harvester with Ctrl-C.
- Stop the web service if needed: `sudo systemctl stop rcs-web`.
- Never commit `work/`, `.env`, tokens, snapshots, or captures.
