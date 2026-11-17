#!/usr/bin/env python3
"""mls_state.py — sync the phone's MLS group state + message template to a profile.

Reads bugle_db (needs adb root):
  - mls_group_states.epoch_data  -> mls_snapshot.bin
  - latest mls_messages row      -> mls_group.json (epoch/era/auth) + template envelope
and optionally pushes them to the Pi.

Commands:
  export [--profile default] [--push]
  check  [--profile default]          # exit 2 if the phone state changed since export
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
DB = "/data/data/com.google.android.apps.messaging/databases/bugle_db"
PI_HOST = os.environ.get("PI_HOST", "")
PI_USER = os.environ.get("PI_USER", "pi")
PI_KEY = os.environ.get("PI_KEY", os.path.expanduser("~/.ssh/id_ed25519"))


def sh(args: list[str], timeout: int = 30) -> str:
    p = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.stdout


def adb_shell(cmd: str, timeout: int = 30) -> str:
    return sh(["adb", "shell", cmd], timeout)


def ensure_root() -> None:
    out = adb_shell("id", 10)
    if "uid=0" not in out:
        sh(["adb", "root"], 20)
        import time
        time.sleep(1)


def sql(q: str) -> str:
    return adb_shell(f"sqlite3 {DB} \"{q}\"", 30).strip()


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export(profile: str, push: bool) -> int:
    prof = WORK / profile
    prof.mkdir(parents=True, exist_ok=True)
    meta = {}
    try:
        meta = json.loads((prof / "meta.json").read_text(encoding="utf-8"))
    except OSError:
        pass
    sender = str(meta.get("sender", ""))
    if not sender:
        print("missing meta.json sender in", prof)
        return 1
    ensure_root()
    row = sql(f"select group_id||'|'||hex(epoch_data) from mls_group_states where identity_id='{sender}'")
    if not row or "|" not in row:
        print(f"no mls_group_states row for {sender}")
        return 1
    group_id, hexdata = row.split("|", 1)
    snap = bytes.fromhex(hexdata)
    (prof / "mls_snapshot.bin").write_bytes(snap)

    row2 = sql(
        "select rcs_message_id||'|'||mls_epoch_id||'|'||mls_era_id||'|'||"
        "hex(epoch_authenticator)||'|'||hex(encrypted_cpim_content)||'|'||hex(raw_cpim_content) "
        f"from mls_messages where mls_group_id='{group_id}' order by encryption_timestamp desc limit 1"
    )
    info = {
        "identity": sender,
        "group_id": group_id,
        "snapshot_sha": hashlib.sha256(snap).hexdigest(),
        "snapshot_len": len(snap),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }
    if row2 and row2.count("|") >= 5:
        mid, epoch, era, auth, env, raw = row2.split("|", 5)
        auth_b = bytes.fromhex(auth)
        (prof / "mls_template_envelope.bin").write_bytes(bytes.fromhex(env))
        (prof / "mls_template_raw.bin").write_bytes(bytes.fromhex(raw))
        info.update(
            {
                "epoch_id": int(epoch),
                "era_id": int(era),
                "epoch_authenticator_b64": base64.b64encode(auth_b).decode(),
                "template_message_id": mid,
                "template_envelope_len": len(bytes.fromhex(env)),
                "template_raw_len": len(bytes.fromhex(raw)),
            }
        )
    (prof / "mls_group.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=2))

    if push:
        push_to_pi(profile, prof)
    return 0


def push_to_pi(profile: str, prof: Path) -> None:
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(PI_HOST, username=PI_USER, key_filename=PI_KEY, timeout=10)
    remote = f"/home/{PI_USER}/rcsclient/work/{profile}"
    cli.exec_command(f"mkdir -p {remote}")[1].channel.recv_exit_status()
    sftp = cli.open_sftp()
    for name in ("mls_snapshot.bin", "mls_group.json", "mls_template_envelope.bin", "mls_template_raw.bin"):
        p = prof / name
        if p.exists():
            tmp = f"{remote}/.{name}.tmp"
            with sftp.open(tmp, "w") as f:
                f.write(p.read_bytes())
            try:
                sftp.posix_rename(tmp, f"{remote}/{name}")
            except IOError:
                try:
                    sftp.remove(f"{remote}/{name}")
                except IOError:
                    pass
                sftp.rename(tmp, f"{remote}/{name}")
    sftp.close()
    cli.close()
    print(f"pushed MLS state to {PI_HOST}:{remote}")


def check(profile: str) -> int:
    prof = WORK / profile
    try:
        old = json.loads((prof / "mls_group.json").read_text(encoding="utf-8"))
    except OSError:
        print("no exported state; run export first")
        return 1
    ensure_root()
    sender = old.get("identity", "")
    row = sql(f"select group_id||'|'||hex(epoch_data) from mls_group_states where identity_id='{sender}'")
    if not row or "|" not in row:
        print("no current state")
        return 2
    group_id, hexdata = row.split("|", 1)
    cur = hashlib.sha256(bytes.fromhex(hexdata)).hexdigest()
    if group_id != old.get("group_id") or cur != old.get("snapshot_sha"):
        print("CHANGED: phone MLS state advanced; re-export before sending")
        return 2
    print("OK: snapshot is current")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["export", "check"])
    ap.add_argument("--profile", default="default")
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()
    return export(a.profile, a.push) if a.cmd == "export" else check(a.profile)


if __name__ == "__main__":
    sys.exit(main())
