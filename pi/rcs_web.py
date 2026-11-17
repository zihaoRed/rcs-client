#!/usr/bin/env python3
"""rcs_web.py — Google Messages-like RCS web UI for the Raspberry Pi.

Runs on the Pi. It:
  * supervises pi_receiver.py (phone ReceiveMessages credentials + MLS decrypt)
  * tails received.jsonl into SQLite
  * exposes a small JSON API and SSE stream
  * sends plaintext RCS messages via the Tachyon SendMessage endpoint

Usage:
  python3 pi/rcs_web.py --work work/default --port 8090
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import rcs_proto  # noqa: E402

STATIC = BASE / "web" / "static"
app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
CTX: dict = {}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default or {}


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.c = sqlite3.connect(str(path), check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        with self.lock:
            self.c.execute("PRAGMA journal_mode=WAL")
            self.c.execute("PRAGMA synchronous=NORMAL")
            self.c.executescript(
                """
                CREATE TABLE IF NOT EXISTS contacts(
                    number TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages(
                    _id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rcs_id TEXT UNIQUE,
                    peer TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    sender TEXT,
                    ctype TEXT,
                    text TEXT,
                    ts TEXT NOT NULL,
                    status TEXT,
                    read INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer, _id);
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self.c.commit()

    def contacts(self):
        with self.lock:
            rows = self.c.execute(
                "SELECT number,name,created_at FROM contacts ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def contact(self, number: str):
        with self.lock:
            r = self.c.execute(
                "SELECT number,name,created_at FROM contacts WHERE number=?", (number,)
            ).fetchone()
        return dict(r) if r else None

    def upsert_contact(self, number: str, name: str):
        with self.lock:
            self.c.execute(
                "INSERT INTO contacts(number,name,created_at) VALUES(?,?,?) "
                "ON CONFLICT(number) DO UPDATE SET name=excluded.name",
                (number, name, now_iso()),
            )
            self.c.commit()
        return self.contact(number)

    def insert_message(self, rcs_id, peer, direction, sender, ctype, text, ts, status, read):
        with self.lock:
            cur = self.c.execute(
                "INSERT OR IGNORE INTO messages"
                "(rcs_id,peer,direction,sender,ctype,text,ts,status,read) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (rcs_id, peer, direction, sender, ctype, text, ts, status, read),
            )
            self.c.commit()
            return cur.rowcount > 0

    def message_by_rcs(self, rcs_id: str):
        with self.lock:
            r = self.c.execute(
                "SELECT * FROM messages WHERE rcs_id=?", (rcs_id,)
            ).fetchone()
        return dict(r) if r else None

    def messages(self, peer: str, limit: int = 1000):
        with self.lock:
            rows = self.c.execute(
                "SELECT * FROM messages WHERE peer=? ORDER BY _id ASC LIMIT ?",
                (peer, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_status(self, rcs_id: str, status: str):
        with self.lock:
            cur = self.c.execute(
                "UPDATE messages SET status=? WHERE rcs_id=? AND direction='out'",
                (status, rcs_id),
            )
            self.c.commit()
            return cur.rowcount > 0

    def mark_read(self, peer: str):
        with self.lock:
            self.c.execute(
                "UPDATE messages SET read=1 WHERE peer=? AND direction='in'", (peer,)
            )
            self.c.commit()

    def unread(self, peer: str) -> int:
        with self.lock:
            r = self.c.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE peer=? AND direction='in' AND read=0",
                (peer,),
            ).fetchone()
        return int(r["n"]) if r else 0

    def threads(self):
        with self.lock:
            peers = self.c.execute(
                "SELECT peer, MAX(_id) AS last_id FROM messages GROUP BY peer"
            ).fetchall()
            out = []
            for p in peers:
                last = self.c.execute(
                    "SELECT * FROM messages WHERE _id=?", (p["last_id"],)
                ).fetchone()
                contact = self.c.execute(
                    "SELECT name FROM contacts WHERE number=?", (p["peer"],)
                ).fetchone()
                unread = self.c.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE peer=? AND direction='in' AND read=0",
                    (p["peer"],),
                ).fetchone()
                out.append({
                    "peer": p["peer"],
                    "name": (contact["name"] if contact else p["peer"]),
                    "last": dict(last) if last else None,
                    "unread": int(unread["n"]) if unread else 0,
                })
        out.sort(key=lambda t: (t["last"] or {}).get("_id", 0), reverse=True)
        return out


class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs: set[queue.Queue] = set()

    def subscribe(self):
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, event: dict):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass


class Ingestor:
    def __init__(self, db: DB, bus: Bus, path: Path):
        self.db = db
        self.bus = bus
        self.path = path
        self.offset = 0
        self.pending = b""

    def load_history(self):
        if not self.path.exists():
            return
        try:
            with self.path.open("rb") as f:
                for raw in f:
                    self._line(raw)
                self.offset = f.tell()
        except Exception as e:  # noqa: BLE001
            print(f"[ingest] history error: {e}", flush=True)

    def _line(self, raw: bytes):
        line = raw.strip()
        if not line.startswith(b"{"):
            return
        try:
            obj = json.loads(line)
        except Exception:
            return
        self.ingest(obj)

    def ingest(self, obj: dict):
        ctype = str(obj.get("type") or "")
        text = str(obj.get("text") or "")
        mid = str(obj.get("id") or "")
        sender = str(obj.get("from") or "")
        ts = str(obj.get("ts") or now_iso())

        try:
            peer = rcs_proto.normalize_number(sender) if sender else ""
        except Exception:
            peer = sender

        if "message/mls" in ctype or ctype.startswith("text/plain"):
            if not peer:
                return
            if not mid:
                mid = f"in-{int(time.time() * 1000)}-{abs(hash(text)) % 100000}"
            inserted = self.db.insert_message(
                mid, peer, "in", peer, ctype, text, ts, "received", 0
            )
            if inserted:
                msg = self.db.message_by_rcs(mid)
                self.bus.publish({"type": "message", "peer": peer, "message": msg})
            return

        if "message/imdn+xml" in ctype:
            m = re.search(r"<message-id>(.*?)</message-id>", text, re.S)
            if not m:
                return
            target = m.group(1).strip()
            if "<displayed" in text:
                status = "read"
            elif "<delivered" in text:
                status = "delivered"
            else:
                status = "delivered"
            if self.db.update_status(target, status):
                self.bus.publish({"type": "status", "rcs_id": target, "status": status})
            return

        if "iscomposing" in ctype:
            m = re.search(r"<state>(.*?)</state>", text, re.S)
            if m and m.group(1).strip() == "active" and peer:
                self.bus.publish({"type": "typing", "peer": peer})

    def tail(self):
        while True:
            try:
                if not self.path.exists():
                    time.sleep(2)
                    continue
                size = self.path.stat().st_size
                if size < self.offset:
                    self.offset = 0
                    self.pending = b""
                if size == self.offset:
                    time.sleep(1)
                    continue
                with self.path.open("rb") as f:
                    f.seek(self.offset)
                    data = f.read()
                    self.offset = f.tell()
                self.pending += data
                while b"\n" in self.pending:
                    raw, self.pending = self.pending.split(b"\n", 1)
                    self._line(raw)
            except Exception as e:  # noqa: BLE001
                print(f"[ingest] tail error: {e}", flush=True)
                time.sleep(2)


class ReceiverSupervisor:
    def __init__(self, work: Path, tools: Path, enabled: bool = True, max_time: int = 60):
        self.work = work
        self.tools = tools
        self.enabled = enabled
        self.max_time = max_time
        self.proc = None
        self.stop_flag = False
        self.log_path = work / "web_receiver.log"
        self.pid_path = work / "web_receiver.pid"

    def _kill_stale(self):
        try:
            out = subprocess.run(
                ["pgrep", "-af", "pi_receiver.py"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2:
                    continue
                pid_s, cmd = parts
                if str(self.work) not in cmd and f"--work {self.work.name}" not in cmd \
                        and f"--work work/{self.work.name}" not in cmd:
                    continue
                try:
                    pid = int(pid_s)
                except ValueError:
                    continue
                if pid != os.getpid():
                    try:
                        os.kill(pid, signal.SIGTERM)
                        print(f"[receiver] stopped stale pid {pid}", flush=True)
                    except OSError:
                        pass
        except Exception:
            pass
        try:
            self.pid_path.unlink()
        except OSError:
            pass

    def start(self):
        if not self.enabled:
            print("[receiver] disabled", flush=True)
            return
        self._kill_stale()
        self._spawn()
        threading.Thread(target=self._monitor, daemon=True).start()

    def _spawn(self):
        cmd = [
            sys.executable, str(self.tools / "pi_receiver.py"),
            "--work", str(self.work),
            "--max-time", str(self.max_time),
            "--recv-headers", str(self.work / "recv_phone_headers.txt"),
            "--recv-frame", str(self.work / "recv_phone_frame.bin"),
        ]
        log = open(self.log_path, "ab", buffering=0)
        self.proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.tools.parent)
        )
        try:
            self.pid_path.write_text(str(self.proc.pid), encoding="utf-8")
        except OSError:
            pass
        print(f"[receiver] started pid {self.proc.pid}", flush=True)

    def _monitor(self):
        while not self.stop_flag:
            if self.proc is None:
                return
            rc = self.proc.wait()
            if self.stop_flag:
                return
            print(f"[receiver] exited rc={rc}; restarting in 5s", flush=True)
            time.sleep(5)
            if self.stop_flag:
                return
            self._spawn()

    def stop(self):
        self.stop_flag = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


def _auth_state():
    work: Path = CTX["work"]
    state = read_json(work / "receiver_state.json")
    meta = read_json(work / "meta.json")
    return {
        "receiver_last_error": state.get("last_error", ""),
        "receiver_last_connect": state.get("last_connect", ""),
        "send_token_captured_at": meta.get("token_captured_at"),
        "client_info_captured_at": meta.get("client_info_captured_at"),
        "recv_headers_captured_at": meta.get("recv_phone_headers_captured_at"),
        "recv_frame_captured_at": meta.get("recv_phone_frame_captured_at"),
        "recv_headers_mtime": _mtime(work / "recv_phone_headers.txt"),
        "recv_frame_mtime": _mtime(work / "recv_phone_frame.bin"),
        "send_headers_mtime": _mtime(work / "send_headers.txt"),
        "client_info_mtime": _mtime(work / "client_info.bin"),
        "send_auth_expired": bool(CTX.get("send_auth_expired")),
    }


def _mtime(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


@app.get("/")
def index():
    resp = send_from_directory(STATIC, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/state")
def api_state():
    db: DB = CTX["db"]
    return jsonify({
        "auth": _auth_state(),
        "contacts": db.contacts(),
        "threads": db.threads(),
        "self": CTX.get("self"),
        "server_time": now_iso(),
    })


@app.get("/api/threads")
def api_threads():
    return jsonify({"threads": CTX["db"].threads()})


@app.get("/api/messages/<path:peer>")
def api_messages(peer: str):
    try:
        peer = rcs_proto.normalize_number(peer)
    except Exception:
        return jsonify({"error": "invalid peer"}), 400
    db: DB = CTX["db"]
    return jsonify({
        "peer": peer,
        "contact": db.contact(peer),
        "messages": db.messages(peer),
    })


@app.post("/api/read/<path:peer>")
def api_read(peer: str):
    try:
        peer = rcs_proto.normalize_number(peer)
    except Exception:
        return jsonify({"error": "invalid peer"}), 400
    CTX["db"].mark_read(peer)
    return jsonify({"ok": True})


@app.post("/api/send")
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    peer = str(data.get("peer") or "").strip()
    text = str(data.get("text") or "").strip()
    if not peer or not text:
        return jsonify({"ok": False, "error": "peer and text are required"}), 400
    try:
        peer = rcs_proto.normalize_number(peer)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400

    res = rcs_proto.send_message(
        CTX["work"], peer, text, timeout=30,
        sender=CTX.get("self") or None,
    )
    status = "sent" if res.get("ok") else ("auth" if res.get("auth_expired") else "failed")
    mid = res.get("id") or f"out-{int(time.time() * 1000)}"
    db: DB = CTX["db"]
    db.insert_message(
        mid, peer, "out", CTX.get("self") or "", "text/plain", text,
        now_iso(), status, 1,
    )
    if res.get("auth_expired"):
        CTX["send_auth_expired"] = True
        CTX["bus"].publish({
            "type": "auth",
            "auth_expired": True,
            "message": res.get("message") or "RCS 凭据失效",
        })
    else:
        if res.get("ok"):
            CTX["send_auth_expired"] = False
    msg = db.message_by_rcs(mid)
    CTX["bus"].publish({"type": "message", "peer": peer, "message": msg})
    res["message_row"] = msg
    return jsonify(res)


@app.get("/api/contacts")
def api_contacts():
    return jsonify({"contacts": CTX["db"].contacts()})


@app.post("/api/contacts")
def api_contacts_add():
    data = request.get_json(force=True, silent=True) or {}
    number = str(data.get("number") or "").strip()
    name = str(data.get("name") or "").strip() or number
    if not number:
        return jsonify({"ok": False, "error": "number required"}), 400
    try:
        number = rcs_proto.normalize_number(number)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400
    contact = CTX["db"].upsert_contact(number, name)
    CTX["bus"].publish({"type": "contacts"})
    return jsonify({"ok": True, "contact": contact})


@app.get("/api/events")
def api_events():
    bus: Bus = CTX["bus"]
    q = bus.subscribe()

    def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/home/pi/rcsclient/work/default")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--no-receiver", action="store_true")
    ap.add_argument("--receiver-max-time", type=int, default=60)
    a = ap.parse_args()

    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    db = DB(work / "web.db")
    bus = Bus()
    meta = read_json(work / "meta.json")
    CTX.update({
        "work": work,
        "db": db,
        "bus": bus,
        "self": meta.get("sender") or os.environ.get("SENDER", ""),
        "send_auth_expired": False,
    })

    ingestor = Ingestor(db, bus, work / "received.jsonl")
    ingestor.load_history()
    threading.Thread(target=ingestor.tail, daemon=True).start()
    print(f"[web] history loaded; threads={len(db.threads())}", flush=True)

    receiver = ReceiverSupervisor(
        work, Path(__file__).resolve().parent, not a.no_receiver, a.receiver_max_time
    )
    receiver.start()
    CTX["receiver"] = receiver

    def _shutdown(*_):
        receiver.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[web] listening on http://{a.host}:{a.port}", flush=True)
    app.run(host=a.host, port=a.port, threaded=True, use_reloader=False, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
