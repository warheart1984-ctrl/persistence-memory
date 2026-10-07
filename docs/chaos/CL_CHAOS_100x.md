# CL_CHAOS_100x

A hammer that runs the same probe list for 100 rounds against a **throwaway clone** of the Jarvis ledger stack and reports what held and
what did not. It exists to find the failures a unit test cannot: ordering under concurrency, fail-closed behaviour when the database
goes away, recovery, drift in the verifiers over a ledger that keeps growing.

**52 probes per round; 100 rounds is 5200 probe runs.** (The count is `len(PROBES)` in `scripts/chaos/cl_chaos_100x.py`, printed by
`--count`; this document, the sample log and the tests all use that number, and a test fails if they drift.)

## Safety, first

* **It never targets the live stack.** The default target is the throwaway stack described by `<stack dir>/stack.json`, written by
  `scripts/chaos/throwaway_stack.sh up`: its own compose project (`jarvis-chaos100x`), containers (`chaos100x-*`), images, volumes, network,
  port (18017) and randomly generated secrets, built from this repository in a directory outside it. The live stack's names, port 8011,
  `~/jarvis-ledger` and `deploy/mint/secrets` are never read or written.
* **It refuses (exit 3)** any target on port 8011 (or the port in `deploy/mint/.env`), any host that is not loopback, and any target whose
  `/ready` does not report a `chaos-throwaway:` stack identity. A target that reports **no** identity (the live stack today, or any older
  build) is refused too: the hammer needs positive proof of a throwaway, not the absence of proof of the live one. The identity is the
  `JARVIS_STACK_ID` environment variable, surfaced as `stack` in `/ready`; the live compose file defaults it to `jarvis-live`, the
  throwaway's `.env` sets `chaos-throwaway:jarvis-chaos100x`.
* `--i-know-this-is-live` lifts the port and identity refusals for a human who means it. **The chaos task never passes it** (a test
  scans the repository for a command that does), and even with it the destructive probes need the next proof.
* **Destructive probes run only on a proven throwaway**: every container in `stack.json` must be named `chaos100x-*`, must not be one of
  the live names, and must carry the compose project label `jarvis-chaos100x`; the target port must be the stack's own. Otherwise they
  are **skipped and reported as skipped**.
* Keys are test keys generated inside the throwaway directory by the script. The live key custody directory is never read, and a key
  directory inside `~/jarvis-ledger` is refused.
* The ledger is bounded: small batches (five records sealed at most five entries per block), a hard stop when the history counter reaches
  `--max-history` (default 6000), and every probe's writes are a handful of records.
* `down` removes only the throwaway project's own names, and deletes its directory only if it carries the throwaway marker file.
* **Nothing but the stack script runs `jarvisctl up`.** A new stack's `up` first checks that its copy carries a `chaos-throwaway:` identity; `rebuild`
  (re-run `jarvisctl up` in an existing throwaway copy, to pick up code changes) refuses unless that stack's own `/ready` answers and says
  `chaos-throwaway:...`: a missing `/ready`, no identity, `jarvis-live` or any other identity is a refusal. A test fails if the hammer or the soak ever
  mention `jarvisctl up`.
* **Signatures stay in `warn`.** The throwaway runs with the default `JARVIS_SIGNATURES=warn` (probe G0 fails if it ever reports another mode), the
  tooling never sets `require`, and nothing here installs or enables a systemd timer.

## Running it

```bash
scripts/chaos/throwaway_stack.sh up                       # builds and starts the clone (a few minutes the first time)
JARVIS_CHAOS_PGDATA_MB=1024 scripts/chaos/throwaway_stack.sh up   # ... with the database on a size-capped tmpfs volume (needed by the full-disk fault, K1)
scripts/chaos/throwaway_stack.sh rebuild                  # pick up code changes in a running throwaway (guarded, see above)
scripts/chaos/cl_chaos_100x.py --rounds 1 --out out/smoke # the smoke round
scripts/chaos/cl_chaos_100x.py --rounds 100 --out out/full
scripts/chaos/throwaway_stack.sh down                     # removes containers, volumes, network, images and the directory
```

`--list` prints the probes, `--count` the per-round count, `--only A2,F1` runs a subset, `--phases I,J,K` runs whole phases, `--max-history N` bounds the ledger. Exit 0 means every probe passed, no unexpected
5xx occurred and the final verifications are clean; 1 a failure; 3 a refusal.

## The probes

| Phase | What it covers |
|---|---|
| **A** records and evidence objects | create/read/history; **evidence objects are content-addressed, so creating the same content again (A2), or in another key order from another agent (A6), returns the same id with `created: false`, not a rejection**; verify; a fact citing an evidence object, a dangling link, Clause V refusals; optimistic locking; delete; the history chain |
| **B** continuity blocks (the routes are live) | **B1 seals a small batch** (five records, force-seal at most five entries per block, nothing hundreds-sized); idempotent sealing; block verification; chaining and recomputed hashes; pagination; auth and body checks |
| **C** auth and request guards | no key / wrong key; malformed, oversized and hostile input; readiness names every check and the throwaway identity; **the schema is v7** |
| **D** retrieval and hostile input | a record is found by a distinctive word; **D2: hostile query strings match nothing, raise no error, and no SQL from the input runs** (nothing written, no delay from an injected `pg_sleep`, the tables intact); filters; conflicts surface and are never merged |
| **F** replay receipts | issue a receipt at a sealed point (F1); **idempotent** (F2); **refuse unsealed points** and create nothing (F3); the service re-derives it (F4) and the offline verifier re-derives it from the raw rows (F5); **F6: on a scratch copy of the database, rewrite a history entry consistently and re-seal every block; the database's own verifiers then pass, and re-deriving the receipt taken before the rewrite fails** |
| **G** signatures, warn mode | a root authorizes a test signing key (G0); **the real signer** signs the pending blocks and receipts, each re-derived first (G1); **a forged signature (G2), a signature by an unauthorized key (G3), a revoked key (G4), a valid signature over different content or at a wrong position (G5) are rejected and store nothing**; **verify never reports success with no trust root** and `require` fails (G6); a signed receipt and block report L1 in warn mode, never more (G7). Test keys, throwaway stack only |
| **E** the database role and row-level security | **as the ordinary non-superuser application role (E5 among them)**: not a superuser and cannot bypass RLS; with no tenant set every ledger table looks empty; with the operator tenant it sees exactly the operator's rows; history, blocks, signatures and receipts cannot be changed or removed; a row for another tenant cannot be written; a hostile tenant string does nothing |
| **I** the application is killed | `kill -9` of the application container (`docker kill -s KILL`) while four writers run: nothing is acknowledged between the kill and the new process; Docker's restart policy is given four seconds (it does not restart an API-killed container; `heal.sh` is the box's answer, so the probe runs the throwaway copy of it exactly as the timer does); recovery is measured; then the gates below (I1) |
| **J** the application is cut off from the database | `docker network disconnect` of the database mid-write for 12 s, then reconnect: no read or write is acknowledged through the severed link, **every request and `/ready` is answered (503) within 15 s** (the dead connection must be noticed, not waited on), liveness stays up; recovery is measured; the gates (J1) |
| **K** the database volume is full | with the database on the size-capped tmpfs volume, a filler file is written until the volume is full (a real disk is never filled) and removed after the faults has bitten: writes are refused with 503, never 500; recovery and the database log are examined; the gates (K1). Skipped, and said so, on a stack without the capped volume |
| **H** destructive | **database down** (stopped, then started): 503 with `Retry-After`, liveness stays up, nothing written is lost, recovery time measured (H1); **schema mismatch**: readiness fails naming `schema_version` and recovers when the mismatch is removed (H2); **pool flood**: 160 concurrent reads end in 200 or a 503 with `Retry-After`, never a 500 (H3); **concurrent writers and force-seals** (12 callers share a pool of 10, so an occasional request is shed): nothing acknowledged is lost, any refusal is a clean 503 with `Retry-After`, blocks stay contiguous and verify (H4) |

### The gates after every ugly-conditions fault

1. **No half-writes.** Every write the API acknowledged exists, whole (the content that was sent, exactly one `create` in its history); every record the
   writers produced is whole; no memory row without a history entry; the history counter equals the newest history seq; the database's own history and
   block verifiers report nothing. A write that committed but whose acknowledgement was lost (the client never saw a 200) is counted separately.
2. **The history chain and the blocks verify** (the API's verifiers).
3. **A replay receipt taken before the fault still re-derives**, through the service and offline from the raw rows in a one-off container.
4. **The API failed closed during the fault and recovered after it**: nothing begun after the fault was acknowledged while it was in force (except in
   the full-disk fault, where a write that fits in space already allocated may succeed: each such write must then be whole and durable, gate 1), every
   refusal was a status the fault explains (connection refused or 503; 503 only for the full disk), and the application then took and returned a write.
5. **The server gave back the slots of clients that are gone.** Database sessions with no matching socket in the application container (found by reading
   `/proc/net/tcp` in the container and `pg_stat_activity`) must be reaped by the server within two minutes. `max_connections` is 30, and an unreaped
   partition strands several sessions per incident.

### Requests under a silent partition

J1 also fails if any request, or `/ready`, takes longer than 15 s to be answered while the link is cut. Before `app/pg_store.py` bounded dead
connections (libpq keepalives and `tcp_user_timeout`), requests and `/ready` hung for the whole partition plus the TCP recovery (52 s measured).

## The soak

`scripts/chaos/soak.py` answers two questions the hammer's first report left open: is the application's memory growth a leak or a cache, and does
retrieval slow down with the ledger? Four phases on one throwaway stack: **growth** (records written and sealed in batches while memory, a typical and a
hostile retrieve, `blocks/verify`, `history/verify` and the database size are sampled), **reads** on the then-constant ledger (memory against *requests*:
a slope there is a leak), **idle** (does memory come back?) and a **control** (restart the application container, same ledger, same load: does it return
to the same level?). An optional fifth, **concurrency** (`soak.py --concurrency`), restarts the application and reads at 1, 4, 16 and 40 callers at once,
reading the process's high-water mark after each: a retrieve that materialises the ledger makes the peak follow callers x ledger size. The verdict is computed from the samples (`verdict()` in the script; thresholds and synthetic leak/cache/accumulation cases are in
`tests/test_soak.py`) and printed with the numbers.

## What it reports

Per probe and per round PASS / FAIL / ERROR / SKIP with the time; a JSON summary (`results.json`) with the probe runs by status, every
HTTP status seen, **every 5xx with its round, probe and whether the probe expected it** (a 503 during the database outage is expected,
anything else is a finding), latency percentiles overall and per probe, writes, blocks sealed and signed, outage detection and recovery
times, flood results, and the final checks on the ledger the run leaves behind: the history chain, the blocks, the signing log, every
receipt re-derived, `jarvisctl verify` in the throwaway stack, container CPU and memory, the database size, and that no scratch database
or schema mismatch was left behind.

## What it does not test

A crash that Docker's restart policy handles (an out-of-memory kill, or a process that dies by itself) is not exercised: `docker kill` is a manual stop and
the policy ignores it, and the host account cannot signal a process in the container. A full volume is simulated on a size-capped tmpfs, not on the
real disk, so its timing and the behaviour of a real filesystem at 100 % are not measured. A partition is a dropped link between two containers on
one bridge network, not a flaky Wi-Fi link.

It does not exercise the OAuth and MCP surfaces, the AMUL/RAG/LLM routes, backups, restores or the drill (those have their own
rehearsals), `require` mode (warn only, as asked), L2 (no root cosigns a checkpoint during the run), the offsite path, anything on the
Windows PC, or the live stack. It runs on one machine over loopback, so network failures are not simulated, and CPU and memory figures
describe this host, not the Mint box.
