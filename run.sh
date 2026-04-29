#!/usr/bin/env bash
set -euo pipefail
[[ -f .env ]] && set -a && source .env && set +a
exec python3 -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8040}" --reload
