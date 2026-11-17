# AGENTS.md

This repository is an authorized personal RCS research project.

All AI agents must read `CLAUDE.md` and `docs/SECURITY.md` before running commands.

Key rules:

- Use only numbers you own or are explicitly authorized to test.
- Never commit or print `.env`, tokens, client_info, MLS snapshots, phone databases, or captures.
- The Android phone must be rooted; the architecture depends on Frida + bugle_db access.
- Do not repeatedly reboot the phone or toggle networks; Google may block RCS registration.
- The web UI has no authentication; keep it on a trusted LAN.
- Pi E2EE send is not supported; Pi sends plaintext RCS.

The operational checklist is in `CLAUDE.md` and `docs/AI_RUNBOOK.md`.
