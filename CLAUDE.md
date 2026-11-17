# CLAUDE.md — AI onboarding and runbook

You are working on **Pi RCS Client**: an independent Raspberry Pi RCS client that uses a rooted Android phone only as the Tachyon registration/credential anchor.

Read this file before doing anything. Also read:

- `README.md` — overview and quick start
- `docs/ARCHITECTURE.md` — component/data-flow details
- `docs/VERIFICATION.md` — what has been proven and what is known to fail
- `docs/SECURITY.md` — authorization and sensitive-data rules
- `docs/AI_RUNBOOK.md` — step-by-step operational checklist

---

## Non-negotiable safety rules

1. **Only use phone numbers you own or are explicitly authorized to test.** Do not send to third parties or build mass-messaging features.
2. **Never commit or print secrets.** This includes:
   - `.env`
   - `pc/work/**` and `/home/pi/rcsclient/work/**`
   - `send_headers.txt`, `client_info*.bin`, `recv_phone_*`, `*_snapshot*`, `token_*`, `*.jsonl` captures
   - tokens, MLS snapshots, phone databases, APKs
3. **The phone must be authorized and rooted** for this architecture. Do not attempt to root someone else's device.
4. **Do not reboot the phone, toggle airplane mode/Wi-Fi, force-stop Messages, or repeatedly open conversations without asking the user.** These actions can trigger a Google RCS registration block (`PERMISSION_DENIED` / `ERROR_NO_RETRY`).
5. If the phone returns `PERMISSION_DENIED`, **stop retrying** and report. Wait for cooldown or ask the user before any recovery action.
6. The web UI has no authentication. Do not expose port 8090 to the public internet.

---

## What the project does

- `pc/token_harvester.py` (PC) attaches Frida to Google Messages and captures:
  - Tachyon token (`x-goog-ext-202964622-bin`)
  - SendMessage `client_info` (275 bytes)
  - plaintext `send_template.pb`
  - ReceiveMessages headers + frame (230-byte `client_info`)
- `pc/mls_state.py` (PC) exports the phone MLS snapshot from `bugle_db`.
- `pi/rcs_web.py` (Pi) is a Flask app + SQLite + SSE + receiver supervisor.
- `pi/pi_receiver.py` (Pi) opens ReceiveMessages, decrypts `message/mls`, updates the snapshot.
- `mls/mls_probe` (Pi) is a Rust `mls-rs` CLI; it uses a patched `mls-rs` to accept RCS commits.

The Pi sends **plaintext** RCS. Pi-originated E2EE send is not supported and returns `MlsError`.

---

## Prerequisites

- Rooted Android phone with Google Messages + working RCS
- USB debugging; `adb root` works
- `frida-server` at `/data/local/tmp/frida-server`
- PC with Python 3.10+, Node.js 18+, adb, SSH key to Pi
- Pi with Python 3.10+, Flask, curl, Rust/cargo, libssl-dev, systemd
- **Mode B (all-in-one)**: Pi also needs `adb`, `frida` Python, Node.js/npm,
  USB access to the phone, and preferably a powered USB hub

If a prerequisite is missing, report it instead of trying to bypass security controls.

---

## Deployment modes

### Mode A — PC harvester + Pi web

The existing fast path below. The PC runs `token_harvester.py` and `mls_state.py` and pushes to the Pi over SSH.

### Mode B — Pi all-in-one

The Pi runs adb + Frida + MLS export + web itself. The phone connects to the Pi by USB.

```bash
# on the Pi
SENDER=+1XXXXXXXXXX FRIDA_SERVER=/data/local/tmp/frida-server17   ./deploy/install_pi_allinone.sh
sudo systemctl status rcs-harvester rcs-web --no-pager
sudo systemctl start rcs-mls-sync   # after phone is connected/registered
```

Services: `rcs-harvester`, `rcs-web`, `rcs-mls-sync`.
Work dir: `/home/pi/rcsclient/work/<profile>`.
No SSH push is used; the harvester writes locally.

See `docs/ALLINONE.md` for hardware, USB/udev, and troubleshooting.

---

## Fast path (AI runbook) — Mode A

### 0. Inspect state

```bash
git status
test -f .env && echo ".env present" || echo ".env missing"
python3 --version
adb devices
```

Do not print `.env` contents; only check that required keys exist.

### 1. Configure

```bash
cp .env.example .env    # only if missing
# ask the user for PI_HOST, PI_USER, PI_KEY, SENDER if not set
./scripts/init_profile.sh
```

### 2. PC setup

```bash
./scripts/setup_pc.sh
# Windows: .\scripts\setup_pc.ps1
```

### 3. Phone preparation

```bash
adb devices
adb root
adb shell "ps -A | grep -i frida || nohup /data/local/tmp/frida-server >/data/local/tmp/frida.log 2>&1 &"
adb shell am start -n com.google.android.apps.messaging/.ui.ConversationListActivity
```

Confirm Messages has RCS available. Do not restart the app repeatedly.

### 4. Harvest credentials

```bash
./scripts/start_harvester.sh
```

Then ask the user to send **one plaintext RCS message** from the phone to a test number. Watch for:

```text
token updated ...
client_info updated ...
send_template.pb captured ...
recv headers updated ...
recv frame updated ...
```

### 5. Export MLS snapshot

```bash
python3 pc/mls_state.py export --profile default --push
```

Requires `adb root`. This writes `pc/work/default/mls_snapshot.bin`.

### 6. Build `mls_probe` on the Pi (Mode A)

On the Pi:

```bash
./mls/build_mls_probe.sh
```

It clones `awslabs/mls-rs` tag `0.51.0`, applies the patch in `mls/patches/`, builds, and installs to `/home/pi/mls_probe/target/release/mls_probe`.

### 7. Deploy the Pi web app (Mode A)

From the PC:

```bash
./deploy/install_pi.sh
```

For Mode B, use `deploy/install_pi_allinone.sh` from the Pi instead.

### 8. Verify

```bash
./scripts/status.sh
curl -sS http://$PI_HOST:8090/api/state
```

Open `http://<PI_HOST>:8090`. Send a test message through the UI. Expected:

- `POST /api/send` returns `{"ok": true, "status": "0"}`
- delivery IMDN updates status to `delivered`
- if the other side replies, the Pi receiver decrypts `message/mls` and the UI shows it

---

## Current known behavior (do not rediscover)

- SendMessage and ReceiveMessages use **different credentials**:
  - SendMessage: token ~1715 chars, client_info 275 bytes
  - ReceiveMessages: token ~1776 chars, client_info 230 bytes
- Using SendMessage credentials for ReceiveMessages yields only control/IMDN and a cancelled stream.
- `PrewarmReceiver` request field 1 contains a 230-byte client_info that can be reused to synthesize a ReceiveMessages frame.
- The phone and Pi may both receive the same message; the Pi dedupes by `rcs_id` but does not suppress the phone copy.
- Pi E2EE send is rejected (`grpc-status: 3 MlsError`).
- Google can return `PERMISSION_DENIED` / `ERROR_NO_RETRY` for Tachyon registration after churn. Stop testing and wait.

---

## Common failures

| Symptom | Likely cause | Action |
|---|---|---|
| `grpc-status: 16` | stale token/client_info | keep harvester running; send from phone; check fresh files |
| `AUTH_EXPIRED` in UI | receiver old creds | verify `recv_phone_*` mtime and receiver log |
| `send_template.pb` missing | no plaintext phone SendMessage captured | example template fallback requires `SENDER`; or capture a plaintext message |
| `PERMISSION_DENIED` | RCS registration blocked | stop, wait, do not reboot repeatedly |
| `EpochNotFound` | MLS snapshot behind | process commit / re-export phone snapshot |
| `KeyMissing(0)` | out-of-order ratchet | build with `out_of_order` (already enabled) / process in order |
| Messages crashes on attach | Frida instability | foreground app, avoid repeated attach; use spare device |
| `rcs-harvester` restarts | no ADB/device, wrong frida-server path, locked phone | `journalctl -u rcs-harvester`, `adb devices`, check `FRIDA_SERVER` |
| all-in-one web `AUTH_EXPIRED` | local recv credentials stale | keep `rcs-harvester` running, send from phone, check `work/<profile>/recv_phone_*` |

---

## Commands worth remembering

```bash
# status
./scripts/status.sh

# harvester
python3 pc/token_harvester.py --profile default --pi-push

# MLS export
python3 pc/mls_state.py export --profile default --push

# deploy Pi
./deploy/install_pi.sh

# Pi service
ssh pi@$PI_HOST 'sudo systemctl status rcs-web --no-pager'
ssh pi@$PI_HOST 'tail -n 30 ~/rcsclient/work/default/web_receiver.log'
```

When in doubt, inspect and report rather than making destructive changes.
