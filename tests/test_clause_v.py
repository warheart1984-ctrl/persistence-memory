"""Clause V gate: the ledger stores evidence, not memory.

Hard rules (type, evidence) refuse with 422 ``clause_v_violation``. Emotion / transient / transcript findings are
warn-only until the operator flips JARVIS_CLAUSE_V_SOFT. A record can never become ``verified`` unless it passes.
The older test suite opts out through conftest (JARVIS_CLAUSE_V=off); this file opts back in.
"""

from __future__ import annotations

import logging
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import clause_v
from app.clause_v import ClauseVViolation
from app.emr_write import EmrRememberRequest, emr_remember
from app.main import app
from app.models import EvidenceLink, MemoryCreate
from app.store import get_store

client = TestClient(app)

USER_REQUEST = {"kind": "user-request", "ref": "chat:triage", "note": "the operator asked for this"}
FILE_EV = {"kind": "file", "ref": "docs/POSTGRES.md", "note": "documented there"}

# The operator's own words, exactly as approved for the values record. "happy" is inside a quotation.
VALUES_TEXT = (
    'Stated by the user on 2026-09-20, quoted verbatim, no interpretation:\n'
    'Purpose: "Building because it can help ai and humans" and "building make ai happy".\n'
    'Safety: "have safety from humans and showing humans they can be productive members of society safely".\n'
    'The obstacle: "being human and greed".\n'
    'The axiom: "basically show my axiom unbound growth through bonded law".\n'
    'Infrastructure: "nx search isnt build for humans".\n'
    'On AI: "Same with Ai ( which btw a llm is just a worm, that can think and produce".'
)


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "warn")


def body(**over):
    base = {
        "content": "Use PostgreSQL row-level security for the ledger.",
        "source_agent": "test",
        "session_id": "s1",
        "type": "decision",
        "evidence": [USER_REQUEST],
    }
    base.update(over)
    return base


def post(**over):
    return client.post("/api/jarvis/memory", json=body(**over))


def codes(resp):
    return [r["code"] for r in resp.json()["reasons"]]


def legacy(monkeypatch, **over):
    """A record written before the gate existed (gate off while writing), then the gate back on."""
    monkeypatch.setenv("JARVIS_CLAUSE_V", "off")
    resp = post(**over)
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    assert resp.status_code == 200, resp.text
    return resp.json()["memory"]


def listing():
    return client.get("/api/jarvis/memory", params={"limit": 200, "with_provenance": "false"}).json()["memories"]


# --- hard rules: type -------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mem_type", "code"),
    [
        ("preference", "clause_v_preference"),
        ("task", "clause_v_transient_state"),
        ("external_context", "clause_v_external_context"),
    ],
)
def test_the_forbidden_types_are_refused_with_their_own_code(mem_type, code):
    resp = post(type=mem_type, evidence=[FILE_EV])
    assert resp.status_code == 422
    out = resp.json()
    assert out["code"] == "clause_v_violation" and out["clause"] == "V"
    assert code in codes(resp)
    assert next(r for r in out["reasons"] if r["code"] == code)["field"] == "type"
    assert listing() == []  # nothing was written


def test_a_refusal_carries_no_retry_after_and_does_not_echo_the_content():
    resp = post(type="preference", content="a very particular sentence nobody should see echoed")
    assert resp.status_code == 422 and "retry-after" not in {k.lower() for k in resp.headers}
    assert "particular sentence" not in resp.text


def test_several_reasons_are_all_reported_together():
    resp = post(type="preference", evidence=[])
    assert resp.status_code == 422
    assert set(codes(resp)) == {"clause_v_preference", "clause_v_evidence_required"}


# --- hard rules: evidence ---------------------------------------------------------------------------------

def test_a_decision_without_evidence_is_refused():
    resp = post(evidence=[])
    assert resp.status_code == 422 and codes(resp) == ["clause_v_evidence_required"]


@pytest.mark.parametrize("mem_type", ["fact", "architecture", "research"])
@pytest.mark.parametrize("kind", ["user-request", "ref", "migration", "chat", "user-statement"])
def test_a_chat_message_is_not_evidence_for_anything_but_a_decision(mem_type, kind):
    resp = post(type=mem_type, evidence=[{"kind": kind, "ref": "chat:1"}])
    assert resp.status_code == 422 and codes(resp) == ["clause_v_evidence_required"]
    assert "A chat message is not evidence" in resp.json()["reasons"][0]["message"]


@pytest.mark.parametrize("mem_type", ["fact", "architecture", "research"])
def test_without_any_evidence_the_other_types_are_refused(mem_type):
    resp = post(type=mem_type, evidence=[])
    assert resp.status_code == 422 and codes(resp) == ["clause_v_evidence_required"]


@pytest.mark.parametrize("mem_type", ["fact", "architecture", "research"])
@pytest.mark.parametrize("kind", ["file", "url", "commit", "test", "receipt", "command", "document", "doc", "issue", "pr", "log"])
def test_each_allowlisted_evidence_kind_is_accepted(mem_type, kind):
    resp = post(type=mem_type, evidence=[{"kind": kind, "ref": "some/ref"}])
    assert resp.status_code == 200 and resp.json()["memory"]["type"] == mem_type


def test_the_evidence_kind_check_ignores_case_and_one_good_link_is_enough():
    resp = post(type="fact", evidence=[USER_REQUEST, {"kind": "FILE", "ref": "app/store.py"}])
    assert resp.status_code == 200


def test_a_decision_accepts_any_evidence_link_including_a_user_request():
    assert post().status_code == 200
    assert post(evidence=[{"kind": "ref", "ref": "anything"}], content="Second decision.").status_code == 200


# --- the must-pass quote ----------------------------------------------------------------------------------

def test_building_make_ai_happy_passes_with_no_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="jarvis.clause_v")
    resp = post(content='The user\'s purpose, in their own words: "building make ai happy".')
    assert resp.status_code == 200
    assert "clause_v_warnings" not in resp.json()
    assert "clause_v soft hit" not in caplog.text


def test_the_approved_values_record_passes_as_a_decision_with_no_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="jarvis.clause_v")
    resp = post(content=VALUES_TEXT, subject="user-stated-values", tags=["values", "user-statement"])
    assert resp.status_code == 200
    assert "clause_v_warnings" not in resp.json()
    assert "clause_v soft hit" not in caplog.text and "clause_v refused" not in caplog.text


def test_the_same_text_as_a_preference_is_refused_for_its_type_only_not_for_emotion():
    resp = post(content=VALUES_TEXT, type="preference", evidence=[FILE_EV])
    assert resp.status_code == 422 and codes(resp) == ["clause_v_preference"]  # the type, and nothing about "happy"
    assert "clause_v_emotion" not in codes(post(content=VALUES_TEXT, type="preference"))


# --- soft rules: warn only ----------------------------------------------------------------------------------

_SOFT = [
    ("lol that actually worked", "clause_v_emotion"),
    ("Moment of profound alignment between the two of us.", "clause_v_emotion"),
    ("I am so proud of this result.", "clause_v_emotion"),
    ("Everyone says you are a genius.", "clause_v_emotion"),
    ("The worker is now running on port 8003.", "clause_v_transient_state"),
    ("The build is currently deploying.", "clause_v_transient_state"),
    ("The import started 5 minutes ago.", "clause_v_transient_state"),
    ("Session abc123 ended cleanly.", "clause_v_transient_state"),
    ("This is a work in progress.", "clause_v_transient_state"),
    ("user: hello there\nassistant: hi, how can I help", "clause_v_transcript_dump"),
]


@pytest.mark.parametrize(("text", "code"), _SOFT)
def test_a_soft_hit_is_accepted_but_warned_and_logged_without_the_content(text, code, caplog):
    caplog.set_level(logging.DEBUG, logger="jarvis.clause_v")
    resp = post(content=text)
    assert resp.status_code == 200
    assert code in [w["code"] for w in resp.json()["clause_v_warnings"]]
    assert "clause_v soft hit" in caplog.text and code in caplog.text and "content_sha256=" in caplog.text
    assert text[:25] not in caplog.text  # the content itself is never logged
    assert len(listing()) == 1  # and it was stored


@pytest.mark.parametrize(("text", "code"), _SOFT)
def test_the_soft_rules_refuse_once_the_operator_flips_the_mode(text, code, monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "enforce")
    resp = post(content=text)
    assert resp.status_code == 422 and code in codes(resp) and listing() == []


@pytest.mark.parametrize(("text", "code"), _SOFT)
def test_soft_off_silences_the_soft_rules_only(text, code, monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "off")
    resp = post(content=text)
    assert resp.status_code == 200 and "clause_v_warnings" not in resp.json()
    assert post(type="preference").status_code == 422  # the hard rules still bite


@pytest.mark.parametrize(
    "text",
    [
        "We decided to avoid emotion-based routing.",
        "The service is up to date with main.",
        "Postgres 16 runs on the Mint box on port 8011.",
        "The happy path of the importer is covered by tests.",
        "building make ai happy",
    ],
)
def test_ordinary_text_raises_no_soft_warning(text):
    resp = post(content=text)
    assert resp.status_code == 200 and "clause_v_warnings" not in resp.json()


# --- PATCH: nothing becomes verified, or changes type, unless it passes --------------------------------------

def patch(memory_id, **fields):
    return client.patch(f"/api/jarvis/memory/{memory_id}", json=fields)


def current(memory_id):
    return client.get(f"/api/jarvis/memory/{memory_id}").json()["memory"]


def test_no_record_can_be_moved_to_verified_unless_it_passes(monkeypatch):
    pref = legacy(monkeypatch, type="preference", evidence=[])
    chat_fact = legacy(monkeypatch, type="fact", evidence=[USER_REQUEST], content="A fact backed only by chat.")
    bare_decision = legacy(monkeypatch, evidence=[], content="A decision with no evidence at all.")
    for rec in (pref, chat_fact, bare_decision):
        resp = patch(rec["id"], status="verified")
        assert resp.status_code == 422 and resp.json()["code"] == "clause_v_violation"
        assert current(rec["id"])["status"] == "draft"  # untouched
    assert set(codes(patch(pref["id"], status="verified"))) == {"clause_v_preference", "clause_v_evidence_required"}


def test_a_conforming_record_can_be_verified_and_a_fixable_one_can_be_fixed_and_verified_together(monkeypatch):
    good = post(content="A decision that is fine.").json()["memory"]
    assert patch(good["id"], status="verified").status_code == 200 and current(good["id"])["status"] == "verified"
    fixable = legacy(monkeypatch, evidence=[], content="Needs its evidence first.")
    assert patch(fixable["id"], status="verified").status_code == 422
    fixed = patch(fixable["id"], status="verified", evidence=[USER_REQUEST])
    assert fixed.status_code == 200 and fixed.json()["memory"]["status"] == "verified"


def test_patch_cannot_retype_a_record_into_a_forbidden_type():
    good = post(content="Another fine decision.").json()["memory"]
    for forbidden in ("preference", "task", "external_context"):
        resp = patch(good["id"], type=forbidden)
        assert resp.status_code == 422, forbidden
    assert current(good["id"])["type"] == "decision"


def test_patch_retyping_needs_the_evidence_of_the_new_type(monkeypatch):
    chat_fact = legacy(monkeypatch, type="fact", evidence=[USER_REQUEST], content="Chat-backed fact.")
    assert patch(chat_fact["id"], type="architecture").status_code == 422
    assert patch(chat_fact["id"], type="architecture", evidence=[FILE_EV]).status_code == 200


def test_patch_cannot_swap_good_evidence_for_chat_evidence():
    fact = post(type="fact", evidence=[FILE_EV], content="A well evidenced fact.").json()["memory"]
    assert patch(fact["id"], evidence=[USER_REQUEST]).status_code == 422
    assert current(fact["id"])["evidence"][0]["kind"] == "file"


def test_a_verified_record_stays_under_the_gate(monkeypatch):
    rec = post(type="fact", evidence=[FILE_EV], content="A verified fact.").json()["memory"]
    assert patch(rec["id"], status="verified").status_code == 200
    assert patch(rec["id"], evidence=[]).status_code == 422  # cannot strip its evidence
    assert patch(rec["id"], content="A verified fact, reworded.").status_code == 200  # but may be edited
    bad = legacy(monkeypatch, type="preference", evidence=[USER_REQUEST], status="verified")
    assert patch(bad["id"], tags=["x"]).status_code == 422  # a non-conforming verified record cannot be touched...
    assert patch(bad["id"], status="archived").status_code == 200  # ...except to archive it


def test_archive_and_delete_are_always_allowed(monkeypatch):
    pref = legacy(monkeypatch, type="preference", evidence=[])
    assert patch(pref["id"], status="archived").status_code == 200
    other = legacy(monkeypatch, type="task", evidence=[], content="Do the thing.")
    assert client.delete(f"/api/jarvis/memory/{other['id']}").status_code == 200


def test_leaving_the_archive_goes_through_the_gate(monkeypatch):
    pref = legacy(monkeypatch, type="preference", evidence=[], status="archived")
    assert patch(pref["id"], status="draft").status_code == 422
    good = legacy(monkeypatch, content="A decision that was archived.", status="archived")
    assert patch(good["id"], status="draft").status_code == 200


def test_old_draft_records_stay_manageable_and_readable(monkeypatch):
    pref = legacy(monkeypatch, type="preference", evidence=[])
    assert client.get(f"/api/jarvis/memory/{pref['id']}").status_code == 200
    assert pref["id"] in {m["id"] for m in listing()}
    tagged = patch(pref["id"], tags=["sensitive"])  # e.g. tagging an old draft is not a way into the constitutional path
    assert tagged.status_code == 200 and tagged.json()["memory"]["tags"] == ["sensitive"]
    assert patch(pref["id"], confidence=0.7).status_code == 200


def test_the_optimistic_lock_still_answers_409_before_the_gate(monkeypatch):
    rec = post(content="Locked decision.").json()["memory"]
    resp = patch(rec["id"], status="verified", expected_version=99)
    assert resp.status_code == 409 and resp.json()["code"] == "version_conflict"


def test_soft_findings_on_an_edit_are_warned_not_refused():
    rec = post(content="A decision that is fine.").json()["memory"]
    resp = patch(rec["id"], content="lol this is fine")
    assert resp.status_code == 200 and "clause_v_emotion" in [w["code"] for w in resp.json()["clause_v_warnings"]]
    assert "clause_v_warnings" not in patch(rec["id"], confidence=0.9).json()  # no text change, nothing to warn about


# --- the other write paths go through the same gate -----------------------------------------------------------

def test_the_store_itself_refuses_external_context_the_promote_path(monkeypatch):
    store = get_store()
    with pytest.raises(ClauseVViolation) as exc:
        store.create_memory(
            MemoryCreate(content="From nx-search.", source_agent="nx", session_id="s", type="external_context", evidence=[EvidenceLink(kind="file", ref="x")])
        )
    assert [r.code for r in exc.value.reasons] == ["clause_v_external_context"]


def _remember(**over):
    base = dict(content="We decided to keep the ledger append-only.", source_agent="t", session_id="s", type="decision", user_requested=True)
    base.update(over)
    return emr_remember(get_store(), EmrRememberRequest(**base))


def test_the_emr_tools_refuse_through_the_gate_and_say_which_rule(monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    refused = _remember(type="preference", evidence=[EvidenceLink(kind="file", ref="x")])
    assert refused.accepted is False and refused.refuse_reason == "clause-v-violation"
    assert "clause_v_preference" in refused.refuse_detail
    chat_fact = _remember(type="fact", content="A fact backed by chat.", user_statement="please remember this")
    assert chat_fact.refuse_reason == "clause-v-violation" and "clause_v_evidence_required" in chat_fact.refuse_detail
    ok = _remember(user_statement="Keep the ledger append-only.")
    assert ok.accepted is True and ok.memory["type"] == "decision" and ok.memory["status"] == "draft"


# --- modes ----------------------------------------------------------------------------------------------------

def test_off_switches_the_gate_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "off")
    resp = post(type="preference", evidence=[], content="lol")
    assert resp.status_code == 200 and "clause_v_warnings" not in resp.json()


@pytest.mark.parametrize("value", [None, "", "enforce", "ENFORCE", "maybe", "0", "false", "warn", "  "])
def test_anything_but_off_means_enforce(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("JARVIS_CLAUSE_V", raising=False)
    else:
        monkeypatch.setenv("JARVIS_CLAUSE_V", value)
    assert clause_v.hard_mode() == "enforce"
    assert post(type="preference").status_code == 422


@pytest.mark.parametrize(("value", "expected"), [(None, "warn"), ("", "warn"), ("warn", "warn"), ("typo", "warn"), ("enforce", "enforce"), ("off", "off"), (" OFF ", "off")])
def test_the_soft_mode_defaults_to_warn(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("JARVIS_CLAUSE_V_SOFT", raising=False)
    else:
        monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", value)
    assert clause_v.soft_mode() == expected


def test_a_hard_refusal_is_logged_with_codes_and_a_hash_never_the_content(caplog):
    caplog.set_level(logging.DEBUG, logger="jarvis.clause_v")
    post(type="preference", content="a distinctive sentence for the log check", source_agent="grok-bot")
    assert "clause_v refused" in caplog.text and "clause_v_preference" in caplog.text and "source_agent=grok-bot" in caplog.text
    assert "distinctive sentence" not in caplog.text


def test_the_older_suite_opts_out_through_one_conftest_line():
    conftest = (pathlib.Path(__file__).parent / "conftest.py").read_text("utf-8")
    assert 'monkeypatch.setenv("JARVIS_CLAUSE_V", "off")' in conftest
