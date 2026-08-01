# Operator Deploy Checklist — persistence-memory (Continuity Ledger v1)

> **Status:** complete — Implementor-filled 2026-07-30 (crew2 trail)
> Evidence required per Drive-G-1 before marking any item "complete".
> Evidence basis: `app/` source, `tests/` suite, `Dockerfile`, `docker-compose.yml`, `scripts/smoke-test.ps1`, `SECURITY.md`, `.env.example`, `docs/PLATFORM_LIMITS.md`.

---

## 1. Pre-deploy

### 1a. Python environment

- [ ] Confirm Python 3.11 or 3.12 is installed and active:
  ```powershell
  python --version   # expected: Python 3.11.x or 3.12.x
  ```
- [ ] Install package with dev extras (or prod-only `pip install -e .`):
  ```powershell
  cd G:\persistence-memory
  python -m pip install -e ".[dev]"
  ```
- [ ] Verify install succeeded — no red errors from `pip`.

### 1b. Environment file

- [ ] Copy `.env.example` to `.env`; **never commit `.env`**:
  ```powershell
  Copy-Item .env.example .env
  ```
- [ ] Open `.env` and set values for your deployment:
  | Variable | Required? | Notes |
  |----------|-----------|-------|
  | `JARVIS_HOST` | Optional | Defaults `0.0.0.0`; keep for bind-all |
  | `JARVIS_PORT` | Optional | Defaults `8001` |
  | `JARVIS_STORE_PATH` | Recommended | E.g. `data/jarvis-store.json` or absolute path on durable volume |
  | `JARVIS_ENV` | **Yes** | Set to `production` — disables uvicorn `--reload` |
  | `JARVIS_CORS_ORIGINS` | **Yes** | Set to actual origin(s), comma-separated; **not `*`** in production |
  | `JARVIS_API_KEY` | **Yes** (default) | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` — store out-of-band, never commit |
  | `JARVIS_ALLOW_UNAUTHENTICATED` | Local only | Set `1` only for trusted loopback when no key; never with published ports |

### 1c. Store path and permissions

- [ ] Ensure the directory pointed to by `JARVIS_STORE_PATH` exists and is writable:
  ```powershell
  New-Item -ItemType Directory -Force -Path (Split-Path $env:JARVIS_STORE_PATH)
  ```
- [ ] **Backup any existing store file** before first run (migration on load rewrites legacy rows):
  ```powershell
  Copy-Item data\jarvis-store.json data\jarvis-store.json.bak -ErrorAction SilentlyContinue
  ```

### 1d. Smoke-verify before service start

- [ ] Run tests once from the deploy directory to confirm environment:
  ```powershell
  python -m pytest -q   # all tests must pass
  ```

---

## 2. Reverse proxy / TLS

> Service itself has **no built-in TLS**. TLS termination is the operator's responsibility.
> See `SECURITY.md §5` for detailed guidance.

### 2a. Nginx example (Linux/WSL)

```nginx
server {
    listen 443 ssl http2;
    server_name memory.example.internal;

    ssl_certificate     /etc/ssl/certs/memory.crt;
    ssl_certificate_key /etc/ssl/private/memory.key;

    location / {
        proxy_pass         http://127.0.0.1:8001;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}
```

### 2b. Caddy (minimal — auto-TLS for public domains)

```caddy
memory.example.com {
    reverse_proxy localhost:8001
}
```

### 2c. CORS tightening

- [ ] Set `JARVIS_CORS_ORIGINS` to the actual client origin(s) — never leave `*` for any publicly-exposed host:
  ```
  JARVIS_CORS_ORIGINS=https://your-client.example.com,https://another-client.example.com
  ```
  The application reads this at startup and passes each origin to FastAPI's `CORSMiddleware`.

### 2d. Header forwarding

- [ ] If your proxy strips or rewrites `Authorization`, confirm the `X-API-Key` header is forwarded if you use that auth method instead.

---

## 3. Docker deploy

### 3a. Build image

```powershell
cd G:\persistence-memory
docker build -t persistence-memory:crew2 .
```

Expected output ends with: `Successfully built …` / `naming to docker.io/library/persistence-memory:crew2`.
The Dockerfile sets `JARVIS_ENV=production` by default — no reload in container.

### 3b. Compose up with durable volume

```powershell
# Inject API key from environment or a .env file (never embed in image):
$env:JARVIS_API_KEY = "your-key-here"   # or set in .env

docker compose up -d
```

`docker-compose.yml` mounts a named volume `ledger-data` to `/data` inside the container — this is the durable store path. **Do not use an ephemeral bind-mount unless you accept data loss on container restart.**

To use a host-directory bind-mount instead (e.g. for backup):

```yaml
volumes:
  - ./data:/data   # replace ledger-data: volume
```

### 3c. API key injection

- [ ] Set `JARVIS_API_KEY` via `env_file` (pointing to your `.env`) or as a Docker/compose secret:
  ```powershell
  docker compose --env-file .env up -d
  ```
  **Never** hardcode the key in `docker-compose.yml` or the `Dockerfile`.

### 3d. Healthcheck verification

After `docker compose up -d`, poll until healthy:

```powershell
Start-Sleep -Seconds 5
Invoke-RestMethod http://127.0.0.1:8001/health
# Expected: {"status":"ok","schema":"continuity-ledger-v1",...}
```

The Dockerfile `HEALTHCHECK` polls `/health` every 30 s; container status should read `healthy` after the `--start-period` (10 s).

### 3e. Restart policy

`docker-compose.yml` sets `restart: unless-stopped` — the container will restart automatically after host reboot unless manually stopped.

---

## 4. Post-deploy smoke

Run these checks after every fresh deploy or restart to confirm the round-trip is intact.

### 4a. Health endpoint

```powershell
$r = Invoke-RestMethod http://127.0.0.1:8001/health
if ($r.status -ne "ok") { throw "health failed" }
Write-Host "Health OK: schema=$($r.schema) memories=$($r.memory_count)"
```

Expected:
```json
{"status":"ok","schema":"continuity-ledger-v1","memory_count":N,"board_id":"board-...","memory_write_enabled":true}
```

### 4b. Create → retrieve round-trip

```powershell
$headers = @{ "X-API-Key" = $env:JARVIS_API_KEY }   # omit if key not set

# Create
$body = @{
  content    = "smoke test $(Get-Date -Format o)"
  source_agent = "deploy-smoke"
  session_id = "deploy-check"
  type       = "fact"
  confidence = 0.5
  status     = "draft"
  evidence   = @(@{ kind = "manual"; ref = "checklist-step-4b"; note = "post-deploy smoke" })
} | ConvertTo-Json -Depth 5

$created = Invoke-RestMethod -Uri http://127.0.0.1:8001/api/jarvis/memory `
  -Method POST -Headers $headers -Body $body -ContentType "application/json"
Write-Host "Created id=$($created.memory.id)"

# Retrieve
$live = Invoke-RestMethod "http://127.0.0.1:8001/api/jarvis/memory/retrieve?truth_scope=live&limit=5" `
  -Headers $headers
Write-Host "Retrieve: $($live.memories.Count) memories, $($live.selections.Count) selections"
```

### 4c. Automated smoke script

```powershell
$env:JARVIS_MEMORYBOARD_URL = "http://127.0.0.1:8001"
# If API key is set:
$env:JARVIS_API_KEY = "your-key"

scripts\smoke-test.ps1
# Expected final line: === ALL SMOKE CHECKS PASSED ===
```

### 4d. Board endpoint

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/jarvis/memory/board
# Returns: {"memory_board":{"board_id":"board-...","context":"","context_updated_at":null}}
```

---

## 5. Explicit non-claims (operator acknowledgment)

> These are **documented gaps — not failures**. The service is honest about its scope.
> Operator must acknowledge each before treating the deployment as production-grade.

| Non-claim | Detail | Where documented |
|-----------|--------|-----------------|
| **TLS** | Service binds HTTP only; TLS is operator-managed via reverse proxy | `SECURITY.md §5`; §2 above |
| **HA / replication** | Single atomic-write JSON file (`os.replace`); not replicated, not HA; **not multi-writer safe** | `docs/PLATFORM_LIMITS.md` |
| **Per-agent write slots** | Not built; board UI `slots` ≠ partitions. Conflicts are **cross-agent by subject** on one store | `docs/PLATFORM_LIMITS.md` §0 |
| **CCS root authority** | Clause V / Constitutional Continuity Service declared only; not enforced by this service | `docs/CONTINUITY_LEDGER_SOC.md` |
| **Mandala constitutional runtime** | No Cursor hook infra, no CCS engine; see `docs/RELATIONSHIP_TO_MANDALA.md` | `docs/RELATIONSHIP_TO_MANDALA.md` |
| **Multi-tenant / commercial** | Not started; no tenant isolation, billing, or signup flow | Scorecard "Commercial operations" |
| **Vector / similarity memory** | Deliberate non-goal — ledger filters only | `docs/CONTINUITY_LEDGER_SOC.md` |
| **JARVIS_ENV=production** | Disables uvicorn `--reload` only; does not enable HA, TLS, or Postgres | `app/__main__.py`, `Dockerfile` |

**Operator checklist before exposing to a network:**

- [ ] TLS termination configured via nginx/Caddy/LB (§2 above)
- [ ] `JARVIS_API_KEY` set to a strong random key (§1b above)
- [ ] `JARVIS_CORS_ORIGINS` restricted to actual origin(s) — not `*`
- [ ] `JARVIS_ENV=production` confirmed in running env
- [ ] Store path points to durable volume (§1c, §3b above)
- [ ] Non-claims above acknowledged

---

## References

- `SECURITY.md` — key management, CORS, TLS guidance
- `docs/scorecards/persistence-memory.md` — Drive-G-2 dimension table
- `docs/RELATIONSHIP_TO_MANDALA.md` — lineage boundary + Clause V note
- `docs/CI_LOCAL_VERIFY.md` — how to replicate CI test job locally
- `.env.example` — all supported environment variables
- `scripts/smoke-test.ps1` — automated smoke round-trip
