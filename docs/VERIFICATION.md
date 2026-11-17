# Verification status

Last updated: 2026-11-17

This document records what has been verified on a personal rooted Android device and a Raspberry Pi. Numbers, tokens, snapshots, and captures are intentionally omitted.

## Summary

| Area | Result |
|---|---|
| Pi plaintext SendMessage | ✅ verified (`grpc-status: 0`, recipient received) |
| Pi direct ReceiveMessages | ✅ verified with phone ReceiveMessages credentials |
| E2EE inbound decrypt | ✅ verified with `mls_probe` + phone snapshot |
| MLS commit / epoch advance | ✅ verified (epoch 4 → 5) |
| Web UI / SQLite / SSE | ✅ verified |
| Phone online + screen off → Pi receive | ✅ verified |
| Pi E2EE send | ❌ rejected by server (`MlsError`) |
| Phone RCS registration stability | ⚠️ can be blocked by Google |
| Unrooted phone | ❌ not supported |

## Credential findings

- SendMessage and ReceiveMessages use different credentials.
  - SendMessage: token ≈1715 chars, `client_info` 275 bytes.
  - ReceiveMessages: token ≈1776 chars, `client_info` 230 bytes.
- Using SendMessage credentials for ReceiveMessages produced only control/IMDN and a cancelled stream.
- `PrewarmReceiver` request field 1 contains a 230-byte client_info that can be used to synthesize a ReceiveMessages frame.
- A SendMessage token observed at 14:53 was still accepted at 20:11 (≥5h18m).
- A newer SendMessage token captured at 18:36 was still accepted at 20:35 (≈119 min).
- A `client_info` captured at 19:38 was still accepted at 20:35 (≈57.5 min).
- A ReceiveMessages token captured at 20:18 returned 16 at 20:46 (≈28 min), but this interval included app crashes/restarts; the exact TTL needs remeasurement.

## E2EE findings

- Inbound `message/mls` frames were decrypted successfully with the phone's MLS group snapshot.
- The phone and Pi may both receive the same message; the Pi deduplicates by RCS message id.
- RCS commits use `MB + MLS PublicMessage` nested inside a protobuf wrapper.
- `mls-rs` needed:
  - X.509 identity support
  - custom credential-type support
  - relaxed credential-capability checks for RCS commits
- After processing the commit, epoch-5 application messages decrypted successfully.
- Outbound E2EE from the Pi is rejected (`grpc-status: 3 MlsError`); the Pi sends plaintext RCS only.

## Web UI findings

- Flask + SQLite runs as a systemd service (`rcs-web`).
- The UI supports contacts, new conversations, live chat, delivery/read status, typing indicators, and an auth-expired banner.
- SSE pushes new messages; the page also polls every 20 seconds.
- Sending through the web API returns `status: 0`; delivery IMDNs update the message to `delivered`.
- The receiver supervisor reloads credential files without restarting the service.

## Registration instability

During testing, Google Tachyon repeatedly returned:

```text
RegistrationProvider: Got StatusRuntimeException for RegisterRefresh
status: PERMISSION_DENIED: The caller does not have permission
recoveryType=ERROR_NO_RETRY
```

It persisted across app restarts and a phone reboot for hours. The UI could still show cached `ConfiguredState`, so logcat is the authoritative signal.

Recommendations:

- Do not repeatedly restart the app, toggle networks, or disable/re-enable RCS.
- Wait for cooldown.
- If it persists, consider the official disable/re-enable flow (risk of a longer block) or a different SIM/device.

## Root dependency

The current architecture requires phone root (or equivalent debug privileges) for:

- Frida attach to Google Messages
- reading `bugle_db` for MLS state and message history
- starting `frida-server`
- invoking `SignupEngine` during recovery

The Pi does not require root; the web service runs as the `pi` user.

## What is not claimed

- No production-grade reliability; Frida can crash Messages on some builds.
- No unrooted-device support.
- No Pi-originated E2EE.
- No exclusive receive; the phone may receive duplicates.
- No web authentication.
