# Clause V hygiene — Continuity Ledger (persistence-memory)

> **Status:** **declared / partial** — guidance + operator practice. **Not API-enforced.**

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
| API accept types | Still accepts `fact`, `preference`, `task`, etc. | **Not enforced** |
| Smoke script | Prefers `type=decision` with evidence link | **partial** (operator path) |
| Silent merge | Conflicts surface; no auto-merge | **enforced** (tests) |

## Operator practice

1. Prefer `type=decision` (or `architecture` / `research`) with non-empty `evidence[]`.
2. Keep chat transcripts out of the ledger; summarize into decisions with refs.
3. Use `status=draft` for provisional notes; promote to `verified` only with evidence.
4. Do **not** claim “Clause V enforced” in READMEs or marketing.

## Explicit non-claim

This distribution does **not** implement CCS root authority or constitutional write-path gates. Continuity unifies evidence records; it does not adjudicate domain truth.
