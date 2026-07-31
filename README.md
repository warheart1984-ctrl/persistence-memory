# persistence-memory

**Continuity Ledger** HTTP service for cross-session agent state.

- **Distribution name:** `persistence-memory` (this GitHub repo)
- **Service identity:** `jarvis-memoryboard` / schema `continuity-ledger-v1`
- **Default URL:** `http://127.0.0.1:8001`

This is **not** a claim of full CCS (Constitutional Continuity Service) readiness. See [docs/RELATIONSHIP_TO_MANDALA.md](docs/RELATIONSHIP_TO_MANDALA.md) and the [maturity scorecard](docs/scorecards/persistence-memory.md).

## Maturity (evidence-bound)

| Capability | Status | Evidence |
|------------|--------|----------|
| Continuity (A→B→C same restore) | **enforced** | `tests/test_acceptance.py::TestContinuityAcceptance` |
| Replay (why/where/when/session) | **enforced** | `TestReplayAcceptance` + `/api/jarvis/memory/retrieve` |
| Conflict (no silent merge) | **enforced** | `TestConflictAcceptance` + `/api/jarvis/memory/conflicts` |
| Drift (hash fidelity) | **partial** | hash check enforced; multi-day protocol operator-owned |
| Optional API key | **enforced** | `tests/test_auth.py` when `JARVIS_API_KEY` set |
| CCS / multi-product authority | **declared** | not implemented here |

### Drive-G-2 dimensions (summary)

| Dimension | Rating |
|-----------|--------|
| Constitutional model | Moderate |
| Governance methodology | Moderate |
| Reference implementation | Moderate (local vertical slice + acceptance tests) |
| Platform engineering | Moderate (CI + Docker + optional auth; JSON file store, no HA) |
| Commercial operations | Not started |

Full table: `docs/scorecards/persistence-memory.md`. Operator deploy: `docs/OPERATOR_DEPLOY_CHECKLIST.md`. Clause V hygiene: `docs/CLAUSE_V_HYGIENE.md` (**partial** / not API-enforced).

## Quick start

```powershell
python -m pip install -e ".[dev]"
python -m app
# or
uvicorn app.main:app --host 127.0.0.1 --port 8001
python -m pytest -q
.\scripts\smoke-test.ps1
```

Docker:

```bash
docker compose up --build -d
```

## API (high level)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness + schema |
| GET/POST/PATCH | `/api/jarvis/memory/board` | Board context |
| GET/POST | `/api/jarvis/memory` | List / create ledger rows |
| GET | `/api/jarvis/memory/retrieve` | Memories + selections + conflicts |
| GET | `/api/jarvis/memory/conflicts` | Conflict sets by subject |
| GET/PATCH/DELETE | `/api/jarvis/memory/{id}` | Row CRUD |

Create requires Continuity Ledger fields: `content`, `source_agent`, `session_id`, `type`, plus optional `confidence`, `evidence`, `status`, `subject`, `supersedes`, `tags`.

Legacy on-disk rows (`category` / `truth_status` / …) migrate on load.

## Operator notes

- Set `JARVIS_API_KEY` when exposing beyond loopback ([SECURITY.md](SECURITY.md)).
- Tighten `JARVIS_CORS_ORIGINS` in shared networks.
- `JARVIS_ENV=production` disables uvicorn reload.
- Store path: `JARVIS_STORE_PATH` (atomic JSON writes).

## License

MIT — see [LICENSE](LICENSE).
