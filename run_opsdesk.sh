#!/usr/bin/env bash
set -euo pipefail

# Always run from the script directory
cd "$(dirname "$0")"

if [[ ! -f .venv/bin/activate ]]; then
  echo "[run_opsdesk] .venv not found. Create it and install requirements first." >&2
  echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

# Activate venv, run the app, then deactivate on exit (including when you press 'q')
source .venv/bin/activate
.venv/bin/python app.py
deactivate || true

