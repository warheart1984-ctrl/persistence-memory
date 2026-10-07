"""Test helpers for signatures: real OpenSSH keys and signatures made with ssh-keygen (so the Python verifier is checked against
the real tool, not against itself).  Test code only: the product never signs."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from app import attest

NOW = "2026-10-07T12:00:00Z"
TENANT = "alice"


def have_ssh_keygen() -> bool:
    return shutil.which("ssh-keygen") is not None


requires_ssh_keygen = pytest.mark.skipif(not have_ssh_keygen(), reason="ssh-keygen not installed")


@dataclass
class Key:
    path: Path
    public: attest.PublicKey

    @property
    def key_id(self) -> str:
        return self.public.key_id

    @property
    def pub_text(self) -> str:
        return self.public.text()


def make_key(directory: Path, name: str, key_type: str = "ed25519") -> Key:
    path = Path(directory) / name
    subprocess.run(["ssh-keygen", "-q", "-t", key_type, "-N", "", "-C", name, "-f", str(path)], check=True, capture_output=True)
    text = (Path(str(path) + ".pub")).read_text()
    return Key(path=path, public=attest.parse_public_key(text) if key_type == "ed25519" else None)  # type: ignore[arg-type]


def sign(key: Key | Path, message: str | bytes, namespace: str = attest.NAMESPACE, hash_alg: str | None = None) -> str:
    """An armored SSHSIG over the message, from the real ssh-keygen."""
    data = message.encode("utf-8") if isinstance(message, str) else message
    path = key.path if isinstance(key, Key) else key
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "msg"
        f.write_bytes(data)
        cmd = ["ssh-keygen", "-Y", "sign", "-f", str(path), "-n", namespace]
        if hash_alg:
            cmd += ["-O", f"hashalg={hash_alg}"]
        subprocess.run(cmd + [str(f)], check=True, capture_output=True)
        return (Path(tmp) / "msg.sig").read_text()


def ssh_verify(key: Key, message: str | bytes, signature: str, namespace: str = attest.NAMESPACE) -> bool:
    """Ask the real ssh-keygen whether the signature is good."""
    data = message.encode("utf-8") if isinstance(message, str) else message
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "allowed").write_text(f"signer {key.pub_text}\n")
        (t / "sig").write_text(signature)
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(t / "allowed"), "-I", "signer", "-n", namespace, "-s", str(t / "sig")],
                           input=data, capture_output=True)
        return r.returncode == 0


def roots_of(*keys: Key) -> dict[str, attest.PublicKey]:
    return {k.key_id: k.public for k in keys}


def statement(root: Key, kind: str, stmt_seq: int, prev_hash: str, *, key: Key | None = None, key_id: str | None = None, arg: int | None = None,
              subject_hash: str | None = None, tenant: str = TENANT) -> attest.Statement:
    """A correctly signed trust statement (tests then break it on purpose)."""
    kid = key_id or (key.key_id if key else root.key_id)
    message = attest.trust_message(kind, tenant, kid, arg, subject_hash, stmt_seq, prev_hash)
    sig = attest.normalize_signature(sign(root, message))
    return attest.Statement(stmt_seq=stmt_seq, kind=kind, key_id=kid, pubkey=key.pub_text if key and kind in ("key", "root_add") else None,
                            arg=arg, subject_hash=subject_hash, prev_hash=prev_hash, signed_by=root.key_id, signature=sig,
                            statement_hash=attest.statement_hash(message, root.key_id, sig))


def trust_chain(root: Key, *steps: tuple, tenant: str = TENANT) -> list[attest.Statement]:
    """Statements chained from the start; each step is (kind, kwargs) signed by ``root`` unless kwargs holds 'by'."""
    out: list[attest.Statement] = []
    prev = attest.GENESIS
    for kind, kw in steps:
        kw = dict(kw)
        by = kw.pop("by", root)
        s = statement(by, kind, len(out) + 1, prev, tenant=tenant, **kw)
        out.append(s)
        prev = s.statement_hash
    return out


def attestation(signer: Key, kind: str, subject: str, subject_hash: str, signer_seq: int, prev_hash: str, *, signed_at: str = NOW,
                tenant: str = TENANT, key_id: str | None = None) -> attest.Attestation:
    message = attest.attestation_message(kind, tenant, subject, subject_hash, signer_seq, prev_hash, signed_at)
    sig = attest.normalize_signature(sign(signer, message))
    kid = key_id or signer.key_id
    return attest.Attestation(signer_seq=signer_seq, kind=kind, subject=subject, subject_hash=subject_hash, prev_hash=prev_hash, key_id=kid,
                              signed_at=signed_at, signature=sig, attestation_hash=attest.attestation_hash(message, kid, sig))


def block_attestation(signer: Key, height: int, block_hash: str, seq: int, prev: str, **kw) -> attest.Attestation:
    return attestation(signer, "block", f"block:{height}", block_hash, seq, prev, **kw)


def checkpoint_attestation(signer: Key, seq: int, prev_rows: list[attest.Attestation], tip_height: int, tip_hash: str, tenant: str = TENANT, **kw) -> attest.Attestation:
    covered_head = prev_rows[-1].attestation_hash if prev_rows else attest.GENESIS
    subject = attest.checkpoint_subject(seq - 1, covered_head, tip_height, tip_hash)
    return attestation(signer, "checkpoint", subject, attest.checkpoint_hash(tenant, seq - 1, covered_head, tip_height, tip_hash), seq, covered_head, tenant=tenant, **kw)


def chain(signer: Key, items: list[tuple[str, str, str]], tenant: str = TENANT) -> list[attest.Attestation]:
    """Attestations in a row: items are (kind, subject, subject_hash); each links to the one before."""
    rows: list[attest.Attestation] = []
    for kind, subject, subject_hash in items:
        prev = rows[-1].attestation_hash if rows else attest.GENESIS
        rows.append(attestation(signer, kind, subject, subject_hash, len(rows) + 1, prev, tenant=tenant))
    return rows


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
