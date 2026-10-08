# AI Twin over the Continuity Ledger

Status: declared — sketch for the next deploy. Core is pure and testable today
(`app/twin.py`); wiring lands when the new ledger deploy is ready.

## Concept (extracted from paragon-one)

A digital twin is not a learned model. It is a **deterministic self-model over
the tenant's own ledger**: score the evidence graph, project the weakest
dimension, emit the smallest actionable step, explain every number from the
same values that produced it.

```
tenant-scoped MemoryRecords + ConflictSets
        → component vector (V P L W S T C N)
        → score, brief, mission, explanations
        → written back as a ledger memory (ai-twin, research, draft)
```

Explanations cannot drift from decisions because they *are* the decision,
narrated. An LLM may sit on top later to render the packet fluently — it
narrates the numbers; it never produces them.

## Component mapping

| Component | Paragon source | Ledger field(s) | Weight |
|---|---|---|---|
| V verified | verified evidence ratio | `status == "verified"` | 0.20 |
| P provenance | provenance chain present | `evidence[]` non-empty | 0.16 |
| L lineage | lineage chain | `supersedes` either direction | 0.12 |
| W weight | evidence count | total records, saturates at 50 | 0.12 |
| S coverage | skills | distinct `type`s, of 7 | 0.12 |
| T temporal | temporal chain | span of `created_at`, sat. 90d | 0.10 |
| C conflict | — (paragon had none) | resolved / total conflict sets | 0.10 |
| N participation | governance logs | distinct `source_agent`s, sat. 5 | 0.08 |

Mission = weakest component → one targeted action (`_MISSIONS` table).

## Invariants

- **Sovereignty**: the twin scores only the records the tenant-scoped store
  hands it. No cross-tenant view exists at this layer.
- **Anti-self-dealing**: `source_agent="ai-twin"` records are excluded from
  all component inputs. The twin cannot raise its own standing by generating
  (paragon's `GOVERNANCE_PARTICIPATION_CATEGORIES` idea, generalized).
- **No silent merge**: unresolved `ConflictSet`s surface verbatim with
  `policy_hint`. The twin reports disagreement; it never adjudicates.
- **Fail closed**: an empty ledger scores 0 and the mission is "produce
  evidence". No fabricated narrative.
- **Self-auditing**: each generation can be persisted via
  `twin_memory_payload()` through the *normal* write path — the twin holds no
  privileged write channel, and its output gains full block-chain lineage.

## Wiring sketch (next deploy)

```python
store = get_store()                      # or PostgresJarvisStore
records = list(store._memories.values()) # already tenant-scoped at the API
conflicts = [s.model_dump() for s in list_conflict_sets(records)]
packet = generate_twin_intelligence(records, conflicts, identity_id=tenant)
# optional persistence through the ordinary authenticated path:
store.create_memory(MemoryCreate(**twin_memory_payload(packet, session_id)))
```

Candidate endpoint: `GET /api/jarvis/twin/daily` — read-mostly; the brief
write is opt-in (`?persist=1`) and requires `require_memory_write`.

## Known gaps

- Stateless per call — each generation recomputes. Reputation *trend*
  (paragon's projected-6m) needs either history snapshots or reading prior
  `twin-brief` memories back as the series. The latter is free once briefs
  persist: the ledger is the time series.
- Conflict resolution today is manual — a future adjudication engine would
  consume `conflicts.subjects` plus evidence links, with DebtItem-style
  `user_confirmed` gating before any supersede.
- `S` measures type coverage, not skill coverage — a profile/skills surface
  doesn't exist on `MemoryRecord`; could key off `tags` vocabulary later.
- Weights are unvalidated heuristics. Exposed in `explanations.payload` so a
  consumer can audit (or re-weight) without trusting them.
