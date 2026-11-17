#!/usr/bin/env python3
"""pi_plain_reply.py — send an unencrypted (text/plain) RCS reply from the Pi.

Uses work/<profile>/send_template.pb, send_headers.txt and client_info.bin
(the latter captured automatically by token_harvester.py from SendMessage).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tachyon_client as tc  # noqa: E402

WORK = ROOT / "work"


def with_client_info(template: bytes, ci: bytes) -> bytes:
    return tc.enc([
        (fn, wt, ci if (fn == 3 and wt == 2) else v)
        for fn, wt, v in tc.parse(template)
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    ap.add_argument("--text", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--client-info", default=None, help="override client_info.bin path")
    ap.add_argument("--headers", default=None, help="override send_headers.txt path")
    a = ap.parse_args()

    prof = WORK / a.profile
    tpl = (prof / "send_template.pb").read_bytes()
    ci_path = Path(a.client_info) if a.client_info else prof / "client_info.bin"
    if not ci_path.exists():
        print(f"missing {ci_path}; wait for the harvester to capture a phone SendMessage")
        return 1
    ci = ci_path.read_bytes()
    hpath = Path(a.headers) if a.headers else prof / "send_headers.txt"
    headers = hpath.read_text(encoding="utf-8")

    tpl_ci = with_client_info(tpl, ci)
    new_id, frame = tc.modified_send(tpl_ci, a.text, tc.normalize_number(a.to))
    print(f"profile : {a.profile}")
    print(f"to      : {tc.normalize_number(a.to)}")
    print(f"msg id  : {new_id}")
    print(f"text    : {a.text}")
    print(f"client info: {len(ci)} bytes")
    print(f"frame   : {len(frame)} bytes")

    if a.dry_run:
        (prof / "pi_plain_reply_dryrun.bin").write_bytes(frame)
        print("dry run: wrote pi_plain_reply_dryrun.bin")
        return 0

    pi = tc.Pi()
    try:
        pi.put("/tmp/pi_plain_headers.txt", headers.encode())
        pi.put("/tmp/pi_plain_frame.bin", frame)
        out = pi.run(
            "curl --http2 -sS -D - -o /tmp/pi_plain_resp.bin -X POST "
            "-H @/tmp/pi_plain_headers.txt --data-binary @/tmp/pi_plain_frame.bin "
            f"{tc.URL_BASE}/SendMessage; echo curl_rc=$?",
            timeout=120,
        )
    finally:
        pi.close()
    for line in out.splitlines():
        if line.lower().startswith(("http/", "grpc-status", "grpc-message", "curl_rc")):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
