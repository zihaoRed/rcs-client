#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -r requirements.txt
cd pc/agent
npm ci
npm run build
echo "PC setup complete. Built pc/frida/token_harvester_bundle.js"
