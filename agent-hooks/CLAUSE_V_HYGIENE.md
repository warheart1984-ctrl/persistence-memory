# Clause V hygiene — Continuity Ledger (persistence-memory)

> **Status:** **partial, enforced at the API for type and evidence.** Emotion, transient-state and transcript findings are
> **warn-only** until the operator reviews the warnings. Nothing else about Clause V is claimed.

## What Clause V means here

Continuity should store **evidence** (decisions, architecture notes with provenance), not:

- Chat / context dumps
- Emotion
- Transient session noise
- Ungoverned memory-as-SoT

Lineage reference (Mandala docs; not imported as runtime):  
`jarvis-memoryboard/docs/CONSTITUTIONAL_BOUNDARY_CLAUSE.md` § Clause V.

## What this service does today

| Surface | Behavior | Tag |
|---------|----------|-----|
| Create schema docstring | Encourages decisions/evidence over conversation dumps | **partial** (docs) |
| API accept types | `decision`, `architecture`, `research`, `fact` only. `preference`, `task` and `external_context` are refused (HTTP 422 `clause_v_violation`, reasons `clause_v_preference`, `clause_v_transient_state`, `clause_v_external_context`) | **enforced** (tests) |
| Evidence | A `decision` needs at least one evidence link (a `user-request` link counts). `fact`, `architecture` and `research` need a checkable link: kind `file`, `url`, `commit`, `test`, `receipt`, `command`, `document`, `doc`, `issue`, `pr` or `log`. Refused as `clause_v_evidence_required` | **enforced** (tests) |
| Verification | A record cannot become (or stay) `verified`, change `type`, change `evidence`, or leave `archived` unless it passes the two rules above, on every write path (REST, EMR tools, promote, pipeline) | **enforced** (tests) |
| Emotion, transient state, transcript dumps | Detected by narrow pattern lists; logged (`jarvis.clause_v`, codes and a content hash, never the content) and returned as `clause_v_warnings`. Nothing is refused until `JARVIS_CLAUSE_V_SOFT=enforce` | **warn only** (heuristic) |
| Hooks | The `sessionEnd` hook is **retired** (a no-op); `afterAgentResponse` is **retired** too (a no-op: no reply is cached anywhere); only `sessionStart` remains, and it only reads | **enforced** by removal (tests) |
| MCP write tools | The ledger MCP server's `write` tool stores `decision` only, with the user's own words as evidence, off unless `JARVIS_LEDGER_MCP_WRITE=1`; the EMR tools go through the API gate | **enforced** (tests) |
| Older records | Written before the gate, they stay readable and can be archived, deleted, tagged or edited as drafts; they cannot be verified until they conform | grandfathered |
| Smoke script | Prefers `type=decision` with evidence link | **partial** (operator path) |
| Silent merge | Conflicts surface; no auto-merge | **enforced** (tests) |

## Configuration

* `JARVIS_CLAUSE_V`: `enforce` (default) or `off`. Any other value means `enforce`; a typo cannot switch the gate off. The older test suite sets `off` in `tests/conftest.py`.
* `JARVIS_CLAUSE_V_SOFT`: `warn` (default), `enforce` or `off`, for the emotion / transient / transcript findings.

## Operator practice

1. Prefer `type=decision` (or `architecture` / `research`) with non-empty `evidence[]`.
2. Keep chat transcripts out of the ledger; summarize into decisions with refs.
3. Use `status=draft` for provisional notes; promote to `verified` only with evidence.
4. Do **not** claim more than the table above: type and evidence are enforced; emotion, transient state and transcripts are heuristic warnings.

## Explicit non-claim

This distribution does **not** implement CCS root authority. It enforces the Clause V type and evidence rules at the ledger API; it does not adjudicate domain truth. Continuity unifies evidence records; it does not adjudicate domain truth.
