#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
set -a; [ -f .env ] && . ./.env; set +a
: "${PROFILE:=default}"
exec python3 pc/token_harvester.py --profile "$PROFILE" --pi-push
