#!/usr/bin/env bash
# OpenAI Secure MCP Tunnel — Jarvis EMR (Electrom-Matic Recall)
#
# Prerequisites:
#   1. Memoryboard on loopback :8001
#   2. tunnel-client v0.0.13+ on PATH (~/.local/bin/tunnel-client)
#   3. Platform tunnel_id + Runtime API key (CONTROL_PLANE_API_KEY)
#
# NEVER commit real API keys. Export in shell or use ~/.config/tunnel-client/.env (chmod 600).
#
# Install binary (Linux amd64 example):
#   curl -fsSL -o /tmp/tc.zip \
#     https://github.com/openai/tunnel-client/releases/download/v0.0.13/tunnel-client-v0.0.13-linux-amd64.zip
#   unzip -q /tmp/tc.zip -d /tmp/tc && install -m755 /tmp/tc/tunnel-client ~/.local/bin/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TUNNEL_ID="${TUNNEL_ID:-tunnel_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX}"
PROFILE="${TUNNEL_PROFILE:-jarvis-emr}"
MCP_LAUNCHER="${REPO_ROOT}/scripts/run-emr-mcp-stdio.sh"
HEALTH_ADDR="${TUNNEL_HEALTH_ADDR:-127.0.0.1:8788}"

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
  echo "ERROR: export CONTROL_PLANE_API_KEY from Platform → Settings → API keys (Runtime key with Tunnels Read+Use)." >&2
  exit 1
fi

# Terminal 1 — memoryboard (if not already running):
#   cd "$REPO_ROOT" && uvicorn app.main:app --host 127.0.0.1 --port 8001

tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile "$PROFILE" \
  --tunnel-id "$TUNNEL_ID" \
  --mcp-command "$MCP_LAUNCHER" \
  --health-listen-addr "$HEALTH_ADDR" \
  --force

tunnel-client doctor --profile "$PROFILE" --explain

echo ""
echo "Profile: ~/.config/tunnel-client/${PROFILE}.yaml"
echo "Admin UI: http://${HEALTH_ADDR}/ui"
echo ""
echo "Start daemon (keep running while ChatGPT uses the tunnel):"
echo "  export CONTROL_PLANE_API_KEY=\"\$CONTROL_PLANE_API_KEY\""
echo "  tunnel-client run --profile $PROFILE"
echo ""
echo "Verify:"
echo "  curl -fsS http://${HEALTH_ADDR}/healthz   # live"
echo "  curl -fsS http://${HEALTH_ADDR}/readyz    # ready"
echo ""
echo "ChatGPT: Plugins → Create → Connection **Tunnel** → select your tunnel name"
echo "Platform: https://platform.openai.com/settings/organization/tunnels"
