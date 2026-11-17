#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PYTHON=${PYTHON:-python3}
if ! "$PYTHON" -V >/dev/null 2>&1; then PYTHON=python; fi
"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -r requirements.txt
cd pc/agent
npm ci
npm run build
echo "PC setup complete. Built pc/frida/token_harvester_bundle.js"
