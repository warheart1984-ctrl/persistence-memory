# PR body — Continuity Ledger v1 production readiness (crew2)

> Paste into GitHub PR when operator is ready to merge.
> Branch: `crew/prod-readiness-2026-07` → `main`

---

## PR: Continuity Ledger v1 — persistence-memory operator baseline

Branch: crew/prod-readiness-2026-07 → main

### What this changes
- Port Continuity Ledger models/store/continuity from Mandala jarvis-memoryboard
- Add: test suite (51 tests), CI (3.11+3.12 + docker build), Dockerfile/compose,
  optional API key middleware, atomic JSON store, legacy migration on load,
  SECURITY.md, Drive-G-2 scorecard, RELATIONSHIP_TO_MANDALA.md (drift table),
  OPERATOR_DEPLOY_CHECKLIST.md, CLAUSE_V_HYGIENE.md, smoke scripts

### Evidence (crew2, 2026-07-30)
- `python -m pytest -q` → **51 passed**
- `docker build -t persistence-memory:crew2 .` → **exit 0** (Docker Desktop 29.6.1)
- Docker smoke on `127.0.0.1:18001` via `scripts/smoke-test.ps1` → **exit 0**
- Maturity tags: enforced = tested; partial = drift / Clause V hygiene; declared = CCS

### Gaps remaining (known, non-blocking for operator baseline)
- TLS/reverse proxy: operator deploy (`SECURITY.md`, checklist)
- HA store: single JSON file; not HA
- Mandala `jarvis-memoryboard/` still lags atomic store / auth / prod reload (documented; sync optional)
- Commercial/multi-tenant: not started
- GitHub Actions green: requires operator push

### What this does NOT claim
- CCS root authority / Clause V API enforcement
- Mandala constitutional runtime
- “Production ready” across all Drive-G-2 dimensions — see scorecard
