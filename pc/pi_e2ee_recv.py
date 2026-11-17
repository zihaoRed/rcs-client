#!/usr/bin/env python3
"""pi_e2ee_recv.py — receive-only E2EE reader for the Pi.

Tails the harvester's token_responses.jsonl, extracts inbound Tachyon messages,
decrypts message/mls frames with mls_probe on the Pi using the profile snapshot,
and updates the snapshot after each successful decryption.

The phone remains the receiver; the Pi only mirrors the stream and never sends.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tachyon_client as tc  # noqa: E402

WORK = ROOT / "work"
PROBE = os.environ.get("MLS_PROBE", "/home/pi/mls_probe/target/release/mls_probe")
RESPONSES = WORK / "native" / "token_responses.jsonl"


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def parse_frames(buf: bytes) -> list[bytes]:
    out = []
    i = 0
    while i + 5 <= len(buf):
        comp = buf[i]
        ln = int.from_bytes(buf[i + 1:i + 5], "big")
        if ln <= 0 or i + 5 + ln > len(buf):
            break
        payload = buf[i + 5:i + 5 + ln]
        i += 5 + ln
        if comp == 1:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                continue
        out.append(payload)
    return out


def addr_number(b: bytes) -> str:
    try:
        for fn, wt, v in tc.parse(b):
            if fn == 2 and wt == 2:
                return v.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def extract_text(data: bytes) -> str:
    idx = data.find(b"\r\n\r\n")
    if idx >= 0:
        return data[idx + 4:].decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


class Reader:
    def __init__(self, profile: str, snapshot_override: str | None, no_update: bool,
                 since_ts: int, limit: int, from_start: bool):
        self.prof = WORK / profile
        self.group = json.loads((self.prof / "mls_group.json").read_text(encoding="utf-8"))
        self.gid = self.group["group_id"]
        self.snap_path = Path(snapshot_override) if snapshot_override else self.prof / "mls_snapshot.bin"
        self.snapshot = self.snap_path.read_bytes()
        self.no_update = no_update
        self.since_ts = since_ts
        self.limit = limit
        self.done = 0
        self.state_path = self.prof / "recv_state.json"
        self.state = {"offset": 0, "processed": []}
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        elif not from_start and RESPONSES.exists():
            self.state["offset"] = RESPONSES.stat().st_size
        self.pi = tc.Pi()

    def close(self) -> None:
        self.pi.close()
        self.save_state()

    def save_state(self) -> None:
        self.state["processed"] = self.state["processed"][-500:]
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def read_new(self) -> list[dict]:
        if not RESPONSES.exists():
            return []
        size = RESPONSES.stat().st_size
        if self.state["offset"] > size:
            self.state["offset"] = 0
        out = []
        with RESPONSES.open("rb") as f:
            f.seek(self.state["offset"])
            data = f.read()
            self.state["offset"] = f.tell()
        for line in data.splitlines():
            line = line.strip()
            if not line.startswith(b"{"):
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def decrypt(self, env: bytes) -> tuple[bytes | None, bytes | None]:
        self.pi.put("/tmp/pi_recv_snap.bin", self.snapshot)
        self.pi.put("/tmp/pi_recv_env.bin", env)
        out = self.pi.run(
            f"{PROBE} /tmp/pi_recv_snap.bin '{self.gid}' /tmp/pi_recv_env.bin "
            "/tmp/pi_recv_snap_out.bin /tmp/pi_recv_app.bin; echo rc=$?; "
            "base64 -w0 /tmp/pi_recv_app.bin 2>/dev/null; echo; "
            "base64 -w0 /tmp/pi_recv_snap_out.bin",
            timeout=60,
        )
        lines = out.splitlines()
        rc_idx = next((i for i, l in enumerate(lines) if l.startswith("rc=")), -1)
        if rc_idx < 0 or not lines[rc_idx].endswith("0"):
            log(f"decrypt failed: {out[:300]}")
            return None, None
        rest = [l for l in lines[rc_idx + 1:] if l.strip()]
        app = base64.b64decode(rest[0]) if rest else b""
        snap = base64.b64decode(rest[1]) if len(rest) > 1 else None
        return app, snap

    def handle_pb(self, pb: bytes, ts: int) -> None:
        if self.limit and self.done >= self.limit:
            return
        try:
            top = tc.parse(pb)
        except Exception:
            return
        msg = next((v for fn, wt, v in top if fn == 2 and wt == 2), None)
        if msg is None:
            return
        mid = ""; sender = ""; wrap = None
        for fn, wt, v in tc.parse(msg):
            if fn == 1 and wt == 2:
                mid = v.decode("latin1")
            elif fn == 8 and wt == 2:
                sender = addr_number(v)
            elif fn == 12 and wt == 2:
                wrap = v
        if wrap is None:
            return
        ctype = ""; content = None
        for fn, wt, v in tc.parse(wrap):
            if fn == 1 and wt == 2:
                for f2, w2, x2 in tc.parse(v):
                    if f2 == 1 and w2 == 2:
                        ctype = x2.decode("latin1", "replace")
                    elif f2 == 2 and w2 == 2:
                        content = x2
        if content is None:
            return
        if mid and mid in self.state["processed"]:
            return
        if "message/mls" in ctype:
            log(f"E2EE from {sender or '?'} id={mid} ({len(content)} bytes)")
            app, snap = self.decrypt(content)
            if app is None:
                return
            text = extract_text(app)
            log(f"  decrypted: {text[:500]!r}")
            if snap and not self.no_update:
                tmp = self.snap_path.with_suffix(".bin.tmp")
                tmp.write_bytes(snap)
                os.replace(tmp, self.snap_path)
                self.snapshot = snap
                log(f"  snapshot updated: {len(snap)} bytes")
            self.done += 1
            if mid:
                self.state["processed"].append(mid)
        elif "text/plain" in ctype or ctype.startswith("application/"):
            text = extract_text(content)
            log(f"plaintext from {sender or '?'} id={mid} type={ctype}: {text[:500]!r}")
            self.done += 1
            if mid:
                self.state["processed"].append(mid)

    def process_events(self, events: list[dict]) -> None:
        for ev in events:
            if self.limit and self.done >= self.limit:
                return
            if ev.get("k") != "read" or "ReceiveMessages" not in (ev.get("m") or ""):
                continue
            if (ev.get("len") or 0) <= 7:
                continue
            ts = int(ev.get("ts") or 0)
            if self.since_ts and ts <= self.since_ts:
                continue
            try:
                buf = bytes.fromhex(ev.get("hex", ""))
            except ValueError:
                continue
            for pb in parse_frames(buf):
                self.handle_pb(pb, ts)

    def run(self, follow: bool) -> None:
        log(f"reader profile={self.prof.name} snapshot={len(self.snapshot)} bytes group={self.gid}")
        while True:
            events = self.read_new()
            if events:
                self.process_events(events)
                self.save_state()
            if not follow or (self.limit and self.done >= self.limit):
                break
            time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--from-start", action="store_true")
    ap.add_argument("--since-ts", type=int, default=0)
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--no-update", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    r = Reader(a.profile, a.snapshot, a.no_update, a.since_ts, a.limit, a.from_start)
    try:
        r.run(a.follow)
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
