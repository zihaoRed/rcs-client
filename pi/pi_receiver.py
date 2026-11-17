#!/usr/bin/env python3
"""pi_receiver.py — standalone E2EE receiver for the Pi.

Runs on the Raspberry Pi. Reloads send_headers.txt / client_info.bin on every
reconnect, opens ReceiveMessages, reconnects when the stream is cancelled,
dedupes by RCS message id, decrypts message/mls with mls_probe, updates the
MLS snapshot, and appends decrypted text to received.jsonl.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

URL = (
    "https://instantmessaging-pa-jms-us.googleapis.com/google.internal."
    "communications.instantmessaging.v1.Messaging/ReceiveMessages"
)
PROBE = os.environ.get("MLS_PROBE", "/home/pi/mls_probe/target/release/mls_probe")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rv(b: bytes, i: int):
    r = 0
    s = 0
    while i < len(b):
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7
    raise ValueError("varint")


def wv(v: int) -> bytes:
    out = b""
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out += bytes([x | 0x80])
        else:
            return out + bytes([x])


def parse(b: bytes):
    i = 0
    out = []
    while i < len(b):
        key, i = rv(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rv(b, i)
            out.append((fn, wt, v))
        elif wt == 2:
            ln, i = rv(b, i)
            out.append((fn, wt, b[i:i + ln]))
            i += ln
        elif wt == 1:
            out.append((fn, wt, b[i:i + 8]))
            i += 8
        elif wt == 5:
            out.append((fn, wt, b[i:i + 4]))
            i += 4
        else:
            raise ValueError(f"wire type {wt}")
    return out


def grpc_frame(ci: bytes) -> bytes:
    payload = b"\x0a" + wv(len(ci)) + ci
    comp = gzip.compress(payload, mtime=0)
    return bytes([1]) + len(comp).to_bytes(4, "big") + comp


def extract_text(data: bytes) -> str:
    i = data.find(b"\r\n\r\n")
    if i >= 0:
        return data[i + 4:].decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


def extract_commit(content: bytes) -> bytes | None:
    """Return the MLS PublicMessage bytes of a commit, or None.

    RCS commit frames carry a nested protobuf field 1 whose value is
    b"MB" + MLS PublicMessage.
    """
    if content.startswith(b"MB\x00\x01\x00"):
        return content[2:]
    try:
        for fn, wt, v in parse(content):
            if fn == 1 and wt == 2 and v.startswith(b"MB\x00\x01\x00"):
                return v[2:]
    except Exception:
        pass
    return None


class Receiver:
    def __init__(self, work: Path, output: Path, sleep_s: float = 1.0,
                 recv_headers: str | None = None, recv_frame: str | None = None,
                 max_time: int = 60):
        self.work = work
        self.output = output
        self.sleep_s = sleep_s
        self.recv_headers = Path(recv_headers) if recv_headers else None
        self.recv_frame = Path(recv_frame) if recv_frame else None
        self.max_time = max_time
        self.state_path = work / "receiver_state.json"
        self.snap_path = work / "mls_snapshot.bin"
        self.state = {"processed": [], "last_error": "", "last_connect": ""}
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.processed = set(self.state.get("processed", []))

    def log(self, msg: str) -> None:
        print(f"[{now()}] {msg}", flush=True)

    def save_state(self) -> None:
        self.state["processed"] = list(self.processed)[-500:]
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def load_group(self) -> dict:
        return json.loads((self.work / "mls_group.json").read_text(encoding="utf-8"))

    def load_inputs(self):
        rh = self.recv_headers or (self.work / "recv_phone_headers.txt")
        rf = self.recv_frame or (self.work / "recv_phone_frame.bin")
        if rh.exists() and rf.exists():
            return rh.read_text(encoding="utf-8"), rf.read_bytes(), "phone-recv"
        headers = (self.work / "send_headers.txt").read_text(encoding="utf-8")
        ci = (self.work / "client_info.bin").read_bytes()
        return headers, grpc_frame(ci), "send-fallback"

    def connect(self, headers: str, frame: bytes) -> bytes:
        hpath = self.work / ".recv_headers.txt"
        fpath = self.work / ".recv_frame.bin"
        rpath = self.work / ".recv_resp.bin"
        hdpath = self.work / ".recv_resp.h"
        hpath.write_text(headers, encoding="utf-8")
        fpath.write_bytes(frame)
        cmd = [
            "curl", "--http2", "--no-buffer", "-sS",
            "-H", f"@{hpath}", "--data-binary", f"@{fpath}",
            URL, "-o", str(rpath), "-D", str(hdpath), "--max-time", str(self.max_time),
        ]
        subprocess.run(cmd, capture_output=True, timeout=max(35, self.max_time + 15))
        data = rpath.read_bytes() if rpath.exists() else b""
        status = ""
        try:
            for line in hdpath.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("grpc-status"):
                    status = line.strip()
        except OSError:
            pass
        return data, status

    def decrypt(self, env: bytes, group: dict, commit: bool = False) -> tuple[str | None, bytes | None]:
        ep = self.work / ".recv_env.bin"
        op = self.work / ".recv_snap_out.bin"
        ap = self.work / ".recv_app.bin"
        ep.write_bytes(env)
        for p in (op, ap):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            r = subprocess.run(
                [PROBE, str(self.snap_path), group["group_id"], str(ep), str(op), str(ap)],
                capture_output=True, timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            self.log(f"mls_probe error: {e}")
            return None, None
        snap = op.read_bytes() if op.exists() else None
        if r.returncode != 0:
            self.log(f"mls_probe failed: {r.stdout.decode('utf-8', 'replace')[:200]}")
            return None, snap
        if commit or not ap.exists() or ap.stat().st_size == 0:
            return None, snap
        text = extract_text(ap.read_bytes())
        return text, snap

    def update_snapshot(self, snap: bytes | None) -> None:
        if not snap:
            return
        tmp = self.snap_path.with_suffix(".bin.tmp")
        tmp.write_bytes(snap)
        os.replace(tmp, self.snap_path)
        self.log(f"snapshot updated: {len(snap)} bytes")

    def handle(self, data: bytes, group: dict) -> int:
        i = 0
        got = 0
        while i + 5 <= len(data):
            comp = data[i]
            ln = int.from_bytes(data[i + 1:i + 5], "big")
            if ln <= 0 or i + 5 + ln > len(data):
                break
            payload = data[i + 5:i + 5 + ln]
            i += 5 + ln
            if comp == 1:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    continue
            try:
                top = parse(payload)
            except Exception:
                continue
            msg = next((v for fn, wt, v in top if fn == 2 and wt == 2), None)
            if msg is None:
                continue
            mid = ""; sender = ""; ctype = ""; content = None
            try:
                for fn, wt, v in parse(msg):
                    if fn == 1 and wt == 2:
                        mid = v.decode("latin1")
                    elif fn == 8 and wt == 2:
                        for a, b, c in parse(v):
                            if a == 2 and b == 2:
                                sender = c.decode("utf-8", "replace")
                    elif fn == 12 and wt == 2:
                        for a, b, c in parse(v):
                            if a == 1 and b == 2:
                                for d, e, f in parse(c):
                                    if d == 1 and e == 2:
                                        ctype = f.decode("latin1", "replace")
                                    elif d == 2 and e == 2:
                                        content = f
            except Exception:
                continue
            if content is None:
                continue
            if mid and mid in self.processed:
                continue
            commit = extract_commit(content)
            if commit is not None:
                text, snap = self.decrypt(commit, group, commit=True)
                self.update_snapshot(snap)
                self.log(
                    f"MLS commit processed id={mid} snapshot="
                    f"{len(snap) if snap else 0}"
                )
                if mid:
                    self.processed.add(mid)
                continue
            if "message/mls" in ctype:
                text, snap = self.decrypt(content, group)
                self.update_snapshot(snap)
                if text is None:
                    self.log(f"MLS commit/control processed id={mid}")
                    if mid:
                        self.processed.add(mid)
                    continue
                self.emit(mid, sender, ctype, text)
                got += 1
            else:
                text = extract_text(content)
                self.emit(mid, sender, ctype, text)
                got += 1
        return got

    def emit(self, mid: str, sender: str, ctype: str, text: str) -> None:
        if mid:
            self.processed.add(mid)
        rec = {"ts": now(), "id": mid, "from": sender, "type": ctype, "text": text}
        with self.output.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.log(f"message id={mid} from={sender} type={ctype} text={text[:300]!r}")

    def run(self) -> None:
        self.log(f"receiver work={self.work}")
        last_headers_mtime = 0.0
        last_mode = ""
        while True:
            try:
                headers, frame, mode = self.load_inputs()
                group = self.load_group()
                hpath = self.recv_headers or (
                    self.work / "recv_phone_headers.txt"
                    if (self.work / "recv_phone_headers.txt").exists()
                    else self.work / "send_headers.txt"
                )
                mtime = hpath.stat().st_mtime
                if mtime != last_headers_mtime or mode != last_mode:
                    last_headers_mtime = mtime
                    last_mode = mode
                    self.log(f"reloaded credentials mode={mode} frame={len(frame)} bytes")
                data, status = self.connect(headers, frame)
                if status:
                    self.log(f"stream end {status}")
                self.handle(data, group)
                self.state["last_connect"] = now()
                if "grpc-status: 16" in status:
                    self.state["last_error"] = "AUTH_EXPIRED"
                    self.log("AUTH_EXPIRED: waiting for fresh token/client_info")
                    self.save_state()
                    time.sleep(30)
                    continue
                self.state["last_error"] = ""
                self.save_state()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                self.log(f"loop error: {e}")
                time.sleep(5)
            time.sleep(self.sleep_s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/home/pi/rcsclient/work/default")
    ap.add_argument("--output", default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--recv-headers", default=None)
    ap.add_argument("--recv-frame", default=None)
    ap.add_argument("--max-time", type=int, default=60)
    a = ap.parse_args()
    work = Path(a.work)
    output = Path(a.output) if a.output else work / "received.jsonl"
    Receiver(work, output, a.sleep, a.recv_headers, a.recv_frame, a.max_time).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
