#!/usr/bin/env python3
"""token_harvester.py — keep a fresh Tachyon client-info token in a work profile.

Attaches a Frida agent to the Google Messages processes, watches every Tachyon
gRPC start (SendMessage / ReceiveMessages / PrewarmReceiver / AckMessages ...),
and atomically updates work/<profile>/send_headers.txt whenever a new
x-goog-ext-202964622-bin token appears.

Run with a Python environment that has the `frida` package:
  python3 pc/token_harvester.py --profile default --pi-push

Options:
  --pi-push        also push send_headers.txt to the Pi over SSH
  --interval N     process poll interval (default 5s)
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import frida

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tachyon_client as tc  # noqa: E402
DEFAULT_WORK = ROOT / "work"
BUNDLE = ROOT / "frida" / "token_harvester_bundle.js"
PROCESSES = [
    "com.google.android.apps.messaging",
    "com.google.android.apps.messaging:rcs",
]

PI_HOST = os.environ.get("PI_HOST", "")
PI_USER = os.environ.get("PI_USER", "pi")
PI_KEY = os.environ.get("PI_KEY", os.path.expanduser("~/.ssh/id_ed25519"))

TOKEN_HEADER = "x-goog-ext-202964622-bin"
WARN_AGE = 100 * 60
EXPIRE_AGE = 140 * 60


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def adb_pid(name: str) -> int | None:
    try:
        out = subprocess.run(
            ["adb", "shell", "pidof", name], capture_output=True, text=True, timeout=8
        ).stdout.strip()
        if not out:
            return None
        return int(out.split()[0])
    except Exception:
        return None


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)


def merge_headers(path: Path, token: str) -> None:
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    replaced = False
    for line in lines:
        m = re.match(r"^([^:]+):\s*(.*)$", line)
        if m and m.group(1).strip().lower() == TOKEN_HEADER:
            out.append(f"{TOKEN_HEADER}: {token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{TOKEN_HEADER}: {token}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Harvester:
    def __init__(self, profile: str, work: Path, pi_push: bool, interval: int):
        self.profile = profile
        self.work = work
        self.prof_dir = work / profile
        self.pi_push = pi_push
        self.interval = interval
        self.device = None
        self.sessions: dict[int, frida.core.Session] = {}
        self.scripts: dict[int, frida.core.Script] = {}
        self.seen: dict[int, float] = {}
        self.settle = 4.0
        self.last_sha = None
        self.last_seen = 0.0
        self.last_warn = 0.0
        self.store = work / "native" / "token_store.jsonl"
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.prof_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.prof_dir / "meta.json"
        self.headers_path = self.prof_dir / "send_headers.txt"
        self.client_info_path = self.prof_dir / "client_info.bin"
        self.client_info_auto_path = self.prof_dir / "client_info_auto.bin"
        self.recv_headers_path = self.prof_dir / "recv_phone_headers.txt"
        self.recv_frame_path = self.prof_dir / "recv_phone_frame.bin"
        self.recv_client_info_path = self.prof_dir / "recv_phone_client_info.bin"
        self.send_buf: dict[int, dict] = {}
        self.recv_buf: dict[int, dict] = {}
        self.last_recv_push = 0.0

    # ---- token handling -------------------------------------------------
    def active_number(self) -> str | None:
        """Number of the current default subscription, digits only."""
        try:
            out = subprocess.run(
                ["adb", "shell", "dumpsys", "isub"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
            ).stdout
        except Exception:
            return None
        m = re.search(r"defaultSmsSubId=(\d+)", out)
        if not m:
            return None
        sub = m.group(1)
        for line in out.splitlines():
            if f"[SubscriptionInfoInternal: id={sub} " in line:
                n = re.search(r"number=([0-9+]+)", line)
                if n:
                    return re.sub(r"\D", "", n.group(1))
        return None

    def handle_token(self, token: str, method: str, pid: int) -> None:
        sha = hashlib.sha256(token.encode()).hexdigest()[:16]
        self.last_seen = time.time()
        if sha == self.last_sha:
            return
        # identity guard: only update this profile when its SIM is the active one
        meta0 = {}
        try:
            meta0 = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        want = re.sub(r"\D", "", str(meta0.get("sender", "")))
        if want:
            got = self.active_number()
            if got and got != want:
                log(f"token ignored: active number {got} != profile {want}")
                return
        if sha == meta0.get("token_sha") and meta0.get("token_captured_ms"):
            # same token as persisted: keep the original capture time
            self.last_sha = sha
            return
        self.last_sha = sha
        ts = now_iso()
        with self.store.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "sha": sha, "method": method, "pid": pid}) + "\n")
        merge_headers(self.headers_path, token)
        meta = {}
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update(
            {
                "profile": self.profile,
                "token_sha": sha,
                "token_captured_at": ts,
                "token_captured_ms": int(time.time() * 1000),
                "token_method": method,
                "token_pid": pid,
            }
        )
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.meta_path)
        log(f"token updated sha={sha} method={method} pid={pid}")
        if self.pi_push:
            self.push_pi()

    def push_pi(self) -> None:
        try:
            import paramiko

            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(PI_HOST, username=PI_USER, key_filename=PI_KEY, timeout=10)
            remote_dir = f"/home/{PI_USER}/rcsclient/work/{self.profile}"
            stdin, stdout, stderr = cli.exec_command(f"mkdir -p {remote_dir}")
            stdout.channel.recv_exit_status()
            sftp = cli.open_sftp()
            for name in ("send_headers.txt", "client_info.bin", "client_info_auto.bin",
                         "send_template.pb", "recv_phone_headers.txt",
                         "recv_phone_frame.bin", "recv_phone_client_info.bin"):
                src = self.prof_dir / name
                if not src.exists():
                    continue
                tmp = remote_dir + f"/.{name}.tmp"
                target = remote_dir + "/" + name
                with sftp.open(tmp, "wb") as f:
                    f.write(src.read_bytes())
                try:
                    sftp.posix_rename(tmp, target)
                except IOError:
                    try:
                        sftp.remove(target)
                    except IOError:
                        pass
                    sftp.rename(tmp, target)
            sftp.close()
            cli.close()
            log("pushed send_headers.txt to Pi")
        except Exception as e:  # noqa: BLE001
            log(f"pi push failed: {e}")

    # ---- frida ----------------------------------------------------------
    def on_message(self, pid: int, msg, data) -> None:
        if msg.get("type") != "send":
            return
        p = msg.get("payload") or {}
        k = p.get("k")
        if k == "token":
            self.handle_token(p.get("token", ""), p.get("m", ""), pid)
        elif k == "start":
            log(f"rpc start pid={pid} method={p.get('m')} hasToken={p.get('hasToken')}")
            if "Registration" in (p.get("m") or ""):
                with (self.work / "native" / "regrefresh_frames.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(p) + "\n")
        elif k == "sendwrite":
            self._on_sendwrite(pid, p)
        elif k == "recvwrite":
            self._log_body(p)
            self._on_recvwrite(pid, p)
        elif k in ("pullwrite", "pullflush", "pullurlreq", "ackwrite", "prewarmwrite"):
            self._log_body(p)
        elif k == "reqstart":
            self._log_body(p)
            if "ReceiveMessages" in (p.get("m") or ""):
                self._on_recv_start(p)

    def _log_body(self, p: dict) -> None:
        with (self.work / "native" / "body_writes.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    def _write_meta(self, updates: dict) -> None:
        meta = {}
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        meta.update(updates)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def _on_recv_start(self, p: dict) -> None:
        hdrs = p.get("hdrs") or {}
        lines = []
        for k, v in hdrs.items():
            lk = k.lower()
            if lk in tc.KEEP_HEADERS:
                if lk == "grpc-timeout":
                    v = "900S"
                lines.append(f"{k}: {v}")
        if not lines:
            return
        text = "\n".join(lines) + "\n"
        old = ""
        try:
            old = self.recv_headers_path.read_text(encoding="utf-8")
        except OSError:
            pass
        if text == old:
            return
        tmp = self.recv_headers_path.with_suffix(".txt.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.recv_headers_path)
        token = hdrs.get("x-goog-ext-202964622-bin", "")
        self._write_meta({
            "recv_phone_headers_captured_at": now_iso(),
            "recv_phone_token_sha": hashlib.sha256(token.encode()).hexdigest()[:16] if token else "",
        })
        log(f"recv headers updated token_len={len(token)}")
        if self.pi_push:
            self.push_pi()

    def _on_recvwrite(self, pid: int, p: dict) -> None:
        try:
            b = bytes.fromhex(p.get("hex", ""))
        except ValueError:
            return
        st = self.recv_buf.setdefault(pid, {"hdr": None, "buf": bytearray(), "total": 0})
        if st["hdr"] is None:
            if len(b) >= 5 and b[0] in (0, 1):
                total = int.from_bytes(b[1:5], "big")
                if total <= 0 or total > 1_000_000:
                    return
                if len(b) >= 5 + total:
                    self._save_recv_frame(b[:5 + total])
                else:
                    st["hdr"] = b[:5]
                    st["total"] = total
                    st["buf"] = bytearray(b[5:])
        else:
            st["buf"] += b
            if len(st["buf"]) >= st["total"]:
                frame = st["hdr"] + bytes(st["buf"][:st["total"]])
                st["hdr"] = None
                st["buf"] = bytearray()
                st["total"] = 0
                self._save_recv_frame(frame)

    def _save_recv_frame(self, frame: bytes) -> None:
        sha = hashlib.sha256(frame).hexdigest()[:16]
        try:
            old = self.recv_frame_path.read_bytes()
            if hashlib.sha256(old).hexdigest()[:16] == sha:
                return
        except OSError:
            pass
        tmp = self.recv_frame_path.with_suffix(".bin.tmp")
        tmp.write_bytes(frame)
        os.replace(tmp, self.recv_frame_path)
        ci = None
        try:
            comp = frame[0]
            ln = int.from_bytes(frame[1:5], "big")
            payload = frame[5:5 + ln]
            if comp == 1:
                payload = gzip.decompress(payload)
            for fn, wt, v in tc.parse(payload):
                if fn == 1 and wt == 2:
                    ci = v
                    break
        except Exception:
            pass
        updates = {
            "recv_phone_frame_sha": sha,
            "recv_phone_frame_len": len(frame),
            "recv_phone_frame_captured_at": now_iso(),
        }
        if ci:
            tmp = self.recv_client_info_path.with_suffix(".bin.tmp")
            tmp.write_bytes(ci)
            os.replace(tmp, self.recv_client_info_path)
            updates.update({
                "recv_phone_client_info_sha": hashlib.sha256(ci).hexdigest()[:16],
                "recv_phone_client_info_len": len(ci),
            })
        self._write_meta(updates)
        log(f"recv frame updated sha={sha} len={len(frame)} ci={len(ci) if ci else 0}")
        if self.pi_push and time.time() - self.last_recv_push > 60:
            self.last_recv_push = time.time()
            self.push_pi()
        elif k in ("regwrite", "respHeaders", "trailers"):
            with (self.work / "native" / "regrefresh_frames.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(p) + "\n")
        elif k in ("read", "respToken"):
            # keep for later RegisterRefresh analysis; avoid spamming
            with (self.work / "native" / "token_responses.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(p) + "\n")

    # ---- SendMessage body capture --------------------------------------
    def _on_sendwrite(self, pid: int, p: dict) -> None:
        st = self.send_buf.setdefault(pid, {"hdr": None, "buf": bytearray(), "total": 0})
        b = bytes.fromhex(p.get("hex", ""))
        if st["hdr"] is None:
            if len(b) == 5 and b[0] in (0, 1):
                st["hdr"] = b
                st["total"] = int.from_bytes(b[1:5], "big")
                st["buf"] = bytearray()
        else:
            st["buf"] += b
            if len(st["buf"]) >= st["total"]:
                hdr = st["hdr"]
                data = bytes(st["buf"])
                st["hdr"] = None
                self._handle_send_frame(hdr, data)

    def _handle_send_frame(self, hdr: bytes, payload: bytes) -> None:
        try:
            data = gzip.decompress(payload) if hdr[0] == 1 else payload
        except Exception:
            return
        try:
            from tachyon_client import parse
            ci = None
            mid = ""
            ctype = ""
            for fn, wt, v in parse(data):
                if fn == 2 and wt == 2:
                    for f2, w2, x2 in parse(v):
                        if f2 == 1 and w2 == 2:
                            mid = x2.decode("latin1")
                        if f2 == 12 and w2 == 2:
                            for f3, w3, y3 in parse(x2):
                                if f3 == 1 and w3 == 2:
                                    for f4, w4, z4 in parse(y3):
                                        if f4 == 1 and w4 == 2 and not ctype:
                                            ctype = z4.decode("latin1", "replace")
                if fn == 3 and wt == 2:
                    ci = v
            if not ci:
                return
            low = ctype.lower()
            # A plaintext SendMessage is a reusable template for Pi sends.
            if "text/plain" in low:
                tpl = self.prof_dir / "send_template.pb"
                if not tpl.exists():
                    tmp = tpl.with_suffix(".pb.tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, tpl)
                    log(f"send_template.pb captured ({len(data)} bytes)")
                    if self.pi_push:
                        self.push_pi()
            # Keep user-message client info as the primary credential; also
            # retain automatic-traffic values (IMDN / isComposing) separately
            # so we can test whether they work for Pi-initiated sends.
            auto = "imdn" in low or "iscomposing" in low
            self._save_client_info(ci, mid, ctype, auto=auto)
        except Exception as e:  # noqa: BLE001
            log(f"client_info parse failed: {e}")

    def _save_client_info(self, ci: bytes, mid: str, ctype: str, auto: bool = False) -> None:
        sha = hashlib.sha256(ci).hexdigest()[:16]
        target = self.client_info_auto_path if auto else self.client_info_path
        try:
            old = target.read_bytes()
            if hashlib.sha256(old).hexdigest()[:16] == sha:
                return
        except OSError:
            pass
        tmp = target.with_suffix(".bin.tmp")
        tmp.write_bytes(ci)
        os.replace(tmp, target)
        meta = {}
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        prefix = "auto_client_info" if auto else "client_info"
        meta.update({
            f"{prefix}_sha": sha,
            f"{prefix}_captured_at": now_iso(),
            f"{prefix}_len": len(ci),
            f"{prefix}_source_mid": mid,
            f"{prefix}_source_type": ctype,
        })
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.meta_path)
        log(f"client_info updated sha={sha} type={ctype} mid={mid} auto={auto}")
        if self.pi_push:
            self.push_pi()

    def attach(self, pid: int) -> None:
        if pid in self.sessions:
            return
        first = self.seen.setdefault(pid, time.time())
        if time.time() - first < self.settle:
            return
        try:
            s = self.device.attach(pid)
            src = BUNDLE.read_text(encoding="utf-8")
            sc = s.create_script(src)
            sc.on("message", lambda m, d, pid=pid: self.on_message(pid, m, d))
            sc.load()
            self.sessions[pid] = s
            self.scripts[pid] = sc
            log(f"attached pid={pid}")
        except Exception as e:  # noqa: BLE001
            log(f"attach pid={pid} failed: {e}")

    def reap(self, live: set[int]) -> None:
        for pid in list(self.sessions):
            if pid not in live:
                try:
                    self.sessions[pid].detach()
                except Exception:
                    pass
                self.sessions.pop(pid, None)
                self.scripts.pop(pid, None)
                self.seen.pop(pid, None)
                log(f"detached pid={pid} (gone)")

    def age_check(self) -> None:
        meta = {}
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return
        ms = meta.get("token_captured_ms")
        if not ms:
            return
        age = time.time() - ms / 1000.0
        if age > EXPIRE_AGE and time.time() - self.last_warn > 600:
            self.last_warn = time.time()
            log(f"WARNING token age {age/60:.0f} min (> {EXPIRE_AGE/60:.0f}): capture a fresh one")
        elif age > WARN_AGE and time.time() - self.last_warn > 600:
            self.last_warn = time.time()
            log(f"token age {age/60:.0f} min: approaching expiry")

    def run(self) -> int:
        log(f"harvester profile={self.profile} work={self.prof_dir}")
        try:
            self.device = frida.get_usb_device(timeout=10)
        except Exception as e:  # noqa: BLE001
            log(f"frida device error: {e}")
            return 1
        while True:
            live = set()
            for name in PROCESSES:
                pid = adb_pid(name)
                if pid:
                    live.add(pid)
                    self.attach(pid)
            self.reap(live)
            self.age_check()
            time.sleep(self.interval)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    ap.add_argument("--work", default=str(DEFAULT_WORK))
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--pi-push", action="store_true")
    a = ap.parse_args()
    h = Harvester(a.profile, Path(a.work), a.pi_push, a.interval)
    try:
        return h.run()
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
