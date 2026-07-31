# Continuity Ledger — Separation of Concerns

**Status:** Continuity Ledger subset **enforced** by tests in this package. Broader CCS layers remain **declared**.

## This package owns

- Persist board context + ledger records to JSON store
- Retrieve with selection provenance (why / where / when / session)
- Surface conflicts without silent merge
- Content hash (`content_sha256`) for drift checks (**partial** vs multi-day protocol)

## This package does not own

- Evidence Engine adjudication
- Knowledge / Understanding engines
- Constitutional Continuity Service (CCS) as multi-product authority
- Chat transcript archival (out of policy — store decisions/evidence)

## Non-goals

- Ranking confidence as truth
- Auto-resolving `supersedes` into deletion
- Multi-tenant SaaS isolation
