#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
set -a; [ -f .env ] && . ./.env; set +a
: "${PI_HOST:?PI_HOST is required}"
: "${PI_USER:=pi}"
: "${PI_KEY:=$HOME/.ssh/id_ed25519}"
: "${PROFILE:=default}"
: "${MLS_PROBE:=/home/pi/mls_probe/target/release/mls_probe}"
SSH=(ssh -i "$PI_KEY" -o StrictHostKeyChecking=accept-new "$PI_USER@$PI_HOST")

"${SSH[@]}" "mkdir -p ~/rcsclient/tools ~/rcsclient/work/$PROFILE"
scp -i "$PI_KEY" -o StrictHostKeyChecking=accept-new -r pi/. "$PI_USER@$PI_HOST:~/rcsclient/tools/"
"${SSH[@]}" "cat > ~/rcsclient/.env" <<ENV
PROFILE=$PROFILE
SENDER=${SENDER:-}
MLS_PROBE=$MLS_PROBE
WEB_PORT=${WEB_PORT:-8090}
ENV
scp -i "$PI_KEY" -o StrictHostKeyChecking=accept-new deploy/rcs-web.service "$PI_USER@$PI_HOST:/tmp/rcs-web.service"
"${SSH[@]}" "sudo cp /tmp/rcs-web.service /etc/systemd/system/rcs-web.service && \
  sudo systemctl daemon-reload && \
  sudo systemctl enable rcs-web >/dev/null 2>&1 && \
  sudo systemctl restart rcs-web && \
  sleep 1 && sudo systemctl --no-pager --lines=3 status rcs-web | tail -5"
echo "Deployed. Web UI: http://$PI_HOST:${WEB_PORT:-8090}"
