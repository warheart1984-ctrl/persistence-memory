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

| every **GET** read of `/api/jarvis/memory` and `/api/jarvis/memory/*` (`/{id}`, `/latest`, `/search`, `/retrieve`, `/board`) | `http-api`, or `mcp-stdio` from a stdio adapter (Grok's `ledger_stdio.py` sends the stdio header and its client name) | `tool` is `"GET /route/{id}"`; the query is hashed like any argument, never stored |

Denied (`401`/`403`) and failed calls are logged too, including requests the API-key check rejects before the route runs.

Reads are high volume (the sessionStart hook, `ping_memoryboard.py`, soak runs), so they are **capped, never dropped silently**: up to
`JARVIS_CALL_LOG_READ_CAP_PER_MIN` (default 600, per process per minute) are written one by one; the rest of that minute is rolled into one
`read_suppressed` entry (`tool: "(read calls)"`, `count`, `window_start`, `window_end`). A write (or the next read in a new minute) closes the
open summary; other calls, such as a read-only tool, do not, so mixed traffic cannot write one summary per read. The order of the log shows how many reads came before a write. `JARVIS_CALL_LOG_READS=0` turns read witnessing off (writes and
tool calls stay witnessed). A read served while the log cannot be written is counted in the recovery `gap` entry like any other served call. An open summary is written when the app shuts down gracefully; it is kept in memory until then, so a hard crash can lose the count of the reads suppressed in the last minute (the individual entries before the cap are already on disk). A summary whose append fails keeps its count and is written by the next flush. Memory routes are templated by position from the route table: a path parameter (anything in an `{id}` slot) is replaced by `{id}` and its value is recorded as `target` only if it is a record id, and a path that matches no route is logged as `.../{other}`, so a typed-in value never reaches the log; a query key given twice keeps both values, in order, in the argument hash.

**Not witnessed:** GET reads outside `/api/jarvis/memory` (`/replay/*`, `/blocks`, the nx routes), the log's own
endpoints, `/ready` and `/health`. The general memory API (`/api/jarvis/memory`) is the live ledger's real write path (MCP writes are
off on live), which is why its writes are in scope.

## Entry

`seq`, `ts` (UTC), `prev_hash`, `entry_hash` (sha256 of the canonical JSON of the entry without this field), `transport`,
`client_name`, `client_version`, `client_self_reported: true`, `tenant`, `tool`, `route`, `method`, `target`, `args_sha256`, `outcome`
(`ok` | `denied` | `error` | `denied_suppressed` | `read_suppressed` | `gap`), `error_code`, `status_code`, `result_digest` (when the tool returned one),
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
  (updated under the log's lock, so several workers add to one count and write one gap). The increments a process could not write there are kept **in memory** (and added to the file's count, never max'd,
  so concurrent workers and threads do not lose each other's counts), because that file lives in the directory that may be the thing that cannot be written; if the process
  itself dies during such an outage, that memory is lost with it.

## Endpoints (operator only; an OAuth tenant gets `403` `AUTHORITY_DENIED`)

* `GET /api/jarvis/tools/calls?tool=&client=&since=&limit=1..200&cursor=` newest first; `next_cursor` for the next page. The response
  says `provenance: server-witnessed` and `client_names: self-reported`.
* `GET /api/jarvis/tools/calls` returns the page **and the head from one locked snapshot** (the log's lock is held across both reads), so for an
  unfiltered request the head is always the newest entry of the first page; no append can land between them. On a later page (`cursor`) the
  head is still the log's current head.
* `GET /api/jarvis/tools/calls/verify` walks the chain across files and returns `ok`, `problems`, the `head` (`seq`, `entry_hash`),
  `anchor`, `degraded` and `gaps_recorded`. Edited, deleted, inserted and reordered lines all fail it.

## Configuration

| Variable | Default | |
|---|---|---|
| `JARVIS_CALL_LOG_ENABLED` | on in dev and test; **off when `JARVIS_ENV=production`** | the live deploy turns it on explicitly |
| `JARVIS_CALL_LOG_DIR` | `<dir of JARVIS_STORE_PATH>/call-log` | |
| `JARVIS_CALL_LOG_RETAIN_DAYS` | `90` | |
| `JARVIS_CALL_LOG_DENIED_CAP_PER_MIN` | `60` | |
| `JARVIS_CALL_LOG_READS` | on whenever the call log is on | `0` stops witnessing memory GET reads |
| `JARVIS_CALL_LOG_READ_CAP_PER_MIN` | `600` | reads beyond this a minute are summarised in one `read_suppressed` entry |

## Honest limits

* Anyone who can **write the directory** can rebuild an entire chain, and trailing entries can be **truncated** without breaking it. Mitigations:
  `verify` returns the head hash, so record it somewhere else (the relay evidence report does, and it goes into a ledger record when one is
  written); and **every hourly backup set anchors the head** (`calllog|<seq>|<entry_hash>` in its `.anchors`, kept in the offsite copies too).
  A later log must still contain the head every earlier set recorded, which `backup`, `restore` and `drill` check, so a truncation or a
  rebuild between backups is caught at the next one. `anchor.json` (the retention anchor) is in the same writable directory, so it only excuses anchors from sets older than the retention window (`JARVIS_CALL_LOG_RETAIN_DAYS` + 2 days), never a recent one. What it cannot catch: a change made and then hidden before the next backup anchors it
  (the window is one backup interval), and anyone who can also rewrite every earlier backup set and the offsite copies.
* The log proves the **server** received a call. It cannot prove which agent or person was behind a client string.
* The denied-flood cap is per process.
