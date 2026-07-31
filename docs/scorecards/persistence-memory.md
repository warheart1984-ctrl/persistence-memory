# Maturity Scorecard — persistence-memory

**Drive-G-2 scorecard**  
**Canonical standard:** Drive-G Maturity Dimensions (Drive G ecosystem)

## Snapshot

| Field | Value |
|-------|-------|
| Project ID | `persistence-memory` |
| Repository path | `G:\persistence-memory` |
| Review date | `2026-07-30` |
| Reviewer | MRS crew2 (`persistence-memory-crew2-modes-2026-07`) |
| Evidence anchor | branch `crew/prod-readiness-2026-07` + pytest + Docker image `persistence-memory:crew2` |

## Dimension ratings

| Dimension | Rating | One-line justification |
|-----------|--------|------------------------|
| Constitutional model | Moderate | Continuity Ledger schema + SoC docs; CCS / Clause V **declared/partial** only |
| Governance methodology | Moderate | Scorecard + Mandala CECP trails; no runtime CES gates |
| Reference implementation | Moderate | FastAPI + 51 acceptance/API/auth tests; live smoke exercised |
| Platform engineering | Moderate | CI, Docker build verified locally, optional API key, atomic JSON; no HA/TLS/DB |
| Commercial operations | Not started | No signup, billing, tenant isolation, SLAs |

## Evidence by dimension

### Constitutional model
- **Claims:** Ledger fields + no silent merge; Clause V hygiene guidance
- **Evidence:** `app/models.py`, `docs/CONTINUITY_LEDGER_SOC.md`, `docs/CLAUSE_V_HYGIENE.md`, conflict tests
- **Gaps:** CCS Continuity Blocks / Clause V API enforcement **not enforced**

### Governance methodology
- **Claims:** Honest maturity tags; Drive-G-1 wording in README
- **Evidence:** this scorecard; trails `persistence-memory-prod-2026-07`, `persistence-memory-crew2-modes-2026-07`
- **Gaps:** No promotion receipt automation in this repo

### Reference implementation
- **Claims:** Continuity / Replay / Conflict acceptance **enforced**
- **Evidence:** `tests/test_acceptance.py`, `python -m pytest -q` → 51 passed; `scripts/smoke-test.ps1`
- **Gaps:** Drift multi-day protocol operator-owned (**partial**)

### Platform engineering
- **Claims:** CI workflow, Dockerfile/compose, optional API key, atomic saves; Docker image build on this host
- **Evidence:** `.github/workflows/ci.yml`, `Dockerfile`, `docker build -t persistence-memory:crew2 .` (exit 0), `app/auth.py`, `app/store.py`
- **Gaps:** No Postgres/HA, no built-in TLS, single-file store; compose default image name needs `docker compose up --build` (or explicit image tag)

### Commercial operations
- **Claims:** none
- **Evidence:** n/a
- **Gaps:** entire commercial surface

## Audience readiness

| Audience | Assessment | Notes |
|----------|------------|-------|
| Operators (deploy & run) | Partial | Viable for careful local/Docker deploy with API key + checklist |
| Users (signup & self-serve) | Not ready | No product UX / tenancy |

## Overall framing

> **This project is** a Moderate Continuity Ledger reference service **at the constitutional/reference layer**, and Moderate **at the platform layer**, with commercial operations **not started**. It is **not** a bare claim of “production ready” across all dimensions.

## Explicit non-claims

- Not CCS root authority
- Not Clause V API-enforced
- Not multi-tenant SaaS
- Not Mandala constitutional runtime
- Not HA durable database-backed ledger
