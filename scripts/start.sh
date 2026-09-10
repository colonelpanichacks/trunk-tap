#!/usr/bin/env bash
# Start the dashboard. First run: `./scripts/start.sh --fresh` for a clean slate.
# TRUNK_TAP_SETUP_ONLY=1: create .venv + install deps, then exit without
# booting the server (used by install.sh -- keep the bootstrap logic here so
# there is exactly one copy of it).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -U pip wheel
  ./.venv/bin/pip install -r requirements.txt
fi

if [[ "${TRUNK_TAP_SETUP_ONLY:-0}" == "1" ]]; then
  exit 0
fi

exec ./.venv/bin/python app.py "$@"
