"""What the READMEs say is live on 8011 must match what is deployed (the box was last deployed 2026-10-10 with schema v9: Clause V, Evidence
Objects, Continuity Blocks, Replay Contracts, emr_latest and ledger search; the signature tables since 2026-10-08), and what is shelved must say so.  If a deploy changes this, change the READMEs and this test together."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text("utf-8")
MINT = (ROOT / "deploy" / "mint" / "README.md").read_text("utf-8")


def row(text: str, start: str) -> str:
    line = next(l for l in text.splitlines() if l.startswith(start))
    return line


def test_the_status_table_says_what_is_live():
    assert "## What is live on 8011 (last deployed 2026-10-10)" in README
    for name in ("**Postgres row store**", "**Clause V**", "**Evidence Objects**", "**Continuity Blocks**"):
        assert "**live**" in row(README, f"| {name}"), name
    replay = row(README, "| **Replay Contracts, `RC.Ledger.v1`**")
    assert "**live** since 2026-10-06" in replay and "receipts" in replay and "not deployed" not in replay


def test_domain_replay_contracts_are_on_hold():
    domain = row(README, "| Domain Replay Contracts")
    assert "**on hold**" in domain and "declared only" in domain
    assert "on hold" in (ROOT / "docs" / "REPLAY_CONTRACTS.md").read_text("utf-8")


def test_signatures_are_deployed_but_shelved():
    sig = row(README, "| **Signatures**")
    for needle in ("shelved", "no key", "sign timer is off", "nothing is signed", "schema v9", "JARVIS_SIGNATURES=warn", "never as verified"):
        assert needle in sig, needle
    assert "**live**" not in sig and "not deployed" not in sig and "schema v6" not in sig


def test_no_stale_claim_that_replay_is_missing_from_the_box():
    for text in (README, MINT):
        for stale in ("not live yet", "not deployed yet", "has no `/api/jarvis/replay", "needs the build with Replay Contracts deployed", "is not on the live box until it is deployed"):
            assert stale not in text, stale
    assert "`main` is ahead of the box" not in README  # it is not: the box was deployed from main on 2026-10-10
    assert "This table describes the deployed build**, which is `main` as of 2026-10-10" in README


def test_the_mint_readme_lists_what_is_deployed_and_what_is_not():
    assert "Where things stand (updated 2026-10-10; the box was last deployed 2026-10-10)" in MINT
    assert "**Deployed on 2026-10-06:**" in MINT and "Replay Contracts (`RC.Ledger.v1`" in MINT
    assert "**Deployed on 2026-10-08 and 2026-10-10:**" in MINT and "schema **v8** and **v9**" in MINT
    assert "**Shelved, though the code is deployed:** signatures" in MINT and "the sign timer is off" in MINT
    assert "The sign timer is **off**" in MINT
