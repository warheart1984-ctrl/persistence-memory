# Continuity Ledger — Separation of Concerns

**Status:** Continuity Ledger subset **enforced** by tests in this package. Broader CCS layers remain **declared**.

## This package owns

- Persist board context + ledger records to **one shared** JSON store
- Retrieve with selection provenance (why / where / when / session) via **explicit filters** (id, type, status, session, subject, text query)
- Surface conflicts without silent merge — grouped by **`subject` across all `source_agent`s**
- Content hash (`content_sha256`) for drift checks (**partial** vs multi-day protocol)

## This package does not own

- Evidence Engine adjudication
- Knowledge / Understanding engines
- Constitutional Continuity Service (CCS) as multi-product authority
- Chat transcript archival (out of policy — store decisions/evidence)
- Embedding / vector / similarity retrieval (deliberate non-goal — this is a ledger)

## Shared continuity vs slots

- **Built:** single shared ledger (pattern **C**). Board `slots` are UI context only.
- **Conflict guarantee:** cross-agent by `subject` (not within-agent-only).
- **Not built:** per-agent write partitions. If added later, must stay pattern **A** (shared conflict read) — never isolated per-agent logs (**B**). See `docs/PLATFORM_LIMITS.md`.

## Non-goals

- Ranking confidence as truth
- Auto-resolving `supersedes` into deletion
- Multi-tenant SaaS isolation
- Recall-by-similarity / associative “memory”
- Claiming multi-writer safety from atomic file replace
