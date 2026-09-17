#!/usr/bin/env bash
# Stdio MCP launcher for tunnel-client / local hosts.
# No secrets — JARVIS_MEMORYBOARD_URL defaults to loopback memoryboard.
#
# MCP writes (emr_remember / emr_upsert) are enforced in the memoryboard uvicorn
# process via JARVIS_MCP_WRITE_ENABLED — NOT here. Start memoryboard with:
#   export JARVIS_MCP_WRITE_ENABLED=true
#   uvicorn app.main:app --host 127.0.0.1 --port 8001
# or: scripts/start-memoryboard.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export JARVIS_MEMORYBOARD_URL="${JARVIS_MEMORYBOARD_URL:-http://127.0.0.1:8001}"
exec python3 -m mcp_server
