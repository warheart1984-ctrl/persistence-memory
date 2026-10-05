#!/usr/bin/env bash
# Stdio MCP launcher for tunnel-client / local hosts.
# No secrets. JARVIS_MEMORYBOARD_URL must be set explicitly (no default), e.g. http://127.0.0.1:8011 through an SSH tunnel.
#
# MCP writes (emr_remember / emr_upsert) are enforced in the memoryboard uvicorn
# process via JARVIS_MCP_WRITE_ENABLED — NOT here. Start memoryboard with:
#   export JARVIS_MCP_WRITE_ENABLED=true
#   uvicorn app.main:app --host 127.0.0.1 --port <port>
# or: scripts/start-memoryboard.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export JARVIS_MEMORYBOARD_URL="${JARVIS_MEMORYBOARD_URL:?JARVIS_MEMORYBOARD_URL is not set; set it to the ledger you mean, e.g. http://127.0.0.1:8011 through an SSH tunnel (no default address)}"
exec python3 -m mcp_server
