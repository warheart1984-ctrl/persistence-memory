# Deploy Jarvis Memoryboard on Render

Host **EMR recall** (`emr_recall`) over HTTPS so ChatGPT and remote assistants
can call the Continuity Ledger without a local MCP tunnel.

| Component | Status |
|-----------|--------|
| Docker image + `render.yaml` | **live** (this doc) |
| Render Disk persistence | **live** (operator enables via blueprint) |
| `EMR_RECALL_API_KEY` gate on recall | **live** (`app/auth.py`) |
| Public write endpoints | **disabled** by default (`JARVIS_MEMORY_WRITE_ENABLED=false`) |
| HTTP MCP transport on Render | **declared** — use REST `POST /api/jarvis/tools/emr_recall` or local stdio MCP |

## Architecture

```
Remote client (ChatGPT Action, curl, hosted MCP bridge)
    → https://jarvis-memoryboard.onrender.com/api/jarvis/tools/emr_recall
    → EMR excite → Continuity Ledger (/var/data/jarvis-store.json)
```

Local dev unchanged: stdio MCP → loopback HTTP.

## Prerequisites

1. [Render](https://render.com) account
2. **Starter plan** (or higher) — free tier has no persistent disk and cold-start limits
3. Git repo: [warheart1984-ctrl/persistence-memory](https://github.com/warheart1984-ctrl/persistence-memory)

## Deploy steps

### 1. Create Web Service from blueprint

**Option A — Blueprint**

```bash
render blueprint launch
```

**Option B — Dashboard**

1. New → Web Service → connect `persistence-memory`
2. Runtime: Docker
3. Add **Persistent Disk**: mount `/var/data`, 1 GB
4. Health check path: `/health`

### 2. Environment variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `EMR_RECALL_API_KEY` | **yes** (public) | — | Generate: `openssl rand -hex 32` |
| `JARVIS_STORE_PATH` | yes | `/var/data/jarvis-store.json` | Must live on Render Disk |
| `JARVIS_EMR_DYNAMICS_PATH` | recommended | `/var/data/emr-dynamics.json` | Reinforcement overlay |
| `JARVIS_MEMORY_WRITE_ENABLED` | recommended | `false` | Blocks POST/PATCH/DELETE on ledger |
| `JARVIS_PROTECT_LEDGER_READ` | recommended | `true` (Render) | Requires API key for `GET /api/jarvis/memory/*` |
| `JARVIS_CORS_ORIGINS` | optional | `*` | Restrict in production |
| `PORT` | auto | Render injects | Do not override |

### 3. Verify

```bash
curl -s https://YOUR-SERVICE.onrender.com/health | jq

curl -s -X POST https://YOUR-SERVICE.onrender.com/api/jarvis/tools/emr_recall \
  -H "Authorization: Bearer $EMR_RECALL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"intent":"code","query":"EMR release bundle","max_memories":5}'
```

## Security notes

- With `JARVIS_PROTECT_LEDGER_READ=true` and `EMR_RECALL_API_KEY` set, all
  `GET /api/jarvis/memory/*` routes require the same operator key as recall.
- Never commit `EMR_RECALL_API_KEY` to git — Render secret only.

## Local Docker smoke test

```bash
docker build -t jarvis-memoryboard .
docker run --rm -p 8001:8001 \
  -e EMR_RECALL_API_KEY=test-key \
  -e JARVIS_STORE_PATH=/tmp/jarvis-store.json \
  -e JARVIS_MEMORY_WRITE_ENABLED=false \
  jarvis-memoryboard
```
