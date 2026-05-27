#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q -r requirements.txt

PORT="${PORT:-8765}"
echo "→ http://127.0.0.1:${PORT}"
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --reload
