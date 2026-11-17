#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRC="$ROOT/mls/mls_probe"
VENDOR="$SRC/vendor/mls-rs"
PATCH="$ROOT/mls/patches/0001-relax-credential-capability-check.patch"

if [ ! -d "$VENDOR/.git" ]; then
  mkdir -p "$SRC/vendor"
  git clone --depth 1 --branch 0.51.0 https://github.com/awslabs/mls-rs.git "$VENDOR"
fi
if git -C "$VENDOR" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
  echo "patch already applied"
else
  git -C "$VENDOR" apply "$PATCH"
fi
cd "$SRC"
cargo build --release
mkdir -p /home/pi/mls_probe/target/release
cp target/release/mls_probe /home/pi/mls_probe/target/release/mls_probe
echo "Installed /home/pi/mls_probe/target/release/mls_probe"
