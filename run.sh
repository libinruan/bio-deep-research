#!/usr/bin/env bash
# Start Bio Deep Research on http://127.0.0.1:8790
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "then run this script again." >&2
  exit 1
fi

[ -f .env ] && set -a && . ./.env && set +a
# uv creates the environment from uv.lock on first run, fetching Python if needed.
exec uv run --quiet python -m uvicorn app.server:app --host 127.0.0.1 --port "${BDR_PORT:-8790}"
