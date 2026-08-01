# persistence-memory

**Continuity Ledger** HTTP service for cross-session agent state.

- **Distribution name:** `persistence-memory` (this GitHub repo)
- **Service identity:** `jarvis-memoryboard` / schema `continuity-ledger-v1`
- **Default URL:** `http://127.0.0.1:8001`

This is a **ledger of evidence records**, not associative/vector “memory.” See [docs/PLATFORM_LIMITS.md](docs/PLATFORM_LIMITS.md) and [docs/RELATIONSHIP_TO_MANDALA.md](docs/RELATIONSHIP_TO_MANDALA.md). Maturity: [docs/scorecards/persistence-memory.md](docs/scorecards/persistence-memory.md).

## Maturity (evidence-bound)

| Capability | Status | Evidence |
|------------|--------|----------|
| Continuity (A→B→C same restore) | **enforced** | `tests/test_acceptance.py::TestContinuityAcceptance` |
| Replay (why/where/when/session) | **enforced** | `TestReplayAcceptance` + `/api/jarvis/memory/retrieve` |
| Conflict (no silent merge) | **enforced** (cross-agent by `subject`) | `TestConflictAcceptance`; groups all agents — not per-agent isolation |
| Drift (hash fidelity) | **partial** | hash check enforced; multi-day semantic protocol operator-owned |
| API key | **enforced** (required by default) | `tests/test_auth.py`; opt-out `JARVIS_ALLOW_UNAUTHENTICATED=1` |
| CCS / multi-product authority | **declared** | not implemented here |
| Multi-writer / HA store | **gap** | single JSON file; atomic write ≠ concurrent-safe |

### Drive-G-2 dimensions (summary)

| Dimension | Rating |
|-----------|--------|
| Constitutional model | Moderate |
| Governance methodology | Moderate |
| Reference implementation | Moderate (local vertical slice + acceptance tests) |
| Platform engineering | Moderate (CI + Docker + auth-by-default; JSON file store, no HA) |
| Commercial operations | Not started |

Full table: `docs/scorecards/persistence-memory.md`. Limits: `docs/PLATFORM_LIMITS.md`. Deploy: `docs/OPERATOR_DEPLOY_CHECKLIST.md`. Clause V: `docs/CLAUSE_V_HYGIENE.md` (**partial**).

## Quick start (local)

```powershell
python -m pip install -e ".[dev]"
# Local open auth (loopback only) — or set JARVIS_API_KEY instead:
$env:JARVIS_ALLOW_UNAUTHENTICATED = "1"
python -m app
# or
uvicorn app.main:app --host 127.0.0.1 --port 8001
python -m pytest -q
.\scripts\smoke-test.ps1
```

Docker (set a key; do not rely on opt-out when publishing ports):

```bash
export JARVIS_API_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
docker compose up --build -d
```

## API (high level)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness + schema (public) |
| GET/POST/PATCH | `/api/jarvis/memory/board` | Board context (UI slots ≠ write partitions) |
| GET/POST | `/api/jarvis/memory` | List / create ledger rows |
| GET | `/api/jarvis/memory/retrieve` | Explicit filters + selections + conflicts |
| GET | `/api/jarvis/memory/conflicts` | Conflict sets by **subject** (all `source_agent`s) |
| GET/PATCH/DELETE | `/api/jarvis/memory/{id}` | Row CRUD |

Create requires Continuity Ledger fields: `content`, `source_agent`, `session_id`, `type`, plus optional `confidence`, `evidence`, `status`, `subject`, `supersedes`, `tags`.

Retrieval is **filter/query/id** — not embedding similarity.

Legacy on-disk rows (`category` / `truth_status` / …) migrate on load.

## Auth

| Env | Role |
|-----|------|
| `JARVIS_API_KEY` | Required by default for protected routes |
| `JARVIS_ALLOW_UNAUTHENTICATED=1` | Explicit local-dev opt-out when no key |

Details: [SECURITY.md](SECURITY.md).

## Operator notes

- Prefer `JARVIS_API_KEY` over opt-out whenever the port may leave the machine.
- Tighten `JARVIS_CORS_ORIGINS` in shared networks.
- `JARVIS_ENV=production` disables uvicorn reload.
- Store path: `JARVIS_STORE_PATH` (atomic JSON writes; single-writer assumption).

## License

MIT — see [LICENSE](LICENSE).
