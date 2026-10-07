#!/usr/bin/env bash
# Start Bio Deep Research on http://127.0.0.1:8790
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x ./.conda/bin/python ]; then
  echo "No environment at ./.conda. Create it first:" >&2
  echo "  conda env create -p ./.conda -f environment.yml" >&2
  exit 1
fi
[ -f .env ] && set -a && . ./.env && set +a
exec ./.conda/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port "${BDR_PORT:-8790}"
