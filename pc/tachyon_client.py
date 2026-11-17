#!/usr/bin/env python3
"""tachyon_client.py — Linux-side Tachyon RCS send/receive toolkit.

The phone only provides a fresh registration identity (headers + client info).
The Raspberry Pi then talks to Google Tachyon directly.

Workflow
--------
1. On the phone, capture a SendMessage start+frame with Frida
   (agent/tachyon_start_write.js) while sending a test message.
2. Capture the active ReceiveMessages headers with agent/extract_headers.js.
3. `python tachyon_client.py prepare --send-capture ... --recv-headers ...`
4. `python tachyon_client.py send --text "hello"`
5. `python tachyon_client.py recv`  (starts the persistent receive loop)
6. `python tachyon_client.py status`

Credentials stay in the work dir / Pi /tmp and are removed after use.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import secrets
import struct
import sys
import time
from pathlib import Path

import paramiko

URL_BASE = (
    "https://instantmessaging-pa-jms-us.googleapis.com/google.internal."
    "communications.instantmessaging.v1.Messaging"
)
DEFAULT_WORK = str(Path(__file__).resolve().parent / "work")
PI_HOST = os.environ.get("PI_HOST", "")
PI_USER = os.environ.get("PI_USER", "pi")
PI_KEY = os.environ.get("PI_KEY", os.path.expanduser("~/.ssh/id_ed25519"))

KEEP_HEADERS = {
    "content-type", "te", "user-agent", "x-goog-api-key",
    "sec-x-google-grpc", "origin", "x-goog-ext-202964622-bin",
    "grpc-encoding", "grpc-accept-encoding", "grpc-timeout",
}


# -- protobuf helpers -----------------------------------------------------
def rv(b, i):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def wv(v):
    out = b""
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out += bytes([x | 0x80])
        else:
            return out + bytes([x])


def parse(b):
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


def enc(fields):
    out = b""
    for fn, wt, v in fields:
        out += wv((fn << 3) | wt)
        if wt == 0:
            out += wv(v)
        elif wt == 2:
            out += wv(len(v)) + v
        else:
            out += v
    return out


def grpc_frame(pb: bytes) -> bytes:
    comp = gzip.compress(pb, mtime=0)
    return bytes([1]) + struct.pack(">I", len(comp)) + comp


def normalize_number(num: str) -> str:
    """Normalize a dialable number to E.164-ish form (+countrycode...)."""
    s = num.strip()
    plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        raise SystemExit(f"invalid number: {num!r}")
    if plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


def _rewrite_address(value: bytes, number: str) -> bytes:
    """Replace the number (field 2) inside an Address message."""
    fields = []
    for fn, wt, v in parse(value):
        if fn == 2 and wt == 2:
            fields.append((fn, wt, number.encode()))
        else:
            fields.append((fn, wt, v))
    return enc(fields)


def template_info(template_pb: bytes) -> tuple[str, str, str]:
    """Return (recipient, sender, client_id) from a SendMessage template."""
    recipient = sender = client_id = ""
    for fn, wt, v in parse(template_pb):
        if fn == 1 and wt == 2:
            for f2, w2, x2 in parse(v):
                if f2 == 2 and w2 == 2:
                    recipient = x2.decode("utf-8", "replace")
        elif fn == 2 and wt == 2:
            for f2, w2, x2 in parse(v):
                if f2 == 8 and w2 == 2:
                    for f3, w3, x3 in parse(x2):
                        if f3 == 2 and w3 == 2:
                            sender = x3.decode("utf-8", "replace")
        elif fn == 3 and wt == 2:
            for f2, w2, x2 in parse(v):
                if f2 == 1 and w2 == 2:
                    client_id = x2.decode("utf-8", "replace")
    return recipient, sender, client_id


def modified_send(template_pb: bytes, text: str, to: str | None = None) -> tuple[str, bytes]:
    top = parse(template_pb)
    new_id = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    out = []
    for fn, wt, v in top:
        if fn == 1 and wt == 2 and to:
            out.append((fn, wt, _rewrite_address(v, to)))
        elif fn == 2 and wt == 2:
            msg = []
            for f2, w2, x2 in parse(v):
                if f2 == 1 and w2 == 2:
                    msg.append((f2, w2, new_id.encode()))
                elif f2 == 9 and w2 == 2 and to:
                    msg.append((f2, w2, _rewrite_address(x2, to)))
                elif f2 == 12 and w2 == 2:
                    wrapper = []
                    for f3, w3, y3 in parse(x2):
                        if f3 == 1 and w3 == 2:
                            inner = [
                                (f4, w4, text.encode() if f4 == 2 else y4)
                                for f4, w4, y4 in parse(y3)
                            ]
                            wrapper.append((f3, w3, enc(inner)))
                        else:
                            wrapper.append((f3, w3, y3))
                    msg.append((f2, w2, enc(wrapper)))
                else:
                    msg.append((f2, w2, x2))
            out.append((fn, wt, enc(msg)))
        else:
            out.append((fn, wt, v))
    return new_id, grpc_frame(enc(out))


# -- capture parsing ------------------------------------------------------
def _header_pairs(headers: list[str]) -> list[tuple[str, str]]:
    return list(zip(headers[0::2], headers[1::2]))


def _headers_text(headers: list[str], timeout: str = "900S") -> str:
    lines = []
    for k, v in _header_pairs(headers):
        lk = k.lower()
        if lk == "grpc-timeout":
            v = timeout
        if lk in KEEP_HEADERS:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


def _content_type(raw: bytes) -> str:
    """Content type of a SendMessage pb (message field 12 -> wrapper -> inner)."""
    try:
        for fn, wt, v in parse(raw):
            if fn != 2 or wt != 2:
                continue
            for f2, w2, x2 in parse(v):
                if f2 != 12 or w2 != 2:
                    continue
                for f3, w3, y3 in parse(x2):
                    if f3 != 1 or w3 != 2:
                        continue
                    for f4, w4, z4 in parse(y3):
                        if f4 == 1 and w4 == 2:
                            return z4.decode("utf-8", "replace")
    except (ValueError, IndexError):
        pass
    return ""


def extract_send_pair(capture: Path) -> tuple[bytes, list[str]]:
    """Return (text message template pb, SendMessage start headers)."""
    cur = None
    writes: list[bytes] = []
    found = []

    def process(start, frames):
        if not start or not start.get("url", "").endswith("/SendMessage"):
            return
        i = 0
        while i < len(frames):
            d = frames[i]
            if len(d) == 5 and d[0] in (0, 1):
                ln = int.from_bytes(d[1:5], "big")
                if i + 1 < len(frames) and len(frames[i + 1]) >= ln:
                    body = frames[i + 1][:ln]
                    raw = gzip.decompress(body) if d[0] else body
                    if _content_type(raw).startswith("text/plain"):
                        found.append((raw, start.get("headers", [])))
                    i += 2
                    continue
            i += 1

    for line in capture.open(encoding="utf-8", errors="replace"):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        p = m.get("payload") or {}
        if p.get("k") == "start":
            process(cur, writes)
            cur, writes = p, []
        elif p.get("k") == "write" and cur is not None:
            d = p.get("data") or {}
            if d.get("hex"):
                writes.append(bytes.fromhex(d["hex"]))
    process(cur, writes)

    if not found:
        raise SystemExit("no text/plain SendMessage frame in capture")
    # Prefer a frame whose content is text/plain (not isComposing)
    raw, headers = found[-1]
    return raw, headers


def extract_recv_headers(capture: Path) -> list[str]:
    entry = None
    for line in capture.open(encoding="utf-8", errors="replace"):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        p = m.get("payload") or {}
        if p.get("url", "").endswith("/ReceiveMessages"):
            entry = p
    if entry is None:
        raise SystemExit("no ReceiveMessages entry in extract_headers capture")
    return entry.get("headers", [])


def build_recv_frame(template_pb: bytes) -> bytes:
    ci = [v for fn, wt, v in parse(template_pb) if fn == 3 and wt == 2]
    if not ci:
        raise SystemExit("template has no client info field 3")
    return grpc_frame(b"\x0a" + wv(len(ci[0])) + ci[0])


def validate_profile(work: Path, to: str | None = None, text: str = "check") -> tuple[list[str], list[str], dict]:
    """Local sanity check before touching the network."""
    errors: list[str] = []
    warnings: list[str] = []
    info: dict = {}

    tpl = work / "send_template.pb"
    if not tpl.is_file():
        errors.append(f"missing {tpl}")
    else:
        pb = tpl.read_bytes()
        recipient, sender, client_id = template_info(pb)
        ctype = _content_type(pb)
        info.update(recipient=recipient, sender=sender, client_id=client_id, content_type=ctype)
        if not ctype.startswith("text/plain"):
            errors.append(
                f"send template is not text/plain (got {ctype or 'none'}); "
                "replaying it will likely return grpc-status 3"
            )
        if not recipient:
            errors.append("template has no recipient (top-level field 1)")
        if not sender:
            errors.append("template has no sender address (message field 8)")
        if not client_id:
            warnings.append("template has no client id (client info field 1); header/client pairing may fail")

        target = normalize_number(to) if to else recipient
        info["target"] = target
        if target:
            try:
                _, frame = modified_send(pb, text, target)
                raw = gzip.decompress(frame[5:]) if frame and frame[0] == 1 else frame[5:]
                got_recipient, _, _ = template_info(raw)
                if got_recipient != target:
                    errors.append(f"recipient rewrite failed: {got_recipient} != {target}")
                got_type = _content_type(raw)
                if not got_type.startswith("text/plain"):
                    errors.append(f"modified frame content type is {got_type or 'none'}")
            except Exception as e:  # noqa: BLE001 - report any parse failure
                errors.append(f"cannot build SendMessage frame: {e}")

    hdr = work / "send_headers.txt"
    if not hdr.is_file():
        errors.append(f"missing {hdr}")
    else:
        headers = hdr.read_text(encoding="utf-8")
        low = headers.lower()
        if "x-goog-api-key:" not in low:
            errors.append("send_headers.txt has no x-goog-api-key")
        if "content-type:" not in low or "application/grpc" not in low:
            warnings.append("send_headers.txt is missing content-type: application/grpc")
        if "x-goog-ext-202964622-bin:" not in low:
            warnings.append("send_headers.txt has no x-goog-ext token; server may reject with grpc-status 16")
        age_min = (time.time() - hdr.stat().st_mtime) / 60
        info["headers_age_min"] = round(age_min, 1)
        if age_min > 110:
            warnings.append(
                f"send headers are {age_min:.0f} min old; x-goog-ext expires in ~2h, recapture first"
            )

    recv_ok = (work / "recv_headers.txt").is_file() and (work / "recv_frame.bin").is_file()
    info["recv_ready"] = recv_ok
    if not recv_ok:
        warnings.append("recv_headers.txt / recv_frame.bin missing; ReceiveMessages not ready")

    return errors, warnings, info


# -- Pi transport ---------------------------------------------------------
class Pi:
    def __init__(self):
        if not PI_HOST:
            raise RuntimeError("PI_HOST is not set (see .env.example)")
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(
            PI_HOST, username=PI_USER, key_filename=PI_KEY,
            look_for_keys=False, allow_agent=False, timeout=20,
        )

    def run(self, cmd: str, stdin_data: bytes | None = None, timeout: float = 120) -> str:
        stdin, stdout, stderr = self.c.exec_command(cmd, timeout=timeout)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if err.strip():
            out += "\n[stderr] " + err.strip()[:300]
        return out

    def put(self, remote: str, data: bytes) -> None:
        self.run(f"cat > {remote}", stdin_data=data)

    def close(self) -> None:
        self.c.close()


# -- commands -------------------------------------------------------------
def _validate_profile(name: str | None) -> str | None:
    if name is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise SystemExit("profile must match [A-Za-z0-9_-]{1,32}")
    return name


def _work(args) -> Path:
    if args.work:
        return Path(args.work)
    prof = getattr(args, "profile", None)
    return Path(DEFAULT_WORK) / prof if prof else Path(DEFAULT_WORK)


def _suf(args) -> str:
    prof = getattr(args, "profile", None)
    return f"_{prof}" if prof else ""


def cmd_prepare(args) -> int:
    work = _work(args)
    work.mkdir(parents=True, exist_ok=True)
    template, send_headers = extract_send_pair(Path(args.send_capture))
    (work / "send_template.pb").write_bytes(template)
    (work / "send_headers.txt").write_text(_headers_text(send_headers, "30S"), encoding="utf-8")

    if args.recv_headers:
        recv_headers = extract_recv_headers(Path(args.recv_headers))
    else:
        print("[warn] no --recv-headers; reusing SendMessage headers (may be cancelled for ReceiveMessages)")
        recv_headers = send_headers
    (work / "recv_headers.txt").write_text(_headers_text(recv_headers, "900S"), encoding="utf-8")
    (work / "recv_frame.bin").write_bytes(build_recv_frame(template))

    recipient, sender, client_id = template_info(template)
    meta = {
        "profile": getattr(args, "profile", None),
        "recipient_in_template": recipient,
        "sender": sender,
        "client_id": client_id,
        "send_capture": str(args.send_capture),
        "recv_headers_capture": str(args.recv_headers) if args.recv_headers else None,
    }
    (work / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"prepared in {work}")
    print(f"  template recipient : {recipient}")
    print(f"  template sender    : {sender}")
    print(f"  client id          : {client_id}")
    print("  send_template.pb / send_headers.txt / recv_headers.txt / recv_frame.bin / meta.json")
    return 0


def cmd_check(args) -> int:
    work = _work(args)
    errors, warnings, info = validate_profile(work, args.to, args.text or "check")
    print(f"profile : {getattr(args, 'profile', None) or 'default'}")
    print(f"work    : {work}")
    for key in ("recipient", "sender", "client_id", "content_type", "target", "headers_age_min", "recv_ready"):
        if key in info:
            print(f"{key:15}: {info[key]}")
    for w in warnings:
        print(f"WARN    : {w}")
    for e in errors:
        print(f"ERROR   : {e}")
    if errors:
        print("NOT OK: fix the errors above before sending")
        return 1
    print("OK: profile looks sendable")
    return 0


def cmd_send(args) -> int:
    work = _work(args)
    suf = _suf(args)
    errors, warnings, info = validate_profile(work, args.to, args.text)
    for w in warnings:
        print(f"[warn] {w}")
    if errors and not getattr(args, "force", False):
        for e in errors:
            print(f"[error] {e}")
        print("aborting; run `check` for details (or pass --force)")
        return 1

    template = (work / "send_template.pb").read_bytes()
    headers = (work / "send_headers.txt").read_text(encoding="utf-8")
    sender = info.get("sender") or template_info(template)[1]
    recipient = info.get("target") or (normalize_number(args.to) if args.to else template_info(template)[0])
    new_id, frame = modified_send(template, args.text, recipient)
    print(f"profile : {getattr(args, 'profile', None) or 'default'}")
    print(f"sender  : {sender}")
    print(f"to      : {recipient}")
    print(f"msg id  : {new_id}")
    print(f"text    : {args.text}")

    hdr_remote = f"/tmp/tachyon{suf}_send_headers.txt"
    frame_remote = f"/tmp/tachyon{suf}_send_frame.bin"
    resp_remote = f"/tmp/tachyon{suf}_send_resp.bin"
    pi = Pi()
    try:
        pi.put(hdr_remote, headers.encode())
        pi.put(frame_remote, frame)
        out = pi.run(
            f"curl --http2 -sS -D - -o {resp_remote} -X POST "
            f"-H @{hdr_remote} --data-binary @{frame_remote} "
            f"{URL_BASE}/SendMessage; "
            f"rc=$?; rm -f {hdr_remote} {frame_remote}; echo curl_rc=$rc"
        )
        for line in out.splitlines():
            if line.lower().startswith(("http/", "grpc-status", "grpc-message", "curl_rc")):
                print(line)
    finally:
        pi.close()
    return 0


CHECK_FRAME = """import sys
b=open(sys.argv[1],'rb').read()
i=0
while i+5<=len(b):
    ln=int.from_bytes(b[i+1:i+5],'big')
    if ln>7 and i+5+ln<=len(b):
        sys.exit(0)
    i+=5+ln
sys.exit(1)
"""

LOOP = """#!/bin/sh
while true; do
  curl --http2 --no-buffer -sS -H @{hdr} --data-binary @{frame} \\
    {url}/ReceiveMessages -o {out} -D {hfile} --max-time 540 &
  CPID=$!
  LAST=-1
  while kill -0 $CPID 2>/dev/null; do
    sz=$(wc -c < {out} 2>/dev/null || echo 0)
    if [ "$sz" != "$LAST" ] && python3 {check} {out}; then
      cp {out} {got}
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) GOT size=$sz" >> {log}
      LAST=$sz
    fi
    sleep 2
  done
  wait $CPID 2>/dev/null
  sz=$(wc -c < {out} 2>/dev/null || echo 0)
  st=$(grep -i "^grpc-status" {hfile} 2>/dev/null | tail -1)
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) size=$sz $st" >> {log}
  sleep 2
done
"""


def _pi_paths(args) -> dict[str, str]:
    suf = _suf(args)
    return {
        "hdr": f"/tmp/tachyon{suf}_recv_headers.txt",
        "frame": f"/tmp/tachyon{suf}_recv_frame.bin",
        "script": f"/tmp/rcs_loop{suf}.sh",
        "out": f"/tmp/rcs_loop{suf}.out",
        "hfile": f"/tmp/rcs_loop{suf}.h",
        "got": f"/tmp/rcs_loop{suf}_got.out",
        "log": f"/tmp/rcs_loop{suf}.log",
        "check": "/tmp/check_frame.py",
    }


def cmd_recv(args) -> int:
    work = _work(args)
    p = _pi_paths(args)
    headers = (work / "recv_headers.txt").read_text(encoding="utf-8")
    frame = (work / "recv_frame.bin").read_bytes()

    pi = Pi()
    try:
        pi.put(p["hdr"], headers.encode())
        pi.put(p["frame"], frame)
        pi.put(p["check"], CHECK_FRAME.encode())
        pi.put(p["script"], LOOP.format(url=URL_BASE, **p).encode())
        # Kill in its own shell: otherwise pkill matches the command line that
        # also contains the script path and kills the shell before nohup runs.
        pi.run(f"chmod +x {p['script']}; pkill -f '[r]cs_loop{_suf(args)}.sh' 2>/dev/null; true")
        out = pi.run(
            f"nohup sh {p['script']} >/dev/null 2>&1 & echo pid=$!; sleep 2; "
            f"ps -ef | grep rcs_loop{_suf(args)}.sh | grep -v grep | head -2"
        )
        print(out)
    finally:
        pi.close()
    return 0


def cmd_status(args) -> int:
    p = _pi_paths(args)
    suf = _suf(args)
    pi = Pi()
    try:
        print(pi.run(
            f"echo '--- log ({p['log']}) ---'; tail -10 {p['log']} 2>/dev/null; "
            f"echo '--- got ---'; ls -la {p['got']} 2>/dev/null; "
            f"echo '--- running ---'; ps -ef | grep rcs_loop{suf}.sh | grep -v grep | head -2"
        ))
    finally:
        pi.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Linux-side Tachyon RCS toolkit")
    ap.add_argument("--work", default=None, help="work directory (default: work[/<profile>])")
    ap.add_argument("--profile", default=None, help="profile name for multiple SIMs (work/<profile>)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_profile(p):
        p.add_argument("--profile", default=argparse.SUPPRESS, help="profile name (work/<profile>)")

    p = sub.add_parser("prepare", help="extract templates/headers from Frida captures")
    p.add_argument("--send-capture", required=True, help="start_write JSONL from a phone SendMessage")
    p.add_argument("--recv-headers", help="extract_headers JSONL for ReceiveMessages headers")
    add_profile(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("check", help="validate profile/template/headers locally (no network)")
    p.add_argument("--to", help="recipient number to validate (default: template recipient)")
    p.add_argument("--text", default="check", help="text used to build the validation frame")
    add_profile(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("send", help="send a text from the Pi")
    p.add_argument("--text", required=True)
    p.add_argument("--to", help="recipient number (default: recipient present in the template)")
    p.add_argument("--force", action="store_true", help="send even if local validation fails")
    add_profile(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("recv", help="start the persistent ReceiveMessages loop")
    add_profile(p)
    p.set_defaults(func=cmd_recv)

    p = sub.add_parser("status", help="show loop log / received file")
    add_profile(p)
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    if args.work and args.profile:
        ap.error("--work and --profile are mutually exclusive")
    args.profile = _validate_profile(args.profile)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
