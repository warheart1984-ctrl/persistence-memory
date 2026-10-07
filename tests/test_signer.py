"""The signer: custody refusals, signing against the real service, refusing to sign what it cannot vouch for, and never leaking a key."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import attest, pg_store, signer
from app.main import app
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from tests.attest_support import make_key, requires_ssh_keygen, sha, statement

pytestmark = [requires_ssh_keygen]

KEY = "signer-test-key"
HDR = {"X-API-Key": KEY}
OP = "operator"


# --- a custody directory ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def custody(tmp_path):
    d = tmp_path / "custody"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def mint(custody):
    """The signing key, made the right way (mode 600 in a mode 700 directory)."""
    k = make_key(custody, "sign-key")
    os.chmod(k.path, 0o600)
    return k


NO_MOUNTS = lambda: []  # noqa: E731


def preflight(path, **kw):
    kw.setdefault("mounts", NO_MOUNTS)
    return signer.preflight_key(path, **kw)


def refused(path, **kw):
    with pytest.raises(signer.SignerError) as exc:
        preflight(path, **kw)
    assert exc.value.code == "custody" and exc.value.exit_code == signer.EXIT_CUSTODY
    return exc.value.message


def test_a_key_kept_the_right_way_passes_and_gives_its_public_identity(mint):
    info = preflight(mint.path)
    assert info.public.key_id == mint.key_id and info.path == mint.path


def test_a_missing_key_is_refused(custody):
    assert "no signing key" in refused(custody / "nothing")


def test_a_symlinked_key_is_refused(mint, custody, tmp_path):
    link = custody / "link"
    link.symlink_to(mint.path)
    (custody / "link.pub").write_text(mint.pub_text + "\n")
    assert "symlink" in refused(link)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o660, 0o604, 0o666, 0o700 | 0o004])
def test_a_key_others_can_read_or_write_is_refused(mint, mode):
    os.chmod(mint.path, mode)
    assert "must be 600" in refused(mint.path)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o707, 0o770])
def test_a_key_directory_others_can_enter_is_refused(mint, custody, mode):
    os.chmod(custody, mode)
    assert "mode 700" in refused(mint.path)


def test_a_key_owned_by_someone_else_is_refused(mint, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: os.stat(mint.path).st_uid + 1)
    assert "not owned by the user" in refused(mint.path)


def test_a_key_inside_the_repository_the_deploy_directory_or_the_backups_is_refused(tmp_path, monkeypatch):
    for place in (signer.repo_root() / ".custody-test", signer.repo_root() / "deploy" / ".custody-test"):
        place.mkdir(mode=0o700, exist_ok=True)
        try:
            k = make_key(place, "k")
            os.chmod(k.path, 0o600)
            assert "inside" in refused(k.path)
        finally:
            for f in place.iterdir():
                f.unlink()
            place.rmdir()
    backups = tmp_path / "bk"
    backups.mkdir(mode=0o700)
    monkeypatch.setenv("JARVIS_BACKUP_DIR", str(backups))
    k = make_key(backups, "k")
    os.chmod(k.path, 0o600)
    assert "must never be in the repository" in refused(k.path)


def test_a_key_under_docker_storage_or_an_extra_forbidden_place_is_refused(mint, custody):
    assert "inside" in refused(mint.path, forbidden=[custody.parent])
    assert refused(mint.path, forbidden=[Path(str(mint.path))])


def test_a_key_whose_directory_a_running_container_mounts_is_refused(mint, custody):
    assert "never enter a container" in refused(mint.path, mounts=lambda: [custody])
    assert "never enter a container" in refused(mint.path, mounts=lambda: [custody.parent])  # a parent of the key's directory
    assert "never enter a container" in refused(mint.path, mounts=lambda: [mint.path])  # the file itself
    preflight(mint.path, mounts=lambda: [Path("/var/lib/docker/volumes/pgdata/_data"), Path("/tmp/somewhere/else")])  # unrelated mounts are fine


def test_a_public_key_that_is_not_this_keys_is_refused(mint, custody):
    other = make_key(custody, "other")
    Path(str(mint.path) + ".pub").write_text(other.pub_text + "\n")
    assert "not the public half" in refused(mint.path)


def test_a_missing_public_key_is_refused(mint):
    Path(str(mint.path) + ".pub").unlink()
    assert "cannot read the public key" in refused(mint.path)


def test_a_passphrase_protected_key_is_refused(custody):
    path = custody / "locked"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "a passphrase", "-f", str(path)], check=True, capture_output=True)
    os.chmod(path, 0o600)
    assert "without a passphrase" in refused(path)


def test_an_rsa_key_is_refused(custody):
    path = custody / "rsa"
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(path)], check=True, capture_output=True)
    os.chmod(path, 0o600)
    assert "Ed25519" in refused(path) or "only ssh-ed25519" in refused(path)


# --- creating the key ---------------------------------------------------------------------------------------------------------------------

def test_init_key_creates_a_key_that_passes_preflight_with_the_right_modes(tmp_path, monkeypatch):
    path = tmp_path / "newcustody" / "k"
    monkeypatch.setattr(signer, "container_mount_sources", NO_MOUNTS)
    info = signer.init_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert info["key_id"].startswith("SHA256:") and info["public_key"].startswith("ssh-ed25519 ") and "PRIVATE" not in json.dumps(info)
    signer.preflight_key(path, mounts=NO_MOUNTS)


def test_init_key_never_overwrites_a_key(mint):
    with pytest.raises(signer.SignerError) as exc:
        signer.init_key(mint.path)
    assert exc.value.exit_code == signer.EXIT_CUSTODY and "refusing to overwrite" in exc.value.message
    assert mint.path.read_bytes()  # untouched


def test_init_key_refuses_a_forbidden_place(tmp_path):
    with pytest.raises(signer.SignerError) as exc:
        signer.init_key(signer.repo_root() / "oops" / "k")
    assert "never be there" in exc.value.message and not (signer.repo_root() / "oops").exists()


# --- signing against the real service -----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("signerkeys")
    out = {n: make_key(d, n) for n in ("root", "stranger")}
    c = d / "custody"
    c.mkdir(mode=0o700)
    out["mint"] = make_key(c, "mint")
    out["mint2"] = make_key(c, "mint2")
    for k in (out["mint"], out["mint2"]):
        os.chmod(k.path, 0o600)
    return out


@pytest.fixture
def pg(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def client(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def roots(keys, tmp_path, monkeypatch):
    f = tmp_path / "roots.pub"
    f.write_text(keys["root"].pub_text + "\n")
    monkeypatch.setenv(attest.ROOTS_ENV, str(f))
    return f


@pytest.fixture
def api(client):
    def transport(method, path, body):
        r = client.request(method, path, headers=HDR, json=body)
        return r.status_code, (r.json() if r.content else {})
    return signer.Api("http://test", "unused", transport)


def key_info(k):
    return signer.KeyInfo(path=k.path, public=k.public)


def seal(client, n=6, size=3):
    for i in range(n):
        assert client.post("/api/jarvis/memory", headers=HDR, json={"content": f"signer record {i} {os.urandom(4).hex()}", "source_agent": "t", "session_id": "s", "type": "fact"}).status_code == 200
    assert client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True, "max_entries": size}).status_code == 200


def authorize(client, keys, key="mint", from_seq=1):
    h = client.get("/api/jarvis/attestations/head", headers=HDR).json()
    s = statement(keys["root"], "key", h["next_stmt_seq"], h["trust_head_hash"], key=keys[key], arg=from_seq, tenant=OP)
    r = client.post("/api/jarvis/trust/statements", headers=HDR, json={"kind": "key", "key_id": s.key_id, "pubkey": s.pubkey, "arg": s.arg, "stmt_seq": s.stmt_seq,
                                                                       "prev_hash": s.prev_hash, "signed_by": s.signed_by, "signature": s.signature})
    assert r.status_code == 200, r.text


def revoke(client, keys, key="mint", cutoff=0):
    h = client.get("/api/jarvis/attestations/head", headers=HDR).json()
    s = statement(keys["root"], "revoke", h["next_stmt_seq"], h["trust_head_hash"], key_id=keys[key].key_id, arg=cutoff, tenant=OP)
    r = client.post("/api/jarvis/trust/statements", headers=HDR, json={"kind": "revoke", "key_id": s.key_id, "arg": s.arg, "stmt_seq": s.stmt_seq,
                                                                       "prev_hash": s.prev_hash, "signed_by": s.signed_by, "signature": s.signature})
    assert r.status_code == 200, r.text


def run(api, keys, key="mint", **kw):
    kw.setdefault("verify_block", lambda h, bh: None)
    return signer.run_sign(api, key_info(keys[key]), **kw)


def ok(client):
    return client.get("/api/jarvis/attestations/verify", headers=HDR).json()


@pytest.mark.postgres
def test_it_signs_every_pending_block_then_one_checkpoint_and_the_service_verifies_all_of_it(client, roots, keys, api):
    seal(client, 9)  # three blocks
    authorize(client, keys)
    calls = []
    result = run(api, keys, verify_block=lambda h, bh: calls.append((h, bh)))
    assert [b["height"] for b in result["signed_blocks"]] == [1, 2, 3] and [b["signer_seq"] for b in result["signed_blocks"]] == [1, 2, 3]
    assert result["checkpoint"]["signer_seq"] == 4 and result["checkpoint"]["covers"] == 3 and result["checkpoint"]["tip_height"] == 3
    assert [c[0] for c in calls] == [3]  # the independent check ran once, on the newest block (it covers the earlier ones)
    v = ok(client)
    assert v["ok"] is True and v["summary"]["blocks_signed"] == 3 and v["summary"]["newest_checkpoint_seq"] == 4 and v["problems"] == []
    assert client.get("/api/jarvis/attestations/pending", headers=HDR).json()["blocks"] == []


@pytest.mark.postgres
def test_a_second_run_with_nothing_new_signs_nothing(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    run(api, keys)
    again = run(api, keys)
    assert again["signed_blocks"] == [] and again["checkpoint"] is None and again["note"] == "nothing pending"
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 3  # two blocks and one checkpoint, nothing more


@pytest.mark.postgres
def test_new_blocks_extend_the_same_chain_with_a_new_checkpoint(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    run(api, keys)
    seal(client, 3)
    result = run(api, keys)
    assert [b["height"] for b in result["signed_blocks"]] == [3] and result["signed_blocks"][0]["signer_seq"] == 4 and result["checkpoint"]["signer_seq"] == 5
    rows = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"]
    assert [r["kind"] for r in rows] == ["block", "block", "checkpoint", "block", "checkpoint"]
    assert all(rows[i]["prev_hash"] == rows[i - 1]["attestation_hash"] for i in range(1, 5))
    assert ok(client)["ok"] is True


@pytest.mark.postgres
def test_a_dry_run_changes_nothing_and_does_not_run_the_independent_check(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    called = []
    result = run(api, keys, dry_run=True, verify_block=lambda h, bh: called.append(h))
    assert result["planned_blocks"] == [1, 2] and result["signed_blocks"] == [] and called == []
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 0


@pytest.mark.postgres
def test_if_the_independent_check_rejects_the_block_nothing_is_signed(client, roots, keys, api):
    seal(client)
    authorize(client, keys)

    def reject(h, bh):
        raise signer.SignerError("pre_sign_verify_failed", "the offline replay does not accept block 2", signer.EXIT_UNHEALTHY)

    with pytest.raises(signer.SignerError) as exc:
        run(api, keys, verify_block=reject)
    assert exc.value.code == "pre_sign_verify_failed" and exc.value.exit_code == signer.EXIT_UNHEALTHY
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 0


@pytest.mark.postgres
def test_it_refuses_a_key_no_root_authorized_a_revoked_key_and_a_key_not_yet_valid(client, roots, keys, api):
    seal(client)
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "key_not_authorized"
    authorize(client, keys, from_seq=5)
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "key_not_yet_valid"


@pytest.mark.postgres
def test_it_refuses_a_revoked_key(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    revoke(client, keys, cutoff=0)
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "key_revoked"


@pytest.mark.postgres
def test_it_refuses_when_the_service_has_no_trust_root(client, roots, keys, api, monkeypatch):
    seal(client)
    authorize(client, keys)
    monkeypatch.delenv(attest.ROOTS_ENV)
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "no_trust_root"


@pytest.mark.postgres
def test_it_will_not_extend_a_log_that_already_has_a_problem(client, roots, keys, api, pg):
    seal(client, 9)
    authorize(client, keys)
    run(api, keys)
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE attestations DISABLE TRIGGER attestations_no_update")
        conn.execute("UPDATE attestations SET subject_hash = %s WHERE signer_seq = 1", (sha("tampered"),))
        conn.execute("ALTER TABLE attestations ENABLE TRIGGER attestations_no_update")
    seal(client, 3)
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "log_unhealthy" and exc.value.exit_code == signer.EXIT_UNHEALTHY
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 4  # nothing was added


@pytest.mark.postgres
def test_a_hostile_service_cannot_make_it_sign_a_block_hash_that_is_not_the_blocks(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    real = api._transport

    def lying(method, path, body=None):
        status, obj = real(method, path, body)
        if path == "/api/jarvis/attestations/pending":
            obj["blocks"][0]["block_hash"] = sha("a different block")  # the pending list lies about block 1's hash
        return status, obj

    api._transport = lying  # type: ignore[assignment]
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "block_inconsistent"
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 0


@pytest.mark.postgres
def test_a_hostile_service_cannot_make_it_sign_altered_block_fields(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    real = api._transport

    def lying(method, path, body=None):
        status, obj = real(method, path, body)
        if path == "/api/jarvis/blocks/1":
            obj["block"]["entries_root"] = sha("not the root")
        return status, obj

    api._transport = lying  # type: ignore[assignment]
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "block_inconsistent"


@pytest.mark.postgres
def test_the_messages_it_signs_are_built_from_the_chain_head_it_was_given_and_the_service_rechecks_them(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    real = api._transport

    def lying(method, path, body=None):
        status, obj = real(method, path, body)
        if path == "/api/jarvis/attestations/pending":
            obj["head"]["prev_hash"] = sha("a forked head")
        return status, obj

    api._transport = lying  # type: ignore[assignment]
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "store_refused" and exc.value.exit_code == signer.EXIT_API and "409" in exc.value.message
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 0


@pytest.mark.postgres
def test_a_signature_that_does_not_verify_is_never_posted(client, roots, keys, api, monkeypatch):
    seal(client)
    authorize(client, keys)
    posted = []
    real = api._transport
    api._transport = lambda m, p, b=None: (posted.append(p) if m == "POST" else None) or real(m, p, b)  # type: ignore[assignment]

    class Shim:  # only the signer's own check is broken; the service in the same process still verifies normally
        def __getattr__(self, name):
            return getattr(attest, name)

        @staticmethod
        def verify_sshsig(*a, **k):
            raise attest.SignatureInvalid("the signature does not match the message")

    monkeypatch.setattr(signer, "attest", Shim())
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "self_check_failed" and posted == []


@pytest.mark.postgres
def test_a_second_key_after_a_rotation_continues_the_chain(client, roots, keys, api):
    seal(client)
    authorize(client, keys)
    run(api, keys)
    seal(client, 3)
    authorize(client, keys, key="mint2", from_seq=4)
    revoke(client, keys, key="mint", cutoff=3)
    result = run(api, keys, key="mint2")
    assert result["signed_blocks"][0]["signer_seq"] == 4
    v = ok(client)
    assert v["ok"] is True and v["summary"]["blocks_signed"] == 3 and v["summary"]["keys_authorized"] == 2


@pytest.mark.postgres
def test_the_status_reports_the_key_the_pending_work_and_no_secret(client, roots, keys, api):
    seal(client)
    st = signer.status(api, key_info(keys["mint"]))
    assert st["authorized"] is False and st["pending_blocks"] == [1, 2] and st["trust_roots_configured"] is True and st["next_signer_seq"] == 1
    authorize(client, keys)
    assert signer.status(api, key_info(keys["mint"]))["authorized"] is True
    assert "PRIVATE" not in json.dumps(st)


# --- the command line -------------------------------------------------------------------------------------------------------------------------

def test_the_command_line_refuses_a_loose_key_with_exit_3_and_prints_no_key_material(mint, capsys, monkeypatch):
    os.chmod(mint.path, 0o644)
    monkeypatch.setenv("JARVIS_SIGN_KEY", str(mint.path))
    monkeypatch.setattr(signer, "container_mount_sources", NO_MOUNTS)
    assert signer.main(["status"]) == signer.EXIT_CUSTODY
    out = capsys.readouterr()
    assert "must be 600" in out.err and "PRIVATE" not in out.out + out.err
    body = [l for l in mint.path.read_text().splitlines() if l and "-----" not in l][0]
    assert body not in out.out + out.err


def test_the_command_line_init_key_prints_only_public_material(tmp_path, capsys, monkeypatch):
    path = tmp_path / "c" / "k"
    monkeypatch.setenv("JARVIS_SIGN_KEY", str(path))
    monkeypatch.setattr(signer, "container_mount_sources", NO_MOUNTS)
    assert signer.main(["init-key"]) == signer.EXIT_OK
    out = capsys.readouterr().out
    assert "key id: SHA256:" in out and "ssh-ed25519 " in out and "PRIVATE" not in out
    body = [l for l in path.read_text().splitlines() if l and "-----" not in l][0]
    assert body not in out
    assert signer.main(["init-key"]) == signer.EXIT_CUSTODY


def test_the_default_key_lives_under_the_home_of_the_ledger_and_outside_every_forbidden_place(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_SIGN_KEY", raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    p = signer.default_key_path()
    assert p == tmp_path / "keys" / "jarvis-sign-ed25519"
    assert not any(signer._inside(p, r) for r in signer.forbidden_roots())


def test_the_signer_runs_without_the_applications_dependencies():
    """It must work on the host with the standard library and ssh-keygen only: importing it must not pull in the web or database stack."""
    code = ("import sys\nfor m in ('cryptography','psycopg','fastapi','pydantic','uvicorn'): sys.modules[m] = None\n"
            "import app.signer as s, app.attest, app.blocks\nprint('imported without the application stack:', s.EXIT_OK)")
    r = subprocess.run([sys.executable, "-c", code], cwd=signer.repo_root(), capture_output=True, text=True)
    assert r.returncode == 0 and "imported without" in r.stdout, r.stderr


import sys  # noqa: E402  (used by the last test)
