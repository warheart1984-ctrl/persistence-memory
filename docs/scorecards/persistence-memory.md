# Maturity Scorecard — persistence-memory

**Drive-G-2 scorecard**  
**Canonical standard:** Drive-G Maturity Dimensions (Drive G ecosystem)

## Snapshot

| Field | Value |
|-------|-------|
| Project ID | `persistence-memory` |
| Repository path | `G:\persistence-memory` |
| Review date | `2026-08-01` |
| Reviewer | MRS crew + operator review (auth default + four constraints) |
| Evidence anchor | branch `crew/prod-readiness-2026-07` + pytest + Docker image `persistence-memory:crew2` |

## Dimension ratings

| Dimension | Rating | One-line justification |
|-----------|--------|------------------------|
| Constitutional model | Moderate | Continuity Ledger schema + SoC docs; CCS / Clause V **declared/partial** only |
| Governance methodology | Moderate | Scorecard + Mandala CECP trails; no runtime CES gates |
| Reference implementation | Moderate | FastAPI + acceptance/API/auth tests; cross-agent conflict by subject |
| Platform engineering | Moderate | CI, Docker, **API key required by default**; single JSON file — **not HA / not multi-writer safe** |
| Commercial operations | Not started | No signup, billing, tenant isolation, SLAs |

## Evidence by dimension

### Constitutional model
- **Claims:** Ledger fields + no silent merge; Clause V hygiene guidance
- **Evidence:** `app/models.py`, `docs/CONTINUITY_LEDGER_SOC.md`, `docs/CLAUSE_V_HYGIENE.md`, conflict tests
- **Gaps:** CCS Continuity Blocks / Clause V API enforcement **not enforced**

### Governance methodology
- **Claims:** Honest maturity tags; Drive-G-1 wording in README
- **Evidence:** this scorecard; trails `persistence-memory-prod-2026-07`, `persistence-memory-crew2-modes-2026-07`; `docs/PLATFORM_LIMITS.md`
- **Gaps:** No promotion receipt automation in this repo

### Reference implementation
- **Claims:** Continuity / Replay / Conflict acceptance **enforced**; conflicts are **cross-agent by subject** on one shared store
- **Evidence:** `tests/test_acceptance.py` (agent-a vs agent-b); `app/continuity.py::detect_conflicts`; `python -m pytest -q`
- **Gaps:** Drift multi-day semantic protocol operator-owned (**partial**); no embedding/similarity retrieval (**deliberate**)

### Platform engineering
- **Claims:** CI, Dockerfile/compose, auth required-by-default (`JARVIS_ALLOW_UNAUTHENTICATED` opt-out), atomic saves
- **Evidence:** `.github/workflows/ci.yml`, `Dockerfile`, `app/auth.py`, `app/store.py`, `SECURITY.md`
- **Gaps:** No Postgres/HA, no built-in TLS, **single-file single-writer** (atomic ≠ multi-writer); board `slots` are UI only — not write partitions

### Commercial operations
- **Claims:** none
- **Evidence:** n/a
- **Gaps:** entire commercial surface

## Audience readiness

| Audience | Assessment | Notes |
|----------|------------|-------|
| Operators (deploy & run) | Partial | Viable for careful local/Docker deploy with API key + checklist |
| Users (signup & self-serve) | Not ready | No product UX / tenancy |

## Conflict detection honesty

| Scope | Status |
|-------|--------|
| Within one `source_agent` on same `subject` | **enforced** |
| Across agents/sessions on same `subject` | **enforced** (shared store; no agent filter) |
| Per-agent write slots / partitioned logs | **not built** (board UI slots ≠ partitions) |
| Multi-writer concurrent safety | **not enforced** (platform gap) |

## Overall framing

> **This project is** a Moderate Continuity Ledger reference service **at the constitutional/reference layer**, and Moderate **at the platform layer**, with commercial operations **not started**. It is **not** a bare claim of “production ready” across all dimensions.

## Explicit non-claims

- Not CCS root authority
- Not Clause V API-enforced
- Not multi-tenant SaaS
- Not Mandala constitutional runtime
- Not HA durable database-backed ledger
- Not multi-writer safe / not HA
- Not associative/vector memory
- Not per-agent isolated ledgers (conflicts are global-by-subject)
