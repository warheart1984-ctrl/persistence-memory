"""Red-team regressions — every test names a hole it pins shut.

Each test maps to a demonstrated defect found adversarially reviewing the
merged TwinChat vertical slice (PR #53):

F1  cited memory-id digits launder numbers into the sentence's NUMBER check
F2  whitespace-only or cite-only model output ships an empty/"[id]" reply
F3  lease clock is process-relative monotonic persisted to disk — an
    abandoned lease reads as live forever after a restart
F4  Turn.content<=8000 validates AFTER the receipt commits — oversized reply
    raises ValidationError post-receipt (and post-persist), a 500 on a
    committed turn
F5  prompt contract says the model may discuss shown confidence/status but
    neither is citable — true restatements die NUMBER_MISMATCH/CLAIM_WORD
F6  reads on the shared SQLite conn bypass _DB_LOCK — uncommitted rows are
    observable (phantom receipts)
F7  lease release is unconditional — a stale owner's release evicts a
    takeover lease, allowing concurrent turns on one session
F8  extract() scans the entire ledger every turn even with zero candidates
F9  backend usage never reaches the receipt
F10 persist receipts carry no timestamp
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from app.twinchat.gate import gate_reply
from app.twinchat.extract import extract
from app.twinchat.receipts import ReceiptStore
import app.twinchat.service as svc
from app.twinchat.models import ChatRequest


def _rec(id="mem-abc123def456", content="deploy uses postgres",
         subject="deploy", status="verified", confidence=0.9,
         mtype="fact", tags=()):
    return {
        "id": id, "content": content, "subject": subject,
        "type": mtype, "status": status, "confidence": confidence,
        "tags": list(tags),
    }


# --- F1: id digits are not evidence -----------------------------------------

def test_digits_inside_a_cited_memory_id_do_not_launder_numbers():
    """A hex id carrying digits must not satisfy NUMBER checks.

    'mem-a1b2c3d4e5f6' contains 1..6 — today those digits count as cited
    numbers, so fabricated counts ('5 projects', '3 conflicts') survive with
    zero textual support.
    """
    recalled = [_rec(id="mem-a1b2c3d4e5f6")]
    out = gate_reply(
        "There are 5 open projects and 3 conflicts [mem-a1b2c3d4e5f6].",
        recalled,
    )
    assert out["dropped"], "digits sourced from the memory id laundered a fabricated count"
    assert out["dropped"][0].reason == "NUMBER_MISMATCH"


def test_a_number_actually_in_the_cited_content_still_passes():
    recalled = [_rec(content="postgres runs on port 5432")]
    out = gate_reply("Postgres runs on port 5432 [mem-abc123def456].", recalled)
    assert out["dropped"] == [] and out["kept"]


# --- F2: empty and bare-cite output is not a reply ---------------------------

def test_whitespace_only_output_is_all_dropped():
    out = gate_reply("   \n\t  ", [_rec()])
    assert out["all_dropped"], "whitespace output returned reply='' instead of falling back"


def test_a_lone_cite_marker_is_not_a_reply():
    out = gate_reply("[mem-abc123def456]", [_rec()])
    assert out["all_dropped"], "a bare '[id]' marker shipped as the entire reply"


# --- F5: shown metadata is citable -------------------------------------------

def test_metadata_prose_dies_but_verbatim_extract_survives():
    """F5, updated for the cited-value-support gate (PR #54): shown metadata
    is citable, but free-form restatement drops UNSUPPORTED_TEXT — only an
    exact extract of the cited value survives. The persona contract was
    updated to match (it previously promised prose discussion of status and
    confidence that the gate could never allow)."""
    recalled = [_rec(status="verified", confidence=0.9)]
    prose = gate_reply(
        "Its confidence is 0.9 and the record is verified [mem-abc123def456].",
        recalled,
    )
    assert prose["dropped"], "paraphrased metadata must not pass the grounding check"
    assert prose["dropped"][0].reason == "UNSUPPORTED_TEXT"
    extract = gate_reply("verified [mem-abc123def456].", recalled)
    assert extract["kept"], "an exact extract of the cited status value should survive"


def test_status_claim_word_still_dies_when_the_record_disagrees():
    """Citing status must not weaken CLAIM_WORD: a 'verified' claim against a
    draft record still drops."""
    out = gate_reply(
        "This is verified [mem-abc123def456].",
        [_rec(status="draft", content="deploy uses postgres")],
    )
    assert out["dropped"] and out["dropped"][0].reason == "CLAIM_WORD"


# --- F3: lease expiry is wall-clock, restart-safe ----------------------------

def test_lease_expiry_survives_process_restart(tmp_path):
    """An abandoned lease must expire in wall time — monotonic clocks reset
    per process and must never gate takeover."""
    db = tmp_path / "r.sqlite3"
    store1 = ReceiptStore(db)
    acquired, _, _tok = store1.acquire_lease("t", "s")
    assert acquired
    # The row survives a crash; whatever clock wrote it, lease_until must be
    # comparable to wall time in a fresh process.
    conn = sqlite3.connect(str(db))
    lease_until = conn.execute(
        "SELECT lease_until FROM leases WHERE tenant_key='t' AND session_id='s'"
    ).fetchone()[0]
    conn.close()
    assert abs(lease_until - time.time()) < 130, (
        "lease_until is on a monotonic clock — incomparable across processes"
    )


def test_stale_lease_takeover_after_expiry(tmp_path):
    db = tmp_path / "r.sqlite3"
    store = ReceiptStore(db)
    assert store.acquire_lease("t", "s")[0]
    store._conn.execute(
        "UPDATE leases SET lease_until=? WHERE tenant_key='t' AND session_id='s'",
        (time.time() - 1,),
    )
    store._conn.commit()
    acquired, stale, _ = store.acquire_lease("t", "s")
    assert acquired and stale


def test_legacy_ownerless_lease_is_not_stolen_during_clock_migration(tmp_path):
    """A pre-owner lease may contain a monotonic deadline from another process.

    Until old workers have drained, the new wall-clock code cannot infer
    whether that lease is active. It must fail closed instead of stealing it.
    """
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE leases (tenant_key TEXT NOT NULL, session_id TEXT NOT NULL,"
        " lease_until REAL NOT NULL, PRIMARY KEY (tenant_key, session_id))"
    )
    conn.execute(
        "INSERT INTO leases VALUES ('t', 's', ?)", (time.monotonic() + 30,)
    )
    conn.commit()
    conn.close()

    store = ReceiptStore(db)
    acquired, stale, token = store.acquire_lease("t", "s")
    assert not acquired and not stale and token is None, (
        "new wall-clock worker stole an owner-less lease from a pre-upgrade worker"
    )

    # After the deployment has drained old workers, their token-less cleanup
    # remains compatible and allows the next worker to start a turn.
    store.release_lease("t", "s")
    acquired, _, token = store.acquire_lease("t", "s")
    assert acquired and token


def test_concurrent_legacy_lease_schema_migration_is_serialized(tmp_path):
    """Two workers opening the same pre-owner database must both initialize."""
    db = tmp_path / "legacy-concurrent.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE leases (tenant_key TEXT NOT NULL, session_id TEXT NOT NULL,"
        " lease_until REAL NOT NULL, PRIMARY KEY (tenant_key, session_id))"
    )
    conn.commit()
    conn.close()

    start = threading.Barrier(3)
    stores = []
    errors = []

    def initialize():
        start.wait()
        try:
            stores.append(ReceiptStore(db))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=initialize) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=15)

    assert not any(worker.is_alive() for worker in workers), "schema migration hung"
    assert not errors, f"concurrent lease schema migration failed: {errors!r}"
    assert len(stores) == 2
    assert all(
        "owner" in {row[1] for row in store._conn.execute("PRAGMA table_info(leases)")}
        for store in stores
    )
    for store in stores:
        store._conn.close()
        store._read.close()


# --- F7: a stale owner's release cannot evict a takeover lease ---------------

def test_release_by_stale_owner_does_not_evict_new_lease(tmp_path):
    db = tmp_path / "r.sqlite3"
    store = ReceiptStore(db)
    _, _, token_a = store.acquire_lease("t", "s")
    # A's turn outlives its lease; B takes over.
    store._conn.execute(
        "UPDATE leases SET lease_until=? WHERE tenant_key='t' AND session_id='s'",
        (time.time() - 1,),
    )
    store._conn.commit()
    acquired_b, stale, _tok_b = store.acquire_lease("t", "s")
    assert acquired_b and stale
    # A finally finishes and releases — must NOT delete B's lease.
    store.release_lease("t", "s", token=token_a)
    acquired_c, _, _ = store.acquire_lease("t", "s")
    assert not acquired_c, "stale owner's release evicted the takeover lease"


# --- F6: reads never observe uncommitted rows --------------------------------

def test_reads_do_not_observe_uncommitted_receipt_rows(tmp_path):
    db = tmp_path / "r.sqlite3"
    store = ReceiptStore(db)
    store.append_turn_receipt("t", {"session_id": "s", "at": "t0", "request_digest": "x"})

    entered = threading.Event()
    release = threading.Event()

    def writer():
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute(
            "INSERT INTO receipts (tenant_key,session_id,turn_index,"
            " receipt_digest,prev_digest,at,body_json)"
            " VALUES (?,?,?,?,?,?,?)",
            ("t", "s", 99, "sha256:phantom", "x", "t1", "{}"),
        )
        entered.set()
        release.wait(5)
        store._conn.execute("ROLLBACK")

    wt = threading.Thread(target=writer)
    wt.start()
    assert entered.wait(3)
    seen = [r["turn_index"] for r in store.session_turns("t", "s")]
    release.set()
    wt.join()
    assert seen == [0], f"reader observed uncommitted phantom row(s): {seen}"


# --- F4: oversized replies degrade, never raise post-commit ------------------

def test_oversized_reply_does_not_raise_after_receipt_commits(tmp_path, monkeypatch):
    os.environ["JARVIS_TWIN_CHAT_DIR"] = str(tmp_path)
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", "1")

    from app.store import get_store
    from app.models import MemoryCreate, EvidenceLink
    from app.twinchat.backends import BackendResult

    store = get_store()
    rec = store.create_memory(MemoryCreate(
        type="fact", content="deploy uses postgres", source_agent="user:t",
        subject="deploy", status="verified", session_id="seed",
        evidence=[EvidenceLink(kind="receipt", ref="seed:r1")],
    ))
    # Verbatim-extract sentences are the only form that survives the gate —
    # 350 of them exceed the 8000-char Turn bound.
    big = ". ".join([f"deploy uses postgres [{rec.id}]"] * 350)
    assert len(big) > 8000

    class BigBackend:
        name = "big"
        def chat(self, messages, **kw):
            return BackendResult(text=big, model="big", latency_ms=1)

    monkeypatch.setattr(svc, "resolve_backend", lambda p: BigBackend())
    monkeypatch.setattr(svc, "get_receipt_store", lambda: ReceiptStore(tmp_path / "r.sqlite3"))

    out = svc.run_turn(store, ChatRequest(session_id="s1", message="postgres deploy"), tenant_key="t")
    # Whatever the reply, the call must succeed; the stored window turn must
    # respect the Turn bound (8000) even though the receipt digests the full
    # reply.
    turns = svc._sessions.load("t", "s1")
    assert turns, "turn was never appended to the session window"
    assert all(len(t.content) <= 8000 for t in turns)
    assert len(out.reply) > 0


# --- F8: dedup scan only runs when candidates exist --------------------------

def test_extract_does_not_scan_the_ledger_when_no_candidate_patterns(tmp_path):
    called = []

    def _existing():
        called.append(True)
        return []

    out = extract("what is the weather today", existing=_existing)
    assert out == [] and called == [], (
        "the dedup scan ran on a message with zero extractable patterns"
    )


def test_extract_still_dedups_when_candidates_exist():
    class M:
        subject = "use postgres"
        content = "use postgres"

    out = extract("I decided to use postgres", existing=lambda: [M()])
    assert out == [], "a proposal identical to an existing memory must dedup away"


def test_persist_receipt_is_timestamped(tmp_path, monkeypatch):
    """F10: the linked outcome receipt must carry `at` like the base does."""
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", "1")
    from app.store import get_store
    from app.twinchat.persist import persist_claims
    from app.twinchat.models import ProposedClaim

    store = get_store()
    receipts = ReceiptStore(tmp_path / "p.sqlite3")
    base = receipts.append_turn_receipt(
        "t", {"session_id": "s", "at": "t0", "request_digest": "x"}
    )
    outcome = persist_claims(
        store, tenant_key="t", session_id="s",
        turn_index=base["turn_index"],
        turn_receipt_digest=base["receipt_digest"],
        proposals=[ProposedClaim(
            claim_type="decision", content="use postgres", subject="use postgres",
            attribution="user", evidence_kind="user-request", extractor="rule:decision_0",
        )],
        receipt_store=receipts,
    )
    assert outcome.at, "persist receipt carries no timestamp"


# --- F9/F10: receipt completeness --------------------------------------------

def test_backend_usage_is_receipted(tmp_path, monkeypatch):
    os.environ["JARVIS_TWIN_CHAT_DIR"] = str(tmp_path)
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", "1")

    from app.store import get_store
    from app.models import MemoryCreate, EvidenceLink
    from app.twinchat.backends import BackendResult

    store = get_store()
    rec = store.create_memory(MemoryCreate(
        type="fact", content="deploy uses postgres", source_agent="user:t",
        subject="deploy", status="verified", session_id="seed",
        evidence=[EvidenceLink(kind="receipt", ref="seed:r1")],
    ))

    class UsageBackend:
        name = "metered"
        def chat(self, messages, **kw):
            return BackendResult(
                text=f"deploy uses postgres [{rec.id}]", model="m1",
                latency_ms=3, usage={"prompt_tokens": 11, "completion_tokens": 7},
            )

    monkeypatch.setattr(svc, "resolve_backend", lambda p: UsageBackend())
    monkeypatch.setattr(svc, "get_receipt_store", lambda: ReceiptStore(tmp_path / "u.sqlite3"))

    out = svc.run_turn(store, ChatRequest(session_id="s1", message="postgres deploy"), tenant_key="t")
    assert out.receipt.usage == {"prompt_tokens": 11, "completion_tokens": 7}, (
        "usage was captured by the backend but dropped before the receipt"
    )
