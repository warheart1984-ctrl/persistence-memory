# CL_CHAOS_100x phase 2: ugly conditions and a soak (2026-10-07)

Three new fault phases (the application killed, the application cut off from the database, the database volume full), a soak, and what they found. Same
rules as the first report: a throwaway clone (project `jarvis-chaos100x`, containers `chaos100x-*`, port 18017, its own volumes, images, secrets and test
keys, the database on a 1 GiB size-capped tmpfs held by a keeper container so that **no real disk is ever filled**). The hammer refuses the live port
(exit 3) and any target whose `/ready` is not `chaos-throwaway:`. The live stack (`jarvis-db`, `jarvis-app`, 8011) was never contacted; `--i-know-this-is-live`
was never passed; `JARVIS_SIGNATURES` stayed at its default `warn`; no timer was enabled; `jarvisctl up` was run only through the guarded
`throwaway_stack.sh up|rebuild`. Raw outputs: `evidence-2026-10-07-phase2/` (its README says which claims have a file and which rest on the transcript),
`results-2026-10-07-phase2-faults.json`.

**52 probes per round (49 + I1, J1, K1). The fault run: 100 rounds of the three fault probes = 300 probe runs; 300 PASS, 0 FAIL, 0 ERROR, 0 SKIP, 0
unexpected 5xx** (seed `bfe89afc`, 6 940 s). The 100-round run of all 49 earlier probes is the first report; this change ran all 52 once (the committed
sample log, 52 of 52 PASS) and the three new ones 100 times, because the new probes are the point and a full 52-probe round takes about 3 minutes on a small ledger (and grows with it).

## What the hammer got wrong before the ledger did

None of these was a ledger failure; each was found by the run or by the tests before the run, and the runs that were wrong were stopped and thrown away
(`evidence-2026-10-07-phase2/faults-aborted-at-r003/r004/r008.stdout`).

* `fail_closed` built its failure message from `wrong[0]` even when `wrong` was empty: it would have crashed every fault probe that passed. A unit test
  found it before any run.
* The client did not treat a response cut off by a dying server (`http.client.IncompleteRead`) as a failed request: writer threads died silently during
  the kill. Now a failed request (status 0), and a writer cannot die unnoticed.
* The fault time was taken before `docker kill` returned, so requests in the first moments (before the signal landed) counted as "acknowledged while the
  fault was in force"; readiness was watched only after the heal, so detection looked late.
* It assumed Docker restarts a container killed with `docker kill`. It does not: an API kill is a manual stop and the restart policy ignores it (the repo's
  own `heal.sh` says so). The probe now follows the real recovery path: give Docker four seconds, then run the throwaway copy of `heal.sh` as the timer would.
* Writers were unpaced; the ledger cap (6 000) stopped the first fault run in round 1.
* K1 demanded that no write be acknowledged while the volume was full. Wrong: a write that fits in a page that already exists succeeds; what must hold is
  that each acknowledged write is whole (gate 1) and every refusal a clean 503. K1 also accepted *any* non-200 as proof the fault bit; a pool shed would
  have done. The proof is now the database's own "No space left on device" log lines. (Aborted at r004 and r008.)
* H4 (12 callers on a pool of 10) demanded zero refusals; one clean 503 shed failed a smoke round. Now: nothing lost, any refusal a clean 503 with `Retry-After`.
* `throwaway_stack.sh rebuild` did not refresh the database build directory (a copy made when the stack was created), so a `postgresql.conf` change was not
  live; the new orphaned-session gate failed three runs in a row until it did. And `SHOW tcp_keepalives_idle` reads 0 over a Unix socket whatever the file says:
  it has to be read over TCP.
* Test hygiene: CI sets `XDG_CONFIG_HOME` (a test then wandered into the wrong unit directory), and a test read every file under `docs/chaos/` as text.

## Chaos metrics (100 rounds, the three faults)

| | |
|---|---|
| probe runs | 300 of 300 expected; PASS 300 |
| HTTP requests | 343 096: 200 ×115 894, **503 ×211 794**, connection refused/reset (status 0) ×15 408; no 500, no timeout |
| 5xx | 227 202, **all expected**: 206 860 × 503 in the partition (J1), 4 934 × 503 on the full volume (K1), 15 408 refused connections while the application was dead (I1). Unexpected: 0 |
| gates after every fault (all 300) | no half-writes (every acknowledged write present and whole, one `create` in its history, no memory row without history, history counter = newest history seq), the database's and the API's history and block verifiers clean, a receipt taken just before the fault re-derived by the service **and** offline from the raw rows, the API took and returned a write afterwards |
| **kill -9 of the application (I1, ×100)** | detected at once (0.0 s); **recovery from the kill to a stable `/ready`: min 6.94 s, median 7.23 s, max 7.69 s**, of which 4.0 s is the wait for Docker's restart policy and 2.9–3.7 s (median 3.2 s) is the application's start after `heal.sh`. **Docker restarted it by itself 0 of 100 times**: recovery is the self-heal. With the real 1-minute timer the worst case is the interval plus the start: **62.9–63.7 s (computed, not measured with the timer)**. 5 835 writes begun during the outage: all refused (connection refused); none acknowledged. 12 writes committed whose acknowledgement never arrived, all whole |
| **database cut off for 12 s (J1, ×100)** | **recovery after reconnecting: min 1.04 s, median 1.50 s, max 7.33 s.** All 97 889 reads and all 97 889 `/ready` polls made during the partitions were answered 503 (none hung); the **slowest answer min 1.01 s, median 1.04 s, max 5.48 s**; 7 662 writes begun during it all refused with 503, none acknowledged |
| **database volume full (K1, ×100)** | the fault bit **0.68–6.35 s** after the fill (median 0.92 s); the volume was full 4.1–9.8 s (median 4.4 s); **recovery after freeing: min 0.61 s, median 0.70 s, max 0.82 s**. 4 938 "No space left" errors; 4 620 writes refused with 503, never 500; 1 530 acknowledged while full (they fit in an existing page) and each found whole afterwards; no restart of the database container |
| orphaned database sessions | 290 sessions over the 100 partitions were held by the server for a client that was gone (0–5 per run, median 4); **all reaped on their own, 0.2–9.8 s after recovery (median 6.5 s)**. In I1 and K1 there were none |
| ledger at the end | 17 029 history entries, 2 249 blocks (5 796 entries unsealed: the fault writers do not seal, by design), 115 receipts, database 43 MB; history and blocks verify, all 115 receipts re-derive, `jarvisctl verify` in the throwaway exit 0, schema v7, no scratch database left. Signing was not exercised in these runs (no attestations on this stack) |

## Fixes the runs proved necessary (and a test for each)

1. **A silently dead database link hung requests, `/ready` included, for as long as TCP kept retrying.** `statement_timeout` is enforced by the server, which is
   the thing that is unreachable, and the pool timeout only bounds waiting for a free connection. Measured with `partition_probe.py`: with the old
   `app/pg_store.py` the four requests caught by the cut were held for **52.5–52.6 s** (nothing answered until 12 s after the link healed); with this branch
   (libpq keepalives and `tcp_user_timeout` on every pooled connection) the same four were answered **503 after 5.5 s** and 18 145 further requests during the
   partition were all answered 503, the longest 5.5 s (`partition-before-old-pg_store.txt`, `partition-after-this-branch.txt`, `partition-comparison.txt`).
   Test: the options are read back from a real connection's socket (`tests/test_pg_dead_link.py`).
2. **A partition stranded database sessions for two hours.** The server never learned the client had closed its socket (the FIN was lost in the partition) and
   PostgreSQL's keepalives default to the operating system's two hours; `max_connections` is 30. Each partition left 6–7 sessions that did not go away
   (hand counts 5 → 11 → 18 and 6 → 11 → 19 over three runs; the new gate failed the same three runs with 1, 4 and 11 sessions still held after 120 s,
   `j1fix1/2/3`), and in the first fault run the application could no longer reach its own database. `tcp_keepalives_*` and `tcp_user_timeout` in the
   image's `postgresql.conf` fix it (`j1fixb1/2/3` and the 290 above). Test: `tests/test_pg_conf.py` bounds the settings; the fault gate measures the effect.
   **This changes the database image's configuration: it is in this change and is not deployed.**

## The soak (`soak.py`, 68 minutes, a clean ledger that grew to 6 400 entries)

* **Memory is not a leak.** Anonymous memory grows 64.6 → 82.1 MiB with the ledger (3.2 MiB per 1 000 records, r² 0.93). On the then-constant ledger it
  warmed to about 128 MiB within a few hundred requests and then stayed flat for the remaining ≈4 700 (no trend; the computed slope has r² 0.19); idle released
  nothing (125.7 MiB: Python does not hand arenas back); **a restarted process under the same load reached 128.9 MiB, 2 % from where the old one ended.**
* **The 76 → 279 MiB of the first report is most likely the high-water mark of concurrent retrievals, but the concurrency run does not prove a per-caller figure.**
  A retrieve materialises the whole ledger, so the peak should follow callers × ledger size. The committed run (6 400 entries, 1 caller 127 MiB, 4 callers 174,
  16 callers 234, 40 callers 269) has two defects found in review: it restarted the application **once** and then ran the levels in ascending order, and `VmHWM` is a
  process-lifetime maximum, so each figure carries every earlier level's peak (it cannot separate caller count from elapsed load); and at 40 callers 1 030 of 1 273
  requests were shed (503), so that level was not 40 concurrent readers. `soak.py --concurrency` now restarts the application before every level and marks a level
  with under 95 % of requests answered as invalid and leaves it out of the analysis. **That corrected run has not been done** (the throwaway stack was torn down); the
  "about 3.3 MiB per extra caller" figure is withdrawn. What stands without it: memory is flat against requests on a constant ledger, returns to the same level after a
  restart, and the pool-flood probe's 40 callers is a plausible source of the first report's peak.
* **Retrieval cost follows ledger size.** A typical retrieve 5 ms → 357 ms and a hostile-string one 4 ms → 309 ms over 6 400 entries (log-log exponents
  0.93 and 0.99: linear); `history/verify` 6 → 360 ms (0.93); `blocks/verify` 8 → 121 ms (0.70).
* Nothing was changed for this: it is how retrieval is built (every record is read and scored in Python, then limited), not a fault. It is the limit to plan
  around (40 simultaneous retrievals on 6 400 entries is 270 MiB), and the candidates are in the next section.

## Anything unexpected

1. **PostgreSQL crash-recovers itself when the volume fills at the wrong moment.** In one of the 100 runs the checkpointer could not write
   `pg_logical/replorigin_checkpoint.tmp` and raised **PANIC**; the server aborted all its processes and ran crash recovery inside the same container (no
   container restart). Every gate still held and the application recovered, but the database dropped every connection for a moment. (A second PANIC in the log is
   from my manual trials before the run.) It is PostgreSQL's behaviour, not something this change causes.
2. **Docker does not restart an API-killed container** (0 of 100), so the application's recovery from `docker kill` is the self-heal timer: up to a minute
   plus 3 s. A crash that Docker's policy handles (an out-of-memory kill) was not exercised.
3. **Under a flood the share of requests shed grows with the ledger** (first report), consistent with the soak: each retrieve holds more and runs longer, so
   the 10-connection pool is busy longer.
4. The first fault run stopped at round 1 against the 6 000-entry cap: the cap works, and my writers were too fast.

Candidates if retrieval ever has to scale (not done, not proven necessary at today's size): do the type/status/session/subject filters and the `limit` in SQL
before scoring, cap concurrent retrievals below the pool size so a burst cannot hold 40 copies, and stream rather than materialise.

## What this run could not test

* A crash that Docker's restart policy handles (OOM kill, a process that dies by itself): the host account cannot signal a process in the container, and
  `docker kill` is a manual stop.
* A real full disk (only a size-capped tmpfs was filled, by design), its timing on a real filesystem, and WAL on a separate volume.
* A flaky link (loss, latency, reordering): the partition is a clean cut of one bridge network, 12 s long; longer cuts, and a cut of the application from the
  host, were not tried. The database was cut off, not the client from the PC (the SSH tunnel is untested).
* The faults while a block is being sealed, while a backup runs, or while a restore is under way; backups, restores, the drill and the offsite copy; the
  signing path under faults (no attestations existed on this stack); `require` mode; L2.
* The soak is 68 minutes and 6 400 entries on one 4-core host; behaviour at 100 000 records, or over days, is extrapolation. The memory plateau was
  measured, not proven to hold forever.
* The new database settings and `app/pg_store.py` on the live stack: **not deployed**, so nothing here says how the live ledger behaves; it is still the
  v6 build without them. The worst-case self-heal recovery (63 s) is the timer's interval plus a measured start, not a measurement of the real timer.
