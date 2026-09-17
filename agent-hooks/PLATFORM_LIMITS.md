# Platform limits (accepted architectural constraints)

**Status:** known gaps — Moderate platform maturity; **not** HA.  
**Review:** operator acceptance 2026-08-01 (crew/prod-readiness follow-up).

These are intentional honesty bounds, not temporary blind spots.

## 0. Shared store + conflict scope (A / B / C)

**What was built: C — single shared store, no per-agent write slots** — with **cross-agent** conflict detection by `subject`.

| Pattern | Built? | Notes |
|---------|--------|-------|
| **A** Partitioned writes + shared conflict read | **No** | Declared only if/when write slots are added — must keep global-by-subject conflicts |
| **B** Fully isolated per-agent logs | **No** | Would break multi-tool conflict detection; not the design |
| **C** Single shared JSON ledger | **Yes** | One `_memories` map; all agents write the same store |

### Board `slots` ≠ write partitions

`MemoryBoard.slots` / `BoardSlot` are **UI/workspace context** fields only (`app/models.py`). They do **not** partition ledger writes or scope retrieval.

### What `/conflicts` actually compares

- Groups active (non-archived, non-superseded) rows by **`subject`**
- Does **not** filter by `source_agent` or `session_id`
- Codex vs Devin contradictory claims on the same `subject` **are** surfaced together
- Evidence: `app/continuity.py` `detect_conflicts`; `tests/test_acceptance.py::test_conflict_surfaces_both_no_merge` (agent-a vs agent-b)

**Honest guarantee:** conflict detection is **cross-agent by subject** on one shared store. Write contention across concurrent processes remains a platform gap (see §1).

If future write-slot partitioning is added to reduce contention, it must remain pattern **A** (shared conflict/retrieve across slots) — never **B**.

## 1. Single JSON file / single-writer

| Claim | Reality |
|-------|---------|
| Atomic writes (`os.replace`) | **Yes** — prevent torn/corrupt files on crash mid-write |
| Multi-writer safety | **No** — concurrent agent instances can lose updates (last writer wins) |
| HA / cluster | **No** — one process, one file store |

**Fit:** one agent / one machine (or carefully serialized writers). Concurrent writers are the **first thing that breaks**.

Do **not** claim multi-writer serialization, optimistic locking, or distributed consistency.

Evidence: `app/store.py` (`_save`), scorecard Platform engineering.

## 2. Drift = partial

| Layer | Status |
|-------|--------|
| `content_sha256` match | **enforced** — tampering / bit-rot / accidental rewrite fidelity |
| Multi-day semantic agreement (day-1 vs day-30) | **operator-owned protocol** — not system-verified |

Unattended long-horizon agents that need “still agree with yourself next month” must run an external schedule + incident process (`docs/DRIFT_PROTOCOL.md`). Hash equality is necessary but not sufficient for semantic continuity.

## 3. API key required by default

| Mode | How |
|------|-----|
| Default (secure) | Set `JARVIS_API_KEY`; clients send Bearer or `X-API-Key` |
| Local-dev opt-out | `JARVIS_ALLOW_UNAUTHENTICATED=1` (open routes; loopback only) |

See `SECURITY.md`, `app/auth.py`.

## 4. Ledger, not associative / vector memory

This service is a **Continuity Ledger**: explicit, queryable rows (id, filters, retrieve, conflicts).

It deliberately does **not** provide embedding/similarity search or “recall by meaning.”  
“Memory” in product language means **durable evidence records**, not associative retrieval.

Non-goal evidence: `docs/CONTINUITY_LEDGER_SOC.md`.
