#!/usr/bin/env python3
"""pi_e2ee_send.py — build and send a GSMA MLS (E2EE) RCS message from the Pi.

Prerequisites in work/<profile>/ (from mls_state.py export):
  mls_snapshot.bin, mls_group.json, mls_template_envelope.bin, send_headers.txt
and mls_probe built on the Pi at /home/pi/mls_probe/target/release/mls_probe.

The phone must be the single writer: run `mls_state.py check` first and stop
the phone from sending while this message is in flight.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tachyon_client as tc  # noqa: E402

WORK = ROOT / "work"
URL_BASE = tc.URL_BASE
PI_MLS_PROBE = os.environ.get("MLS_PROBE", "/home/pi/mls_probe/target/release/mls_probe")


def new_message_id() -> str:
    # phone ids look like "Mx" + base64url(16 bytes) = 24 chars
    return "Mx" + base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()


def build_envelope(template: bytes, group: dict, new_id: str, ciphertext: bytes) -> bytes:
    gid = group["group_id"].encode()
    epoch = int(group["epoch_id"]).to_bytes(8, "big")
    era = int(group.get("era_id", 1))
    out = bytearray()
    out += b"\x00\x01\x00\x02"
    out += bytes([len(gid)]) + gid
    out += epoch
    out += bytes([era])
    out += b"\x20\x00\x01"
    out += bytes([len(new_id)]) + new_id.encode()
    out += b"\x00\x00\x00\x01\x00\x1c"  # cipher suite / version trailer
    out += ciphertext
    return bytes(out)


def rewrite_wrapper(wrap: bytes, mls_msg: bytes, group: dict) -> bytes:
    auth = group["epoch_authenticator_b64"]
    era = str(group.get("era_id", 1))
    fields = []
    for fn, wt, v in tc.parse(wrap):
        if fn == 1 and wt == 2:
            inner = []
            for f2, w2, x2 in tc.parse(v):
                if f2 == 1 and w2 == 2:
                    inner.append((1, 2, b"message/mls"))
                elif f2 == 2 and w2 == 2:
                    inner.append((2, 2, mls_msg))
                else:
                    inner.append((f2, w2, x2))
            fields.append((1, 2, tc.enc(inner)))
        elif fn == 2 and wt == 2:
            meta = []
            for f2, w2, x2 in tc.parse(v):
                if f2 == 2 and w2 == 2:
                    name = x2.decode("latin1")
                    if name == "Epoch-Authenticator":
                        meta.append((3, 2, auth.encode()))
                        continue
                    if name == "Era-ID":
                        meta.append((3, 2, era.encode()))
                        continue
                meta.append((f2, w2, x2))
            fields.append((2, 2, tc.enc(meta)))
        else:
            fields.append((fn, wt, v))
    return tc.enc(fields)


def build_message(template_pb: bytes, new_id: str, envelope: bytes, group: dict, to: str | None) -> bytes:
    top = []
    for fn, wt, v in tc.parse(template_pb):
        if fn == 1 and wt == 2 and to:
            top.append((1, 2, tc._rewrite_address(v, to)))
        elif fn == 2 and wt == 2:
            msg = []
            for f2, w2, x2 in tc.parse(v):
                if f2 == 1 and w2 == 2:
                    msg.append((1, 2, new_id.encode()))
                elif f2 == 9 and w2 == 2 and to:
                    msg.append((9, 2, tc._rewrite_address(x2, to)))
                elif f2 == 12 and w2 == 2:
                    msg.append((12, 2, rewrite_wrapper(x2, envelope, group)))
                else:
                    msg.append((f2, w2, x2))
            top.append((2, 2, tc.enc(msg)))
        else:
            top.append((fn, wt, v))
    return tc.enc(top)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    ap.add_argument("--text", required=True)
    ap.add_argument("--to", default=None, help="recipient number (default: template recipient)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip mls_state check")
    ap.add_argument("--skip-encrypt", action="store_true", help="build frames with dummy ciphertext (no mls_probe)")
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()

    prof = WORK / a.profile
    group = json.loads((prof / "mls_group.json").read_text(encoding="utf-8"))
    snap = (prof / "mls_snapshot.bin").read_bytes()
    template_pb = (prof / "mls_template_message.pb").read_bytes() if (prof / "mls_template_message.pb").exists() else None
    if template_pb is None:
        print("missing mls_template_message.pb (captured E2EE SendMessage protobuf)")
        return 1
    headers = (prof / "send_headers.txt").read_text(encoding="utf-8")

    if not a.force:
        rc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "mls_state.py"), "check", "--profile", a.profile]
        ).returncode
        if rc != 0:
            print("aborting: phone MLS state advanced or state missing")
            return 1

    # 1. RCS CPIM payload (matches phone raw_cpim_content format)
    payload = b"\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n" + a.text.encode()

    new_id = new_message_id()
    print(f"profile : {a.profile}")
    print(f"identity: {group['identity']}")
    print(f"group   : {group['group_id']} epoch={group['epoch_id']} era={group['era_id']}")
    print(f"msg id  : {new_id}")
    print(f"text    : {a.text}")

    # AAD binds the RCS message id into the MLS message (observed format)
    aad = bytes([0, 1, 0x18]) + new_id.encode() + bytes([0, 0, 0, 1, 0])

    # 2. encrypt on the Pi with mls_probe
    if a.skip_encrypt:
        ciphertext = bytes(190)
        snap_out = snap
        print("skip-encrypt: using dummy ciphertext")
    else:
        pi = tc.Pi()
        try:
            pi.put("/tmp/pi_mls_snapshot.bin", snap)
            pi.put("/tmp/pi_mls_payload.bin", payload)
            pi.put("/tmp/pi_mls_aad.bin", aad)
            out = pi.run(
                f"{PI_MLS_PROBE} /tmp/pi_mls_snapshot.bin '{group['group_id']}' encrypt-aad "
                "/tmp/pi_mls_payload.bin /tmp/pi_mls_aad.bin /tmp/pi_mls_out.bin /tmp/pi_mls_snap_out.bin; echo rc=$?; "
                "base64 -w0 /tmp/pi_mls_out.bin; echo; base64 -w0 /tmp/pi_mls_snap_out.bin",
                timeout=120,
            )
        finally:
            pi.close()
        lines = out.splitlines()
        rc_idx = next((i for i, l in enumerate(lines) if l.startswith("rc=")), -1)
        rc_line = lines[rc_idx] if rc_idx >= 0 else ""
        for l in lines[:rc_idx if rc_idx >= 0 else len(lines)]:
            if l.strip():
                print(f"[mls_probe] {l}")
        print(rc_line)
        if not rc_line.endswith("0"):
            print(out[:800])
            return 1
        rest = [l for l in lines[rc_idx + 1:] if l.strip()]
        try:
            mls_out_b64, snap_out_b64 = rest[0], rest[1]
            ciphertext = base64.b64decode(mls_out_b64)
            snap_out = base64.b64decode(snap_out_b64)
        except Exception as e:  # noqa: BLE001
            print("cannot read mls_probe output:", e)
            print(out[:800])
            return 1
        print(f"mls ciphertext: {len(ciphertext)} bytes; updated snapshot: {len(snap_out)} bytes")

    # 3. build SendMessage protobuf (mls_probe output is already the full MLS message)
    to = tc.normalize_number(a.to) if a.to else None
    pb = build_message(template_pb, new_id, ciphertext, group, to)
    frame = tc.grpc_frame(pb)
    print(f"mls message: {len(ciphertext)} bytes; SendMessage pb: {len(pb)} bytes; frame: {len(frame)} bytes")

    if a.dry_run:
        (prof / "pi_e2ee_dryrun_frame.bin").write_bytes(frame)
        (prof / "pi_e2ee_dryrun_mls.bin").write_bytes(ciphertext)
        print("dry run: wrote pi_e2ee_dryrun_frame.bin / pi_e2ee_dryrun_envelope.bin")
        return 0

    # 4. send from the Pi
    pi = tc.Pi()
    try:
        pi.put("/tmp/pi_e2ee_headers.txt", headers.encode())
        pi.put("/tmp/pi_e2ee_frame.bin", frame)
        out = pi.run(
            "curl --http2 -sS -D - -o /tmp/pi_e2ee_resp.bin -X POST "
            "-H @/tmp/pi_e2ee_headers.txt --data-binary @/tmp/pi_e2ee_frame.bin "
            f"{URL_BASE}/SendMessage; echo curl_rc=$?",
            timeout=a.timeout,
        )
    finally:
        pi.close()
    for line in out.splitlines():
        if line.lower().startswith(("http/", "grpc-status", "grpc-message", "curl_rc")):
            print(line)

    # 5. persist the advanced snapshot (Pi is now the single writer)
    (prof / "mls_snapshot.bin").write_bytes(snap_out)
    group["snapshot_sha"] = __import__("hashlib").sha256(snap_out).hexdigest()
    (prof / "mls_group.json").write_text(json.dumps(group, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved updated snapshot; keep the phone from sending until the next sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
