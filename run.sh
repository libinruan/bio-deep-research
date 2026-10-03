#!/usr/bin/env bash
# Start Bio Deep Research on http://127.0.0.1:8790
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec ./.conda/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port "${BDR_PORT:-8790}"
