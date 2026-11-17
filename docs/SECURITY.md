# Security and authorization

## Authorized use

Use this project only with:

- phone numbers you own, or
- recipients who have explicitly agreed to testing, or
- an isolated lab/CTF environment

Do not use it for unsolicited messaging, harassment, spam, or mass targeting.

## Phone root

This architecture requires root (or equivalent debug privileges) on the Android phone because it:

- attaches Frida to Google Messages to capture Tachyon credentials
- reads `bugle_db` to export the MLS group state and message history
- runs `frida-server`
- can invoke `SignupEngine` for recovery

Root has real risks:

- weakens the phone's security model
- may break banking/Play Integrity/OTA
- increases the impact of a compromised app
- is not appropriate for a normal user's daily phone

Use a dedicated spare device where possible.

## Sensitive artifacts

Never commit or share:

- `.env`
- `pc/work/**`
- `/home/pi/rcsclient/work/**`
- `send_headers.txt`
- `client_info*.bin`
- `recv_phone_*`
- `mls_snapshot*`
- `token_*`
- `*.jsonl` captures
- `bugle_db` or any phone database
- APKs / Frida binaries

These files are gitignored. They contain live session credentials, MLS keys, or personal data.

## Credential handling

- Treat tokens and client_info as bearer credentials for the RCS account.
- Do not paste them into logs, issues, chat, or screenshots.
- Do not replay them from untrusted networks.
- Rotate/refresh by re-running the harvester with the phone.
- The Pi stores them under its work directory; restrict filesystem access.

## Web UI

- The web UI has **no authentication**.
- Bind it to a trusted LAN or localhost behind a reverse proxy.
- Use a firewall/VPN; never expose port 8090 directly to the internet.
- The web UI displays message contents and contact numbers; treat it as sensitive.

## MLS state

The MLS snapshot is effectively the group's decryption state. If leaked, it may allow decryption of messages within that epoch. Store it with the same care as credentials.

## Legal / terms

RCS/Tachyon is a Google/Jibe service. This project reverse-engineers client behavior for personal interoperability testing. Check the terms that apply to your account, carrier, and jurisdiction. Do not use it to bypass authorization or access data you do not own.

## Reporting

If you accidentally commit a secret:

1. Revoke/refresh the affected RCS session by re-registering the phone.
2. Remove the secret from git history (do not just delete the file in a new commit).
3. Rotate any SSH keys or tokens that were exposed.
