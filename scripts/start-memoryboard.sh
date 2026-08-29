#!/usr/bin/env bash
# Start Continuity Ledger on loopback :8001 with local MCP writes enabled.
# Writes (emr_remember / emr_upsert) are gated in the uvicorn process — NOT the stdio MCP proxy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env.local"
  set +a
fi

export JARVIS_MCP_WRITE_ENABLED="${JARVIS_MCP_WRITE_ENABLED:-true}"
HOST="${JARVIS_HOST:-127.0.0.1}"
PORT="${JARVIS_PORT:-8001}"

exec uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
