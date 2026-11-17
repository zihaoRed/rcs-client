"""Minimal RCS/Tachyon SendMessage helpers for the Pi web app.

No paramiko / SSH dependency: this module runs locally on the Pi and uses curl.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import secrets
import struct
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent

URL_BASE = (
    "https://instantmessaging-pa-jms-us.googleapis.com/google.internal."
    "communications.instantmessaging.v1.Messaging"
)


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
    s = str(num).strip()
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
    fields = []
    for fn, wt, v in parse(value):
        if fn == 2 and wt == 2:
            fields.append((fn, wt, number.encode()))
        else:
            fields.append((fn, wt, v))
    return enc(fields)


def modified_send(template_pb: bytes, text: str, to: str | None = None,
                  sender: str | None = None) -> tuple[str, bytes]:
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
                elif f2 == 8 and w2 == 2 and sender:
                    msg.append((f2, w2, _rewrite_address(x2, sender)))
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


def send_message(work: str | Path, peer: str, text: str, timeout: int = 30,
                 sender: str | None = None) -> dict:
    """Send a plaintext RCS message from the Pi. Returns a status dict.

    If work/send_template.pb is absent, a sanitized example template is used
    and `sender` (or SENDER / meta.json) must be configured.
    """
    work = Path(work)
    tpl_path = work / "send_template.pb"
    using_example = not tpl_path.exists()
    if using_example:
        tpl_path = BASE / "templates" / "send_template.example.pb"
    ci_path = work / "client_info.bin"
    hdr_path = work / "send_headers.txt"
    if not tpl_path.exists() or not ci_path.exists() or not hdr_path.exists():
        return {
            "ok": False,
            "status": "missing",
            "error": "缺少 send_template.pb / client_info.bin / send_headers.txt",
        }

    try:
        to = normalize_number(peer)
        if not sender:
            try:
                meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
                sender = meta.get("sender") or ""
            except Exception:
                sender = ""
        if not sender:
            sender = os.environ.get("SENDER", "")
        if using_example and not sender:
            return {
                "ok": False,
                "status": "missing_sender",
                "error": "示例模板需要配置 SENDER 或 work/meta.json 的 sender",
            }
        template = tpl_path.read_bytes()
        ci = ci_path.read_bytes()
        headers = hdr_path.read_text(encoding="utf-8")
        fields = list(parse(template))
        has_ci = any(fn == 3 and wt == 2 for fn, wt, _ in fields)
        has_marker = any(fn == 15 and wt == 0 for fn, wt, _ in fields)
        out = []
        for fn, wt, v in fields:
            if fn == 3 and wt == 2:
                out.append((fn, wt, ci))
            elif fn == 15 and wt == 0:
                out.append((fn, wt, 1))
            else:
                out.append((fn, wt, v))
        if not has_ci:
            out.append((3, 2, ci))
        if not has_marker:
            out.append((15, 0, 1))
        template = enc(out)
        new_id, frame = modified_send(template, text, to, sender=sender)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "build_error", "error": str(e)}

    h = work / ".web_send_headers.txt"
    f = work / ".web_send_frame.bin"
    r = work / ".web_send_resp.bin"
    d = work / ".web_send_resp.h"
    h.write_text(headers, encoding="utf-8")
    f.write_bytes(frame)
    cmd = [
        "curl", "--http2", "-sS", "-D", str(d), "-o", str(r), "-X", "POST",
        "-H", f"@{h}", "--data-binary", f"@{f}",
        f"{URL_BASE}/SendMessage", "--max-time", str(timeout),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=max(10, timeout + 10))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "transport", "error": str(e), "id": new_id}

    status = ""
    message = ""
    try:
        for line in d.read_text(encoding="utf-8", errors="replace").splitlines():
            low = line.lower()
            if low.startswith("grpc-status:"):
                status = line.split(":", 1)[1].strip()
            elif low.startswith("grpc-message:"):
                message = line.split(":", 1)[1].strip()
    except OSError:
        pass

    return {
        "ok": status == "0",
        "status": status or "unknown",
        "message": message,
        "id": new_id,
        "peer": to,
        "text": text,
        "auth_expired": status == "16",
    }
