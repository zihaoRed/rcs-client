#!/usr/bin/env python3
"""ci_lifetime_probe.py — measure how long a pinned client_info/token stays valid.

Sends a short plaintext RCS message from the Pi with a fixed client_info and
fixed token at a fixed interval, records grpc-status, and retries with the
current (possibly refreshed) token when the pinned one fails.

Example:
  python pc/ci_lifetime_probe.py --profile default \
      --ci work/default/lifetime/ci_t0.bin \
      --headers work/default/lifetime/headers_t0.txt \
      --ci-captured-at 2026-11-16T19:38:00 \
      --token-captured-at 2026-11-16T18:36:08 \
      --to +1XXXXXXXXXX --interval-min 30 --duration-hours 6 \
      --initial-delay-min 25 --label t0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tachyon_client as tc  # noqa: E402

WORK = ROOT / "work"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def age_min(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return round((datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 60, 1)
    except Exception:
        return None


def with_client_info(template: bytes, ci: bytes) -> bytes:
    return tc.enc([
        (fn, wt, ci if (fn == 3 and wt == 2) else v)
        for fn, wt, v in tc.parse(template)
    ])


def send_once(template: bytes, ci: bytes, headers: str, to: str, text: str) -> tuple[str, str]:
    tpl_ci = with_client_info(template, ci)
    new_id, frame = tc.modified_send(tpl_ci, text, tc.normalize_number(to))
    pi = tc.Pi()
    try:
        pi.put("/tmp/lt_headers.txt", headers.encode())
        pi.put("/tmp/lt_frame.bin", frame)
        out = pi.run(
            "curl --http2 -sS -D - -o /tmp/lt_resp.bin -X POST "
            "-H @/tmp/lt_headers.txt --data-binary @/tmp/lt_frame.bin "
            f"{tc.URL_BASE}/SendMessage; echo curl_rc=$?",
            timeout=120,
        )
    finally:
        pi.close()
    status = ""
    for line in out.splitlines():
        if line.lower().startswith("grpc-status:"):
            status = line.split(":", 1)[1].strip()
    return status, new_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    ap.add_argument("--ci", required=True)
    ap.add_argument("--headers", required=True)
    ap.add_argument("--fresh-headers", default=None)
    ap.add_argument("--ci-captured-at", default=None)
    ap.add_argument("--token-captured-at", default=None)
    ap.add_argument("--to", required=True)
    ap.add_argument("--interval-min", type=float, default=30)
    ap.add_argument("--duration-hours", type=float, default=6)
    ap.add_argument("--initial-delay-min", type=float, default=0)
    ap.add_argument("--label", default="ci")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    prof = WORK / a.profile
    template = (prof / "send_template.pb").read_bytes()
    ci = Path(a.ci).read_bytes()
    headers = Path(a.headers).read_text(encoding="utf-8")
    fresh_headers_path = Path(a.fresh_headers) if a.fresh_headers else prof / "send_headers.txt"
    out_path = Path(a.out) if a.out else prof / "lifetime" / "probes.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    if a.initial_delay_min > 0:
        time.sleep(a.initial_delay_min * 60)

    failures = 0
    n = 0
    while True:
        elapsed = round((time.time() - start) / 60, 1)
        text = f"lt {a.label} {int(elapsed)}m"
        status, mid = send_once(template, ci, headers, a.to, text)
        retry = None
        if status == "16":
            failures += 1
            fresh = fresh_headers_path.read_text(encoding="utf-8")
            if fresh != headers:
                retry, _ = send_once(template, ci, fresh, a.to, text + " retry")
            else:
                retry = "same-token"
        else:
            failures = 0
        rec = {
            "ts": now(),
            "label": a.label,
            "elapsed_min": elapsed,
            "ci_age_min": age_min(a.ci_captured_at),
            "token_age_min": age_min(a.token_captured_at),
            "status": status,
            "retry_status": retry,
            "msg_id": mid,
            "text": text,
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        n += 1
        if failures >= 3:
            print("stopping: 3 consecutive auth failures", flush=True)
            break
        if time.time() - start >= a.duration_hours * 3600:
            print("stopping: duration reached", flush=True)
            break
        time.sleep(a.interval_min * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
