# The call log ("call witness")

The ledger's server writes down every tool call it receives, so the evidence that an agent called a tool comes from the **server**,
not from the agent's own report. It is a separate, append-only, hash-chained file. It is **not** a ledger record: it imports nothing
from the store, so logging can never change the ledger's state root, history or receipts.

## What is witnessed

| Path | `transport` | Notes |
|---|---|---|
| `POST /api/jarvis/tools/*` (all nine tool routes) | `http-tool` | |
| the same routes called by an MCP stdio proxy (`mcp_server/emr_stdio.py`, `ledger_stdio.py`) | `mcp-stdio` | only when the proxy sends `X-Jarvis-MCP-Transport: stdio` (self-reported) |
| `tools/call` on `POST /mcp` | `mcp-http` | one entry per call, including every call in a batch, unknown tools and denied calls |
| every **non-GET** call under `/api/jarvis/` (`POST`/`PATCH`/`DELETE` on `/memory`, `/blocks/seal`, `/replay/receipts`, ...) | `http-api` | `tool` is `"METHOD /route/{id}"`, `target` is the record id in the path |

Denied (`401`/`403`) and failed calls are logged too, including requests the API-key check rejects before the route runs.

**Not witnessed (v1):** `GET` reads of `/api/jarvis/memory/*` (high volume: hooks, soak; tracked in the follow-up issue), the log's own
endpoints, `/ready` and `/health`. The general memory API (`/api/jarvis/memory`) is the live ledger's real write path (MCP writes are
off on live), which is why its writes are in scope.

## Entry

`seq`, `ts` (UTC), `prev_hash`, `entry_hash` (sha256 of the canonical JSON of the entry without this field), `transport`,
`client_name`, `client_version`, `client_self_reported: true`, `tenant`, `tool`, `route`, `method`, `target`, `args_sha256`, `outcome`
(`ok` | `denied` | `error` | `denied_suppressed` | `gap`), `error_code`, `status_code`, `result_digest` (when the tool returned one),
`duration_ms`.

* **Never stored:** raw arguments, API keys, headers, tokens. `args_sha256` is over the canonical JSON (sorted keys, compact), so key
  order does not matter. Control characters are stripped from the self-reported client string and it is cut to 128 characters.
* **Client name and version are self-reported** (MCP `clientInfo`, `X-Jarvis-MCP-Client`, or the User-Agent). The server vouches that a
  call happened, when, with what argument hash and what result; it does not vouch for who the client really is.
* **`tenant`** is `operator` for the API key, or the opaque tenant key (a hash, never the OAuth subject) for an OAuth principal.
* The entry is written **before** the response is sent. HTTP responses carry `X-Jarvis-Call-Seq: <seq>` so a caller can find its call.

## Storage, rotation, retention

* Daily files `calls-YYYYMMDD.jsonl` (UTC) in `JARVIS_CALL_LOG_DIR` (default: `call-log/` next to `JARVIS_STORE_PATH`, so `/data/call-log`
  in the mint deployment, which is in the appdata volume and therefore in backups). Files are mode 600, the directory 700.
* One hash chain across files: each file's first entry carries the previous file's final hash and `seq` continues.
* `JARVIS_CALL_LOG_RETAIN_DAYS` (default 90) deletes whole old files. The last deleted entry's `seq` and hash are kept in `anchor.json`,
  so `verify` still checks the chain from that anchor.
* One writer at a time: an in-process lock plus an `fcntl` file lock (`msvcrt` on Windows), held across "read the head, assign seq,
  write, fsync", so `seq` and `prev_hash` stay correct across threads and processes.
* A flood of denied calls is capped (`JARVIS_CALL_LOG_DENIED_CAP_PER_MIN`, default 60 per process per minute); the rest are rolled into
  one `denied_suppressed` entry with the count and the time window.

## If the log cannot be written

* **Ledger writes fail closed.** `emr_remember`, `emr_upsert`, `POST /api/jarvis/memory` and `PATCH`/`DELETE /api/jarvis/memory/{id}` run
  a preflight first (lock, read the head, directory writable, **today's log file writable**, 1 MiB free). If it fails the call is refused with **503
  `CALL_LOG_UNAVAILABLE`** and nothing is written. Other non-GET calls (seal, receipts) are logged but fail open, so an unwritable
  log cannot stop the hourly seal.
* **Reads fail open.** The call is served, the failure is logged at ERROR, the log is flagged degraded (`DEGRADED` file, and
  `X-Jarvis-Call-Log: degraded` on the response).
* **A write that committed but could not be logged** returns its real result (it cannot be undone, and the ledger's own history records
  it), flags the log degraded, and further writes are refused until the log can be written again.
* On recovery a `gap` entry records how many calls were served but not logged (`count`), how many ledger writes were refused
  because the log was unavailable (`refused`), and since when, so `verify` shows the hole. The shared record is the `DEGRADED` file
  (updated under the log's lock, so several workers add to one count and write one gap). The outage is kept **in memory only while it
  could not be written there**, because that file lives in the directory that may be the thing that cannot be written; if the process
  itself dies during such an outage, that memory is lost with it.

## Endpoints (operator only; an OAuth tenant gets `403` `AUTHORITY_DENIED`)

* `GET /api/jarvis/tools/calls?tool=&client=&since=&limit=1..200&cursor=` newest first; `next_cursor` for the next page. The response
  says `provenance: server-witnessed` and `client_names: self-reported`.
* `GET /api/jarvis/tools/calls/verify` walks the chain across files and returns `ok`, `problems`, the `head` (`seq`, `entry_hash`),
  `anchor`, `degraded` and `gaps_recorded`. Edited, deleted, inserted and reordered lines all fail it.

## Configuration

| Variable | Default | |
|---|---|---|
| `JARVIS_CALL_LOG_ENABLED` | on in dev and test; **off when `JARVIS_ENV=production`** | the live deploy turns it on explicitly |
| `JARVIS_CALL_LOG_DIR` | `<dir of JARVIS_STORE_PATH>/call-log` | |
| `JARVIS_CALL_LOG_RETAIN_DAYS` | `90` | |
| `JARVIS_CALL_LOG_DENIED_CAP_PER_MIN` | `60` | |

## Honest limits

* Anyone who can **write the directory** can rebuild an entire chain, and trailing entries can be **truncated** without breaking it. Mitigation in
  v1: `verify` returns the head hash, so record it somewhere else (the relay evidence report does, and it should be copied into a ledger
  record the next time one is written). Adding the head to the backup anchors is a follow-up.
* The log proves the **server** received a call. It cannot prove which agent or person was behind a client string.
* The denied-flood cap is per process.
