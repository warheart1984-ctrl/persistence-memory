# Evidence for the phase-2 run (2026-10-07)

Raw outputs, copied unedited from the run's scratch directory. Nothing here was written by hand. The 100-round fault run was still in progress when this
was committed; its `results.json` is added when it finishes. Claims in the report that rest on a **transcript only** (no file) are listed at the end.

| File | What it shows |
|---|---|
| `soak.json`, `soak.log` | the 68-minute soak on a clean ledger: every sample (memory, retrieve and verify times, database size), the verdict computed from them. Anonymous memory 64.6 -> 82.1 MiB over 6,400 entries (3.2 MiB / 1000 records, r2 0.93); flat plateau under constant reads; 125.7 MiB after idle; 128.9 MiB after a restart under the same load (2 % apart): "NOT A LEAK" |
| `concurrency.json` | the same restarted process read at 1, 4, 16, 40 callers: high-water mark 127 / 174 / 234 / 269 MiB; at 40 callers 1,030 of 1,273 requests shed with 503 |
| `j1fix1/2/3/results.json` | the partition fault BEFORE the database was told to reap dead sessions: each run failed the new gate ("N database session(s) held for a client that is gone were still there 120 s after recovery": 1, 4, 11) |
| `j1fixb1/2/3/results.json` | the same fault AFTER (server-side keepalives live): `orphaned_sessions_at_recovery` 4, 1, 6, all reaped in 9.7 s; `slowest_answer_s` 1.02, 5.49, 5.45 |
| `k1fix1/2/3/results.json` | the full-disk fault: the database's own "No space left" log lines (44-50), writes refused (40-49), writes that fit acknowledged (7-13), 0 PANIC, 0 restarts, recovery ~0.7 s |
| `i1-1/2/results.json` | the application kill: detection, the 4 s Docker gets to restart it (it does not), `heal.sh`, startup, worst case with the 60 s timer |
| `faults-aborted-at-r003/r004/r008.stdout` | the three fault runs I stopped because a check of MINE was wrong (see the report), kept so the mistakes can be read |
| `partition_probe.py` | the experiment that measured the hang (below) |

## Only in the session transcript, not in a file

* **The 52.7 s hang before the fix and 5.5 s after.** Printed by `partition_probe.py 40` against the throwaway stack before and after the application
  image got keepalives and `tcp_user_timeout` (`app/pg_store.py`). Reproduce: check out the commit before `70ae852`'s parent
  `app/pg_store.py` change, `throwaway_stack.sh rebuild`, run the script, then check out this branch and repeat.
* **The session counts 6 -> 11 -> 19 (and 5 -> 11 -> 18) over three partitions.** Counted by hand with `pg_stat_activity` between runs; the gate that
  now measures it is `j1fix*` above.
* The first 100-round run's committed evidence is `results-2026-10-07.json` and `REPORT-2026-10-07.md` one directory up.
