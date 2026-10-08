# Jarvis Continuity Ledger

A governed, evidence-first memory ledger for AI agents, backed by PostgreSQL. It **preserves what was recorded**, with
provenance, and proves it was not quietly changed. It does **not** decide what is true (`docs/CONTINUITY_LEDGER_SOC.md`).
Continuity lives in ledger records, not in chat transcripts; the service itself is replaceable.

## What is live on 8011 (last deployed 2026-10-06; nothing has been deployed since)

One ledger runs in Docker on a private Linux box at **`127.0.0.1:8011`** (PostgreSQL row store, schema **v6**). Nothing is
reachable from the network; the PC and the agents reach it through an SSH tunnel. Deployment, operations and the rehearsed
restore are in [`deploy/mint/README.md`](deploy/mint/README.md). **This table describes the deployed build, not `main`**: `main` is ahead of the box
(schema v7 and the signature code, below).

| Capability | Status |
|---|---|
| **Postgres row store**: one row per record, forced row-level security, two roles (the app cannot change history), hash-chained `record_history` | **live** (`docs/POSTGRES.md`) |
| **Clause V** at the API: only `decision`, `architecture`, `research`, `fact`; every record needs evidence; nothing becomes `verified` without it. Emotion / transient-state / transcript heuristics only **warn** | **live**, type + evidence enforced (`docs/CLAUSE_V_HYGIENE.md`) |
| **Evidence Objects**: content-addressed (`eo:sha256:…`), immutable, hash only, operator key to create | **live**, **unsigned** (`docs/EVIDENCE_OBJECTS.md`) |
| **Continuity Blocks**: sealed, Merkle-rooted, hash-chained ranges of the history; every block hash is also kept outside the database in the backup anchors | **live**, **unsigned** (`docs/CONTINUITY_BLOCKS.md`) |
| **Replay Contracts, `RC.Ledger.v1`**: rebuild the ledger's state and ordered events as of any seq or sealed block, with a state root; receipts at sealed points (Evidence Objects); offline verifier `python -m app.replay verify`; `jarvisctl replay`; a restore-drill step | **live** since 2026-10-06: the `/api/jarvis/replay/*` routes, receipts at sealed points, the offline verifier, `jarvisctl replay` and the restore-drill step run on the box (`docs/REPLAY_CONTRACTS.md`). A receipt is an unsigned claim until it is re-derived |
| Domain Replay Contracts (`RC.AIKI`, `ARIS`, `SX`, `Lineage`, `Mandala`) | **on hold**: declared only (no schema, owner or algorithm exists) |
| **Signatures**: attestations of blocks, receipts and checkpoints by a key a root key authorized, a trust log, the host signer, the PC witness/cosign tool, signature levels L0/L1/L2 in replay verification, `JARVIS_SIGNATURES=off\|warn\|require` (schema v7, `docs/SIGNATURES.md`, `docs/SIGNING_RUNBOOK.md`) | **built on `main`, shelved, not deployed**: no key has been made and no key ceremony done, the sign timer is off, nothing is signed, and the live database is schema v6 (deploying `main` would migrate it to v7; the way back is the backup taken before) |
| CES registry, unified provenance chain, ESFR promotion | **not built** (`docs/CCS_CHARTER.md`) |

Also enforced by tests: continuity across sessions, replay of a retrieve with why / where / when / session, conflicts surfaced
and never silently merged (`tests/test_acceptance.py`). Drift checking and the AMUL / RAG parts are partial
(`docs/DRIFT_PROTOCOL.md`, `app/amul*.py`).

## Operating it: `jarvisctl`

On the box, from `deploy/mint/`: `bin/jarvisctl <command>` (`help` lists all; the table is the usual set).

| Command | Does |
|---|---|
| `up` | build and start db → migrate → app (this is how a schema upgrade is applied) |
| `verify [tenant]` | recompute the history chain and every sealed block; reports the unsealed tail |
| `smoke [--no-write]` | acceptance checks: exposure, hardening, roles, one write/history/delete round trip |
| `seal [--force\|--status]` | seal new history into blocks now (a block is made at 500 waiting entries or when the oldest is an hour old) |
| `backup` | take a verified backup set; its anchors (chain heads, counters, block hashes) must be a legitimate successor of the last or the dump is quarantined |
| `drill [--prove-detection]` | restore the newest set into a scratch database, verify it, and replay it at its last anchored block; with the flag it also proves tampering, a removed block and an altered entry are detected |
| `replay state\|receipt\|receipts\|check\|verify` | Replay Contracts: replay the ledger at a point, issue and re-derive receipts at sealed points |
| `attest status\|sign\|init-key\|install-roots\|verify` | the host-side signer and its key ceremony steps (needs schema v7; nothing is signed until a key is authorized by a root) |
| `offsite` | encrypt (age) and send the newest set to the Windows PC |
| `restore --yes-destroy-current-data` | **destructive**; the app stays down unless counts, anchors and hash chains all match |

Timers (systemd user units) run the hourly backup, daily offsite copy, weekly drill, 15-minute watchdog and 1-minute self-heal.
**`jarvis-seal.timer` (hourly at :55, five minutes before the backup) was enabled on 2026-10-06 after an explicit OK**; stop it with
`systemctl --user disable --now jarvis-seal.timer`. Upgrade and rollback rules (v5 code refuses v6 and the reverse) are in the
Mint README.

## Reaching it: tunnel and agents

* **SSH tunnel.** A scheduled task on the PC keeps `ssh -N -L 127.0.0.1:8011:127.0.0.1:8011` up with a dedicated key that the
  box restricts to forwarding that one port (no shell). Setup, key restriction, host-key pinning and undo are in
  `deploy/mint/README.md`.
* **Address and key.** `JARVIS_MEMORYBOARD_URL=http://127.0.0.1:8011` and `JARVIS_API_KEY_FILE=<file holding the operator key>`.
  There is **no default address**: the hooks and MCP servers refuse to run without it and never send the key anywhere else.
  Every `/api/jarvis/*` route needs the key (`X-API-Key` or `Authorization: Bearer`).
* **MCP.** `mcp_server/ledger_stdio.py` (`health`, `recall`, `get`; `write` only when `JARVIS_LEDGER_MCP_WRITE=1`, decisions only,
  user's own words required, credentials refused) and `python -m mcp_server` (EMR tools `emr_recall`, `emr_remember`, `emr_upsert`;
  the write tools are off on the live ledger, `JARVIS_MCP_WRITE_ENABLED=false`). Examples: `config/`, `docs/MCP_EMR_SETUP.md`.
* **Hooks.** Cursor's `sessionStart` hook only reads; `sessionEnd` and `afterAgentResponse` are retired no-ops (`agent-hooks/`).

## Records and API

Each record has `id`, `content`, `type`, `status` (`draft | verified | archived`), caller-asserted `confidence`, `evidence`
(`{kind, ref, note?}` links, including `evidence-object` links), `source_agent`, `session_id`, optional `subject` and `supersedes`
(a recorded claim, never a silent merge), timestamps, a database-owned `version` (optimistic locking, 409 on conflict) and
`content_sha256`. The API accepts `decision`, `architecture`, `research` and `fact`; a `preference`, `task` or `external_context`
write, or one without checkable evidence, is refused with `422 clause_v_violation` and the reasons. Older records stay readable.

| Route | |
|---|---|
| `GET /health`, `/ready` | liveness; readiness (database, schema version, roles) |
| `GET/POST/PATCH/DELETE /api/jarvis/memory[/{id}]`, `GET …/retrieve`, `…/conflicts`, `…/{id}/history`, `…/history/verify` | the ledger |
| `POST /api/jarvis/memory/pipeline` | EMR → STM → LTM consolidation, draft-only (needs `JARVIS_MCP_WRITE_ENABLED`) |
| `POST /api/jarvis/evidence`, `GET …/{id}`, `…/{id}/verify` | Evidence Objects (create: operator key only) |
| `POST /api/jarvis/blocks/seal`, `GET …/blocks`, `…/head`, `…/verify`, `…/{height}` | Continuity Blocks (operator key only) |
| `GET /api/jarvis/replay/contracts`, `…/state`, `…/events`, `POST …/receipts`, `GET …/receipts[/{id}[/verify]]` | `RC.Ledger.v1` and its receipts (operator key only; live) |

Blocks and replay answer 501 on the JSON store, which has no history.

## Develop and test

```bash
pip install -e ".[dev]"
python -m pytest -q                 # JSON-store suite; Postgres tests skip without a server
scripts/test-postgres.sh            # the full suite against a throwaway Postgres container
```

With your own throwaway server: `JARVIS_TEST_PG_DSN=postgresql://postgres:…@localhost:5432/postgres python -m pytest -q`, and
`JARVIS_TEST_BACKEND=postgres` runs the whole HTTP suite on the row store. For local poking only,
`JARVIS_STORE_BOOTSTRAP=1 uvicorn app.main:app --host 127.0.0.1 --port 8000` starts the **dev/test JSON file store**; without a
database URL or that opt-in the service answers 503 rather than creating a ledger. CI runs both suites on every PR.

## Honest limits

* **Nothing is signed (yet).** Evidence Objects, blocks and receipts prove what was recorded and that it was not altered,
  not who vouches for it. The signer (blocks and replay receipts) and the verification code are on `main`, shelved and not deployed, and no key exists. Once signing runs, a signature is the Mint key's word about a digest, not proof the content is true; `require` is off unless you turn it on. Authority is the recorded actor (the tenant key) plus the record's own `source_agent`.
* **Tamper evidence has an outside part.** Someone with full database control can rewrite history and re-seal every block; the
  database alone would pass. What exposes it is the anchors in the backups and in the encrypted offsite copies, so they matter.
  A block sealed after the last backup is not anchored yet; a receipt taken earlier also exposes a later rewrite.
* **Domain Replay Contracts (on hold) and CES schemas are not built.** Earlier docs claimed stubs under `schemas/rc/` and `schemas/ces/`;
  they never existed (corrected). Only `RC.Ledger.v1` has files (`schemas/rc/`). Receipts are unsigned claims until re-derived.
* **`main` is ahead of the box.** The live ledger is schema v6 and unsigned; the signature code (schema v7) is on `main` and shelved. Nothing on 8011 verifies or makes a signature.
* **Soft Clause V rules only warn** (emotion, transient state, transcripts) until the operator flips `JARVIS_CLAUSE_V_SOFT`.
* **One box.** Wi-Fi only, disk not encrypted, an hour's worth of data at risk between backups, no automatic security updates
  (`deploy/mint/README.md`, "Honest limits").
* The ledger records claims; it does not evaluate them.
* An AI Twin coverage index exists on `main` (`app/twin.py`, `docs/AI_TWIN.md`) — **off by default**
  (`JARVIS_TWIN_ENABLED`), read-only, and it measures ledger structure, not truth.
* A governed Twin Narrator also exists (`app/narrator/`, `docs/TWIN_NARRATOR.md`) — dark behind
  `JARVIS_TWIN_NARRATOR_ENABLED`; model text is gated clause-by-clause against TwinState and
  falls back to a deterministic template; narration writes nothing.

## Docs

`docs/CCS_CHARTER.md` and `docs/LEDGER_TO_CCS_MAPPING.md` (what is declared versus built) · `docs/CONSTITUTIONAL_BOUNDARY_CLAUSE.md` ·
`docs/CONTINUITY_LEDGER_SOC.md` · `docs/CONSTITUTIONAL_MEMORY_CONTRACT.md` · `docs/EMR_RECALL_PROTOCOL.md` · `docs/ADAPTER_CONSUMERS.md` ·
`docs/chaos/CL_CHAOS_100x.md` (the throwaway-stack hammer and its first report) · `docs/POSTGRES.md` · `docs/CLAUSE_V_HYGIENE.md` · `docs/EVIDENCE_OBJECTS.md` · `docs/CONTINUITY_BLOCKS.md` · `docs/REPLAY_CONTRACTS.md` · `docs/SIGNATURES.md` · `docs/SIGNING_RUNBOOK.md` ·
`SECURITY.md`.
