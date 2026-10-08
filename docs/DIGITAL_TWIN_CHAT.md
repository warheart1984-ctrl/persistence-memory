# Digital Twin Chat

A governed chatbot surface over the Continuity Ledger. Each turn runs a
fixed pipeline — session window → optional HumanSignal → EMR recall →
prompt → backend → clause gate → receipt → optional draft persistence —
and every turn is receipted into a sha256-chained store. Chat is dark by
default like the rest of the twin surface.

## What it is (and is not)

- **Conversation is not memory.** Chat history lives in a process-local,
  TTL'd session window (`session.py`): not durable, not shared across
  replicas, never seeded from client input. Restart → `context_reset=true`
  on the next receipt; turn numbering continues from the receipt store.
- **The gate is an extractive grounding filter, not a fact-checker.** Every
  model-authored sentence must carry a `[memory_id]` citation resolving to
  the recalled bundle (`recalled[i]` pseudo-state) or it is dropped —
  `CITE_MISSING`, `ENTITY_UNSUPPORTED`, `UNSUPPORTED_TEXT`, `HEDGE_CLAUSE`, `NUMBER_MISMATCH`.
  It accepts source-text extracts and a finite set of render forms; arbitrary
  paraphrase cannot pass on vocabulary overlap alone. A citation still does
  not prove that the stored record itself is true.
- **Writes are extraction, not transcription.** Only user-attributed
  `decision` utterances become ledger proposals, always `status="draft"`,
  always carrying `turn-receipt:sha256:<digest>` evidence. A receipt proves
  the utterance occurred, not that its content is true — preferences and
  bare assertions are deliberately not persistable.
- **Two write identities.** User-stated claims land under `user:<tenant>`
  (they count toward twin coverage/state). Anything the twin itself
  produces stays under `ai-twin`, which `FilteredRecords` excludes from
  state — the twin cannot inflate its own evidence — while remaining
  recallable for conversation context.

## Endpoints

| Endpoint | Flag | Notes |
|---|---|---|
| `POST /api/jarvis/twin/chat` | `JARVIS_TWIN_ENABLED` + `JARVIS_TWIN_CHAT_ENABLED` | One governed turn |
| `GET /api/jarvis/twin/chat/receipts/{digest}` | same | Tenant-scoped; cross-tenant is indistinguishable from unknown (404) |
| `GET /api/jarvis/twin/chat/sessions/{session_id}/turns` | same | Ordered `{turn_index, receipt_digest}` list |

`persist=true` on a chat request additionally requires
`JARVIS_TWIN_CHAT_PERSIST_ENABLED` **and** the same write auth as
`POST /api/jarvis/memory`. The check runs before any model call — a
refused persist never spends a turn.

Error codes: `404` when dark · `403 TWIN_CHAT_PERSIST_DISABLED` ·
`409 SESSION_BUSY` (lease held; current-format stale leases recover after 120 s) ·
`503 RECEIPT_STORE_FULL`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `JARVIS_TWIN_CHAT_ENABLED` | off | Lights the three chat endpoints + UI pane |
| `JARVIS_TWIN_CHAT_PERSIST_ENABLED` | off | Allows `persist=true` to write drafts |
| `JARVIS_TWIN_CHAT_BACKEND` | `none` | `gateway`, `narrator:<provider>`, or `none` (template fallback) |
| `JARVIS_TWIN_CHAT_GATEWAY_URL` | — | llm-gateway base URL when backend is `gateway` |
| `JARVIS_TWIN_CHAT_GATEWAY_KEY_ENV` | — | Name of the env var holding the gateway bearer token (the token itself never appears in config or receipts) |
| `JARVIS_TWIN_CHAT_MODEL` | — | Optional model override passed to the gateway |
| `JARVIS_TWIN_CHAT_GATE` | `enforce` | `enforce`: raw model text never leaves the server; `shadow`: reply built from kept sentences only, receipt records what *would* be dropped; `off`: no model call at all, template only |
| `JARVIS_TWIN_CHAT_DIR` | `data/twin-chat` | SQLite receipt store location |
| `JARVIS_TWIN_CHAT_MAX_BYTES` | 64 MiB | Receipt-store cap; exhaustion → `503` |
| `JARVIS_TWIN_SIGNAL_ENABLED` | off | Optional HumanSignal pass on the inbound message |
| `JARVIS_TWIN_SIGNAL_URL` | — | HumanSignal service URL when enabled |

Session windows are bounded in code (`session.py`): 256 sessions, 60
turns each, 30 min TTL. Receipt chains survive process restarts; windows
do not — by design.

## Operator notes

- **Rate limiting.** `/chat` sits behind `_twin_guard` — anyone with
  twin-read access can spend model calls. When the backend is `gateway`,
  llm-gateway's per-tenant budgets absorb real spend, but **put a
  per-token or per-IP rate limit at the service edge** (reverse proxy or
  platform quota) before exposing chat beyond operators. `none` and
  `narrator:` backends have no external cost but still consume recall +
  gate CPU.
- **Receipt store.** One SQLite file per deployment at
  `JARVIS_TWIN_CHAT_DIR`; writes take `BEGIN IMMEDIATE` and sessions are
  leased. Two replicas pointed at one file will serialize on the file lock
  — run one chat replica per file, or keep chat on the same instance as
  the ledger. During an upgrade, owner-less leases from older workers are
  treated as busy because their monotonic deadlines cannot be compared with
  wall time safely. Let old workers finish and release them. If an old worker
  died, first stop all old workers and drain in-flight requests, then remove
  only the confirmed orphaned `(tenant_key, session_id)` row from the
  `leases` table; do not clear owner-less leases while old workers can still
  be serving turns.
- **Backups.** Receipts are evidence. Include `JARVIS_TWIN_CHAT_DIR` in
  whatever backup covers the ledger.
- **Persistence path.** Draft writes go through `store.create_memory`, so
  Clause V, evidence requirements, and conflict detection apply unchanged.
  Refusals surface as `persist_receipt.failures` on the response — the
  chat turn itself still succeeds and receipts.
- **HumanSignal.** Off by default; when on it annotates the inbound
  message (pressure/momentum/intent signals) for prompt shaping only.
  It never gates, never writes, and is never authoritative.

## UI

`/ui/twin` gains a chat pane when `JARVIS_TWIN_CHAT_ENABLED` is set —
per-turn receipts (backend, model, latency, drops), record-link
citations, proposed-claim breakdowns, a persist checkbox, and a
chat-is-dark state otherwise. Session id lives in `sessionStorage`, so a
refresh keeps the chain.

## Module map

```
app/twinchat/
  models.py    ChatRequest/ChatResponse/ChatTurnReceipt.v1/Turn
  session.py   tenant-bound in-memory windows (ephemeral by design)
  backends.py  ChatBackend protocol → GatewayBackend | NarratorBackend | NoneBackend
  prompt.py    persona contract + bounded prompt assembly
  gate.py      clause gate: [id] cites resolved through recalled[i] pseudo-state
  receipts.py  transactional SQLite store, hash chain, session leases
  extract.py   deterministic decision-proposal extraction (no model)
  persist.py   draft writes via store.create_memory + persist receipts
  signal.py    optional HumanSignal bridge (off by default)
  service.py   the 9-stage turn orchestrator
```

Tests: `tests/test_twinchat.py` — gate, receipts/chaining/leases,
extraction, Clause V shadow/enforce parity, tenant isolation,
fake-gateway E2E, fallback, persistence-disabled, HumanSignal toggles.
