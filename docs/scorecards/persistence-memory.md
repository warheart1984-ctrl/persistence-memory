# Maturity Scorecard — persistence-memory

**Drive-G-2 scorecard**  
**Canonical standard:** Drive-G Maturity Dimensions (Drive G ecosystem)

## Snapshot

| Field | Value |
|-------|-------|
| Project ID | `persistence-memory` |
| Repository path | `G:\persistence-memory` |
| Review date | `2026-07-30` |
| Reviewer | MRS crew (persistence-memory-prod-2026-07) |
| Evidence anchor | local branch `crew/prod-readiness-2026-07` + `pytest` |

## Dimension ratings

| Dimension | Rating | One-line justification |
|-----------|--------|------------------------|
| Constitutional model | Moderate | Continuity Ledger schema + SoC docs; CCS declared only |
| Governance methodology | Moderate | Scorecard + CECP trail in Mandala; no runtime CES gates |
| Reference implementation | Moderate | End-to-end FastAPI + acceptance tests; not multi-host proven |
| Platform engineering | Moderate | CI, Docker, optional API key, atomic JSON; no HA/TLS/DB |
| Commercial operations | Not started | No signup, billing, tenant isolation, SLAs |

## Evidence by dimension

### Constitutional model
- **Claims:** Ledger fields + no silent merge policy documented
- **Evidence:** `app/models.py`, `docs/CONTINUITY_LEDGER_SOC.md`, conflict tests
- **Gaps:** CCS Continuity Blocks / signed evidence **declared** elsewhere only

### Governance methodology
- **Claims:** Honest maturity tags; Drive-G-1 wording in README
- **Evidence:** this scorecard; Mandala trail `persistence-memory-prod-2026-07`
- **Gaps:** No promotion receipt automation in this repo

### Reference implementation
- **Claims:** Continuity / Replay / Conflict acceptance **enforced**
- **Evidence:** `tests/test_acceptance.py`, `python -m pytest -q`
- **Gaps:** Drift multi-day protocol operator-owned (**partial**)

### Platform engineering
- **Claims:** CI workflow, Dockerfile/compose, optional API key, atomic saves
- **Evidence:** `.github/workflows/ci.yml`, `Dockerfile`, `app/auth.py`, `app/store.py`
- **Gaps:** No Postgres/HA, no built-in TLS, single-file store

### Commercial operations
- **Claims:** none
- **Evidence:** n/a
- **Gaps:** entire commercial surface

## Audience readiness

| Audience | Assessment | Notes |
|----------|------------|-------|
| Operators (deploy & run) | Partial | Viable for careful local/Docker deploy with API key |
| Users (signup & self-serve) | Not ready | No product UX / tenancy |

## Overall framing

> **This project is** a Moderate Continuity Ledger reference service **at the constitutional/reference layer**, and Moderate-early **at the platform layer**, with commercial operations **not started**. It is **not** a bare claim of “production ready” across all dimensions.

## Explicit non-claims

- Not CCS root authority
- Not multi-tenant SaaS
- Not Mandala constitutional runtime
- Not HA durable database-backed ledger
