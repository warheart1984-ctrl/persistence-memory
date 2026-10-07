# CL_CHAOS_100x

A hammer that runs the same probe list for 100 rounds against a **throwaway clone** of the Jarvis ledger stack and reports what held and
what did not. It exists to find the failures a unit test cannot: ordering under concurrency, fail-closed behaviour when the database
goes away, recovery, drift in the verifiers over a ledger that keeps growing.

**49 probes per round; 100 rounds is 4900 probe runs.** (The count is `len(PROBES)` in `scripts/chaos/cl_chaos_100x.py`, printed by
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

## Running it

```bash
scripts/chaos/throwaway_stack.sh up                       # builds and starts the clone (a few minutes the first time)
scripts/chaos/cl_chaos_100x.py --rounds 1 --out out/smoke # the smoke round
scripts/chaos/cl_chaos_100x.py --rounds 100 --out out/full
scripts/chaos/throwaway_stack.sh down                     # removes containers, volumes, network, images and the directory
```

`--list` prints the probes, `--count` the per-round count, `--only A2,F1` runs a subset. Exit 0 means every probe passed, no unexpected
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
| **H** destructive | **database down** (stopped, then started): 503 with `Retry-After`, liveness stays up, nothing written is lost, recovery time measured (H1); **schema mismatch**: readiness fails naming `schema_version` and recovers when the mismatch is removed (H2); **pool flood**: 160 concurrent reads end in 200 or a 503 with `Retry-After`, never a 500 (H3); **concurrent writers and force-seals**: no write fails, blocks stay contiguous and verify (H4) |

## What it reports

Per probe and per round PASS / FAIL / ERROR / SKIP with the time; a JSON summary (`results.json`) with the probe runs by status, every
HTTP status seen, **every 5xx with its round, probe and whether the probe expected it** (a 503 during the database outage is expected,
anything else is a finding), latency percentiles overall and per probe, writes, blocks sealed and signed, outage detection and recovery
times, flood results, and the final checks on the ledger the run leaves behind: the history chain, the blocks, the signing log, every
receipt re-derived, `jarvisctl verify` in the throwaway stack, container CPU and memory, the database size, and that no scratch database
or schema mismatch was left behind.

## What it does not test

It does not exercise the OAuth and MCP surfaces, the AMUL/RAG/LLM routes, backups, restores or the drill (those have their own
rehearsals), `require` mode (warn only, as asked), L2 (no root cosigns a checkpoint during the run), the offsite path, anything on the
Windows PC, or the live stack. It runs on one machine over loopback, so network failures are not simulated, and CPU and memory figures
describe this host, not the Mint box.
