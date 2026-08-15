#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
python3 -m pip install -r requirements.txt -q
if [[ ! -d frontend/node_modules ]]; then
  (cd frontend && npm install)
fi
(cd frontend && npm run build)
export PYTHONPATH=.
exec python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
