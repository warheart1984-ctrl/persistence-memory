#!/usr/bin/env bash
# Stdio MCP launcher for tunnel-client / local hosts.
# No secrets — JARVIS_MEMORYBOARD_URL defaults to loopback memoryboard.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export JARVIS_MEMORYBOARD_URL="${JARVIS_MEMORYBOARD_URL:-http://127.0.0.1:8001}"
exec python3 -m mcp_server
