#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
set -a; [ -f .env ] && . ./.env; set +a
: "${PROFILE:=default}"
PYTHON=${PYTHON:-python3}
if ! "$PYTHON" -V >/dev/null 2>&1; then PYTHON=python; fi
exec "$PYTHON" pc/token_harvester.py --profile "$PROFILE" --pi-push
