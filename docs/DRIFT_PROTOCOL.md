# Drift protocol (operator)

**Status:** **partial** — hash fidelity is enforced in tests; multi-day semantic consistency is operator-owned.

## What the system verifies

1. Capture a baseline row (or use `tests/fixtures/drift_baseline.json` pattern).
2. On later retrieve, compare `content_sha256` to the baseline hash of normalized content.
3. On mismatch: treat as continuity incident — do not silently rewrite; append a new record with `supersedes` if replacing intentionally.

This catches **tampering / corruption / accidental rewrite** of stored bytes. It does **not** prove that day-30 decisions still agree with day-1 intent.

## What operators must own

- Multi-day / multi-week semantic agreement across agents and sessions
- Scheduled retrieve + human (or consumer) review of open conflicts on critical subjects
- Incident response when hash matches but meaning has drifted (new contradictory posts on same `subject`)

Automated multi-day schedulers are **not** shipped in this package.

**Does this gap matter for unattended long-horizon agents?** Yes — if an agent fleet must stay consistent without an operator, hash checks alone are insufficient. Plan an external protocol or accept that continuity is “byte fidelity + conflict surfacing,” not “semantic lock.”
