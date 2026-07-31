# Drift protocol (operator)

**Status:** **partial** — hash fidelity is enforced in tests; multi-day monitoring is operator-owned.

1. Capture a baseline row (or use `tests/fixtures/drift_baseline.json` pattern).
2. On later retrieve, compare `content_sha256` to the baseline hash of normalized content.
3. On mismatch: treat as continuity incident — do not silently rewrite; append a new record with `supersedes` if replacing intentionally.
4. Automated multi-day schedulers are **not** shipped in this package.
