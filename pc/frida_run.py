#!/usr/bin/env python3
# frida_run.py — attach to a process, load a compiled bundle, stream messages to stdout/file.
import sys, time, json, argparse, signal

import frida


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", default=None)
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    dev = frida.get_usb_device(timeout=10)
    pid = a.pid
    if pid is None:
        procs = []
        deadline = time.time() + 20
        while not procs and time.time() < deadline:
            procs = [p for p in dev.enumerate_processes() if p.name == a.process]
            if not procs:
                time.sleep(1)
        if not procs:
            print("process not found:", a.process)
            return 1
        pid = procs[-1].pid
    print("attach", pid, a.process, flush=True)
    s = dev.attach(pid)
    src = open(a.bundle, encoding="utf-8").read()
    sc = s.create_script(src)
    out = open(a.out, "w", encoding="utf-8") if a.out else None

    def on(msg, data):
        line = json.dumps(msg, ensure_ascii=False, default=str)
        print(line[:4000], flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    sc.on("message", on)
    time.sleep(getattr(a, "settle", 3))
    sc.load()
    t0 = time.time()
    try:
        while time.time() - t0 < a.seconds:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    if out:
        out.close()
    try:
        s.detach()
    except Exception:
        pass
    print("detached", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
