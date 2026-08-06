#!/bin/bash
# Run the 800x480 work-calendar image server.
# Creates/uses a local venv, installs deps, and starts the app. The app itself
# only serves on the restream LAN (via en0) + loopback — see app/netguard.py.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-/opt/homebrew/bin/python3.12}"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Install deps only if fastapi is missing (fast no-op on subsequent runs).
if ! python -c "import fastapi" >/dev/null 2>&1; then
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
fi

# Drop the rendered 800x480 image into ~/Pictures (picked up by whatever syncs
# that folder). Override EINK_RENDER_IMAGE to change the destination.
export EINK_RENDER_IMAGE="${EINK_RENDER_IMAGE:-$HOME/Pictures/eink-calendar-800x480.png}"

# Plain HTTP so a headers-less e-ink device can fetch the capability URL
# (self-signed HTTPS is rejected by such devices). LAN only.
export SSL_ENABLED="${SSL_ENABLED:-0}"

exec python -m app.mac_main
