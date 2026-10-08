# AI Twin over the Continuity Ledger

Status: partial — core proven by `tests/test_twin.py`; endpoint wired but
**dark by default** (`JARVIS_TWIN_ENABLED`). Not deployed; not run against
live data.

## What this is

A deterministic coverage index over a tenant's own `MemoryRecord`s. No
learned model: every output (index, brief, mission, explanation) is derived
from the same component values, so an explanation can never drift from the
number it explains.

```
tenant-scoped records → FilteredRecords (twin-authorship + validation gate)
        → component vector (V P L W S T C N) + verbatim conflict sets
        → coverage_index, brief, mission, explanations, twin_input_digest
        → optional write-back as an ordinary memory (ai-twin, research, draft)
```

## What this number is NOT

> **This index measures how well-structured and evidenced the ledger is.
> It does not say whether any memory is true.**

A high `coverage_index` means records are verified-*statused*, evidenced,
lineage-linked, diverse in type, temporally deep, conflict-tidy, and written
by several agents. It says nothing about correctness of content. A ledger of
elaborately evidenced falsehoods can score well. Consumers must not present
the number as a truth score.

## Components

| Component | Ledger field(s) | Weight |
|---|---|---|
| V verified | `status == "verified"` ratio | 0.20 |
| P provenance | ≥1 evidence link not resolving to a twin-authored id | 0.16 |
| L lineage | records in a `supersedes` chain with **both ends** in the filtered set | 0.12 |
| W weight | record count, saturates at 50 | 0.12 |
| S coverage | distinct `type`s, of the 7 declared | 0.12 |
| T temporal | `created_at` span (clamped to injected `now`), saturates at 90d | 0.10 |
| C conflict health | benign sets / all sets (1.0 when none; see mechanism below) | 0.10 |
| N participation | distinct non-twin `source_agent`s, saturates at 5 | 0.08 |

Mission = weakest component → one targeted action (`_MISSIONS` table).
Empty ledger: all components 0, mission is *"Write the first evidenced
memory."*

## Invariants — and the tests that prove them

| Invariant | Mechanism | Test |
|---|---|---|
| Fail closed on empty/invalid input | zeroed vector; invalid records counted in `skipped_records` | `test_empty_ledger_fails_closed`, `test_invalid_records_skipped_and_counted` |
| No self-grading | `FilteredRecords.from_records` is the only entry point; components refuse raw lists (`TypeError`) | `test_twin_records_of_every_kind_are_invisible`, `test_components_refuse_raw_list` |
| Twin supersedes can't game L/V | `supersedes` counts only when **both ends** survive filtering | `test_twin_supersede_of_human_does_not_change_l_or_v`, `test_human_supersede_of_twin_does_not_change_l` |
| Twin can't be cited as evidence | evidence refs resolving to twin ids don't count for P | `test_evidence_ref_to_twin_record_does_not_count_for_p` |
| Unknown status/type never counts | pydantic literal validation at the filter boundary | `test_unknown_status_or_type_never_counts` |
| Conflicts surfaced, never resolved | `detect_conflicts` over filtered set; verbatim subjects + record ids in output; inputs unmutated | `test_conflicts_quoted_verbatim_with_ids`, `test_inputs_not_mutated`, `test_twin_supersede_cannot_resolve_conflict` |
| C counts only non-twin resolutions | twin records don't exist for `detect_conflicts`, so a twin supersede can't collapse a set | `test_twin_supersede_cannot_resolve_conflict`, `test_human_supersede_resolves_conflict_for_c` |
| Determinism | sorted digest input; `now` injected; no wall-clock reads | `test_shuffle_determinism`, `test_now_is_injected_and_wall_clock_never_read` |
| Replayable input identity | `twin_input_digest` = SHA-256 of sorted `id|version|content_sha256` | `test_input_digest_stable_across_order_and_sensitive_to_content` |
| Endpoint dark by default | `JARVIS_TWIN_ENABLED` unset → 404 | `test_endpoint_disabled_returns_404` |
| Read-only by default | no store writes unless `persist=1` + second flag + write auth | `test_endpoint_read_only_writes_nothing`, `test_persist_refused_when_flag_off` |
| Persist is idempotent per day | `subject=twin:daily:{tenant}:{YYYY-MM-DD}` + `twin_digest:` tag | `test_persist_idempotent_same_digest_same_day`, `test_persist_different_digest_same_day_supersedes_today`, `test_persist_new_day_does_not_supersede` |

## Endpoint

`GET /api/jarvis/twin/daily` — dark until deployed.

- `JARVIS_TWIN_ENABLED` (default off). Off → **404**: a dark surface stays
  invisible rather than advertising a disabled feature.
- Read path: tenant-scoped records via `get_store()` only. When
  `JARVIS_PROTECT_LEDGER_READ` is on (non-OAuth mode), the operator-key check
  is mirrored on this route since it sits outside `LEDGER_READ_PREFIX`.
- `?persist=1` — gated three deep: `JARVIS_TWIN_ENABLED` **and**
  `JARVIS_TWIN_PERSIST_ENABLED` (off → **403 `TWIN_PERSIST_DISABLED`**) **and**
  `require_memory_write()` — the identical write gate as `POST /api/jarvis/memory`.
  The record is created through the normal `MemoryCreate` path: same
  validation, hashing, Clause V warnings, and RLS as any write.

### Persist semantics

- **Same digest, same day** → returns the existing `memory_id`; nothing written.
- **Different digest, same day** → new record, `supersedes` = today's earlier twin record.
- **First persist of a new day** → new record, no `supersedes` (day is in the subject).

## `twin_input_digest`

```python
lines = sorted(f"{r.id}|{r.version}|{r.content_sha256 or content_sha256(r.content)}"
               for r in filtered.records)
digest = sha256("\n".join(lines))
```

`content_sha256` falls back to the canonical hash when the field is empty
(same rule as `detect_conflicts`). `row_hash` isn't on the `MemoryRecord`
wire — it lives in pg `record_history` — so the digest binds `content_sha256`
today; upgrading to `row_hash` when exposed is a documented future change.

## Proven

Everything in the invariants table above, on `tests/test_twin.py`
(31 tests + 1 postgres-marked skip). Plus: the endpoint writes through the
real `MemoryCreate` path (`test_persist_writes_one_record_through_memory_create`).

## Not proven

- The index says nothing about whether any memory is **true**.
- `is_twin_authored` trusts `source_agent` — caller-supplied free text. Any
  caller with write access can spoof it. Verified writer identity (Wicket
  witness / principal stamping) is a later step.
- Two-tenant isolation is proven at the API seam: `test_two_tenants_isolated`
  runs OAuth-mode per-tenant stores and verifies A's brief never includes B's
  records (and digests differ). On the Postgres backend the same test runs
  through `PostgresRowStore`/RLS when `JARVIS_TEST_BACKEND=postgres`.
- Persist is not yet behind a witness-signed caller identity.
- Never run against live data or the 8011 service.
- Weights are unvalidated heuristics; `explanations.payload` exposes them so
  consumers can audit rather than trust.
- `C` can only observe conflict sets that still exist — a fully resolved set
  disappears, so C measures *remaining* disagreement, not resolution history.
