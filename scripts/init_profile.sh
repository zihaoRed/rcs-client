#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
set -a; [ -f .env ] && . ./.env; set +a
: "${PROFILE:=default}"
: "${SENDER:?SENDER is required}"
python3 - "$PROFILE" "$SENDER" <<'PY'
import json, sys
from pathlib import Path
profile, sender = sys.argv[1], sys.argv[2]
p = Path("pc/work") / profile
p.mkdir(parents=True, exist_ok=True)
meta = p / "meta.json"
data = {}
if meta.exists():
    try: data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception: data = {}
data["profile"] = profile
data["sender"] = sender
meta.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"profile {profile} initialized at {p}")
PY
