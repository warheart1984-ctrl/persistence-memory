# Constitutional Continuity Service (CCS) — Charter

**Maturity of this document’s vision relative to runtime:** largely **declared / roadmap**.  
**What exists today:** Continuity Ledger v1 (`continuity-ledger-v1`) in this package with Continuity / Replay / Conflict **enforced** by tests; Drift **partial**.  

CCS, CES registration, unified provenance across AIKI/ARIS/SX/Lineage/Mandala, and ESFR promotion are **not** claimed implemented.

Reconcile with SoC (`CONTINUITY_LEDGER_SOC.md`): CCS/ledger **preserves continuity and enforces continuity invariants**. It does **not** become Evidence, Knowledge, or Understanding engines, and does **not** adjudicate epistemic truth.

**Constitutional Boundary Clause (declared):** *Continuity unifies evidence, not domains.* Full six clauses: `CONSTITUTIONAL_BOUNDARY_CLAUSE.md`. CCS is a substrate/bridge across AIKI, ARIS, Sovereign X, Lineage, and Mandala — not a merger of their semantics or authority models.

---

## 1. Constitutional Continuity Service (CCS)

### 1.1 Definition (**declared**)

CCS is the intended **root continuity authority** for Mandala Rendering Software ecosystems:

| Function | Intent | Status |
|----------|--------|--------|
| Record constitutional / continuity events | Append-only style history of what was claimed | **partial** via ledger POST |
| Store evidence objects | Typed, linked evidence | **partial** — content-addressed, immutable, hash-only Evidence Objects with a minimal local CES (`EVIDENCE_OBJECTS.md`); no signatures |
| Lineage / provenance | Who/when/session/source | **enforced** on ledger records |
| Deterministic replay | Same retrieve → same provenance envelope | **enforced** for ledger retrieve |
| Enforce continuity invariants | Required fields, no silent merge, hash fidelity helpers | **enforced** / **partial** (Drift multi-day) |

“Invariants” here = continuity/governance invariants (immutability of recorded hashes, required provenance, conflict surfacing), **not** “deciding what is true.”

### 1.2 Ledger structure (**declared** target)

| Construct | Meaning | Today |
|-----------|---------|-------|
| **Continuity Blocks** | Immutable batches / blocks of continuity events | **partial** — on the PostgreSQL row store (schema v6) contiguous ranges of the history are sealed into immutable, hash-chained blocks with an RFC 6962 Merkle root, verified by `pg_verify`, and every block hash is kept outside the database in the backup anchors; **not signed**, and the JSON store has no blocks (`CONTINUITY_BLOCKS.md`) |
| **Evidence Objects** | Typed, signed evidence payloads | **partial** — hash-only objects linked by `kind: evidence-object`; **not signed** (`EVIDENCE_OBJECTS.md`) |
| **Replay Contracts** | Registered reconstruction rules (RC.*) | **partial** — `RC.Ledger.v1` (the ledger's own state and events as of a history seq or sealed block) is implemented on the PostgreSQL row store; the five domain contracts are **declared** only (`REPLAY_CONTRACTS.md`) |
| **Provenance Chains** | Linked identity → intent → evidence → … → replay | **declared** — ledger has per-record provenance, not full chain |

---

## 2. Evidence Schema Registration (CES) — **declared**

Each subsystem **should** register a CES with CCS. **No CES schema files exist in this repository** (earlier versions of this charter said `schemas/ces/` held stubs; it never did). The only evidence schemas the ledger knows are the two local ones in `EVIDENCE_OBJECTS.md`.

| CES ID | Domain | Status |
|--------|--------|--------|
| `CES.AIKI.KO.v1` | Knowledge Objects | **declared** |
| `CES.ARIS.Decision.v1` | Governed Decisions | **declared** |
| `CES.SX.Execution.v1` | Execution Records | **declared** |
| `CES.Lineage.Identity.v1` | Identity & Provenance | **declared** |
| `CES.Mandala.Render.v1` | Rendering Evidence | **declared** |

Field lists for these are not written down anywhere yet: they await formal CES ownership sign-off and are not validated by the ledger API.

---

## 3. Replay Contract Registration (RC) — **partial**

| RC ID | Consumer | Status |
|-------|----------|--------|
| `RC.Ledger.v1` | The Continuity Ledger itself: state and ordered events as of a seq or sealed block | **implemented** (row store) — `schemas/rc/RC.Ledger.v1.*.schema.json`, `app/replay.py` |
| `RC.AIKI.v1` | AIKI reconstruction rules | **declared** |
| `RC.ARIS.v1` | ARIS reconstruction rules | **declared** |
| `RC.SX.v1` | Sovereign X / SX reconstruction | **declared** |
| `RC.Lineage.v1` | Lineage reconstruction | **declared** |
| `RC.Mandala.v1` | Mandala render/replay | **declared** |

Only `RC.Ledger.v1` has files under `schemas/rc/` (generated from the code and checked by a test). **The five domain contracts have no stubs, no owner and no algorithm**; their semantics live in other products and Clause III keeps their replay logic sovereign, so this service does not execute them. Earlier versions of this charter claimed stubs for them in `schemas/rc/`; there were none.

---

## 4. Unified Provenance Model — **declared**

Intended chain:

`Root Authority → Identity → Intent → Evidence → Decision → Execution → Verification → Replay`

- Shared identity domain via Lineage — **declared**
- Shared evidence semantics (signature / authority / hash / verification rules) — **declared**

Today’s ledger supplies: `source_agent`, `session_id`, `created_at`, `evidence[]`, `content_sha256`, `supersedes`, `status`, `confidence` (caller-asserted).

---

## 5. Integration Rules — **declared** (target)

| Rule | Intent | Today |
|------|--------|-------|
| Single write path | All continuity writes through CCS | **partial** — this API is a write path; not yet sole path across products |
| Single read path | Retrieve via CCS / ledger retrieve | **partial** — retrieve API exists; not universal |
| Deterministic replay across consumers | Same RC + evidence → same reconstruction | **declared** for multi-product; **enforced** within ledger retrieve tests |
| Constitutional boundaries | No emotion / transient / ungoverned memory as continuity SoT | **partial** — hooks prefer decisions; chat dumps discouraged, not fully banned at API |

---

## 6. Promotion Criteria (checklist)

Promotable toward “CCS as infrastructure” when:

| # | Criterion | Current |
|---|-----------|---------|
| P1 | All CES.* registered (schemas + owners) | **gap** — no CES schema files exist; only the two local evidence schemas |
| P2 | All RC.* registered | **gap** — `RC.Ledger.v1` is implemented; the five domain RCs are declared only (no schema, owner or algorithm) |
| P3 | Replay deterministic across registered consumers | **gap** — ledger-only enforced |
| P4 | Evidence chains validate (signatures / hashes end-to-end) | **gap** — hashes verify end-to-end for evidence objects (`pg_verify`); no signatures |
| P5 | Provenance unifies across AIKI/ARIS/SX/Lineage/Mandala | **gap** — declared model only |
| P6 | ESFR `PROMOTE_WITH_GAPS` or better for CCS milestone | **gap** — no CCS ESFR run recorded in this package |

---

## 7. Charter Outcome — **declared**

One constitutional continuity history across AIKI, ARIS, SX, Lineage, and Mandala — Continuity Ledger becomes **infrastructure, not mere storage**. Intended milestone; not present capability.

---

## Quote-ready SoC / CCS statement

The Continuity Ledger (and the declared Constitutional Continuity Service built around it) is continuity infrastructure: it records what was claimed, with required provenance, surfaces conflicts without merging, and supports deterministic replay of those records. It enforces continuity invariants — not epistemic truth. Evidence, Knowledge, and Understanding engines remain separate layers that may consume the ledger read-only and decide what to believe. Per the Constitutional Boundary Clause, continuity unifies evidence across domains without blending AIKI, ARIS, Sovereign X, Lineage, or Mandala into one semantics or authority model.
