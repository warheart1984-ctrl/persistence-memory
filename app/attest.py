"""Signatures, verification side (PR A): attestations of blocks, replay receipts and checkpoints, and the trust statements
that say which keys may sign.  Nothing in this module signs anything, and nothing holds a private key.

* A **signature** is an OpenSSH one (``ssh-keygen -Y sign -n jarvis-ledger-v1``, the SSHSIG format), Ed25519 only for now.  It is
  verified here in Python (``cryptography`` Ed25519, already installed through PyJWT[crypto]); where that package is missing (the
  signer on the host, the witness on the PC) the same check is delegated to ``ssh-keygen -Y verify``.  The tests cross-check both
  against each other.  Hardware (``sk-``) keys are refused until their extra signed fields are supported.
* An **attestation** signs a domain-separated message built from digests the ledger already has (a block hash, a receipt's evidence
  id, a checkpoint over the signing log), never from re-encoded JSON.  Attestations form a log: a gapless ``signer_seq`` and the hash
  of the previous attestation, so a removed or forked entry is visible.
* **Trust statements** form a second log, signed only by a *root* key (pinned from outside the database): authorize a signing key
  from a signer_seq, revoke it with a cutoff, add another root, cosign a checkpoint, void one bad attestation (a row parked in the
  log by someone who cannot sign cannot be deleted, so a root marks it void instead).  A compromised signing key cannot authorize
  its own successor.

What a signature says: this key (authorized by a root) attested this digest.  It does not say the content is true, who wrote it,
or when (``signed_at`` is the signer's own claim; order is ``signer_seq`` and the chain).  See docs/SIGNATURES.md.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import subprocess
import tempfile

NAMESPACE = "jarvis-ledger-v1"
GENESIS = "0" * 64
ATTEST_KINDS = ("block", "receipt", "checkpoint")
TRUST_KINDS = ("key", "revoke", "root_add", "cosign", "void")
ROOTS_ENV = "JARVIS_TRUST_ROOTS_FILE"
MODE_ENV = "JARVIS_SIGNATURES"
GRACE_ENV = "JARVIS_SIGNATURE_GRACE_HOURS"
DEFAULT_GRACE_HOURS = 2.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_SIGNED_AT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_ARMOR_END = "-----END SSH SIGNATURE-----"


class AttestError(Exception):
    """A refusal with a stable code; ``status`` is the HTTP status the API maps it to."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class SignatureInvalid(Exception):
    """The signature does not verify (or is not a signature we accept)."""


Problem = dict[str, str]


def _problem(check: str, subject: str, message: str) -> Problem:
    return {"check": check, "subject": subject, "problem": message}


# --- OpenSSH wire format ---------------------------------------------------------------------------------------------

def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _read_string(buf: bytes, off: int) -> tuple[bytes, int]:
    if off + 4 > len(buf):
        raise ValueError("truncated")
    (n,) = struct.unpack_from(">I", buf, off)
    off += 4
    if off + n > len(buf):
        raise ValueError("truncated")
    return buf[off:off + n], off + n


def fingerprint(blob: bytes) -> str:
    """The OpenSSH SHA256 fingerprint of a public key blob (what ``ssh-keygen -lf`` prints): the key's id everywhere here."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class PublicKey:
    blob: bytes
    raw: bytes
    key_id: str
    comment: str = ""

    def text(self) -> str:
        return "ssh-ed25519 " + base64.b64encode(self.blob).decode() + (f" {self.comment}" if self.comment else "")


def public_key_from_blob(blob: bytes, comment: str = "") -> PublicKey:
    try:
        kind, off = _read_string(blob, 0)
        raw, off = _read_string(blob, off)
    except ValueError as exc:
        raise AttestError("key_malformed", "the public key blob is truncated") from exc
    if kind != b"ssh-ed25519":
        raise AttestError("key_unsupported", f"only ssh-ed25519 keys are supported, not {kind.decode('ascii', 'replace')} (hardware keys come later)")
    if len(raw) != 32 or off != len(blob):
        raise AttestError("key_malformed", "the Ed25519 public key must be exactly 32 bytes")
    return PublicKey(blob=blob, raw=raw, key_id=fingerprint(blob), comment=comment)


def parse_public_key(text: str) -> PublicKey:
    """An OpenSSH public key line, ``ssh-ed25519 AAAA... [comment]``."""
    parts = (text or "").strip().split(None, 2)
    if len(parts) < 2:
        raise AttestError("key_malformed", "a public key looks like: ssh-ed25519 AAAA... comment")
    if parts[0] != "ssh-ed25519":
        raise AttestError("key_unsupported", f"only ssh-ed25519 keys are supported, not {parts[0]} (hardware keys come later)")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestError("key_malformed", "the public key is not valid base64") from exc
    return public_key_from_blob(blob, parts[2] if len(parts) > 2 else "")


def load_roots(path: str | os.PathLike | None = None) -> dict[str, PublicKey]:
    """The pinned root public keys (one OpenSSH line each; ``#`` comments).  Empty when nothing is configured.

    These come from outside the database on purpose: a root cannot be added by whoever can write the ledger."""
    p = path or os.getenv(ROOTS_ENV)
    if not p:
        return {}
    roots: dict[str, PublicKey] = {}
    try:
        text = Path(p).read_text("utf-8")
    except OSError as exc:
        raise AttestError("trust_roots_unreadable", f"cannot read the trust roots file: {exc.strerror or exc}") from exc
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            key = parse_public_key(line)
        except AttestError as exc:
            raise AttestError(exc.code, f"trust roots file line {number}: {exc.message}") from exc
        roots[key.key_id] = key
    return roots


@dataclass(frozen=True)
class ParsedSignature:
    key: PublicKey
    namespace: str
    hash_algorithm: str
    signature: bytes


def normalize_signature(armored: str) -> str:
    """One canonical text for a stored signature: LF line ends, no surrounding blanks."""
    return (armored or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def parse_sshsig(armored: str) -> ParsedSignature:
    text = normalize_signature(armored)
    lines = text.split("\n")
    if len(lines) < 3 or lines[0] != _ARMOR_BEGIN or lines[-1] != _ARMOR_END:
        raise SignatureInvalid("not an OpenSSH signature (missing the BEGIN/END SSH SIGNATURE lines)")
    try:
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureInvalid("the signature body is not valid base64") from exc
    if not blob.startswith(b"SSHSIG"):
        raise SignatureInvalid("the signature does not start with SSHSIG")
    try:
        off = 6
        (version,) = struct.unpack_from(">I", blob, off)
        off += 4
        if version != 1:
            raise SignatureInvalid(f"unsupported SSHSIG version {version}")
        pub, off = _read_string(blob, off)
        namespace, off = _read_string(blob, off)
        reserved, off = _read_string(blob, off)
        algorithm, off = _read_string(blob, off)
        sig, off = _read_string(blob, off)
    except (struct.error, ValueError) as exc:
        raise SignatureInvalid("the signature is truncated") from exc
    if off != len(blob) or reserved:
        raise SignatureInvalid("the signature has unexpected trailing or reserved data")
    try:
        key = public_key_from_blob(pub)
    except AttestError as exc:
        raise SignatureInvalid(exc.message) from exc
    try:
        sig_type, o2 = _read_string(sig, 0)
        sig_raw, o2 = _read_string(sig, o2)
    except ValueError as exc:
        raise SignatureInvalid("the signature blob is truncated") from exc
    if sig_type != b"ssh-ed25519" or len(sig_raw) != 64 or o2 != len(sig):
        raise SignatureInvalid("the signature is not a plain Ed25519 signature")
    return ParsedSignature(key=key, namespace=namespace.decode("utf-8", "replace"), hash_algorithm=algorithm.decode("ascii", "replace"), signature=sig_raw)


def verify_sshsig(armored: str, message: bytes, *, namespace: str = NAMESPACE) -> PublicKey:
    """Check an SSHSIG over ``message``; returns the signing key.  Raises SignatureInvalid, never returns False."""
    parsed = parse_sshsig(armored)
    if parsed.namespace != namespace:
        raise SignatureInvalid(f"the signature is for namespace {parsed.namespace!r}, not {namespace!r}")
    if parsed.hash_algorithm == "sha512":
        digest = hashlib.sha512(message).digest()
    elif parsed.hash_algorithm == "sha256":
        digest = hashlib.sha256(message).digest()
    else:
        raise SignatureInvalid(f"unsupported hash algorithm {parsed.hash_algorithm!r}")
    signed = b"SSHSIG" + _string(parsed.namespace.encode()) + _string(b"") + _string(parsed.hash_algorithm.encode()) + _string(digest)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        _verify_with_ssh_keygen(armored, message, namespace, parsed.key)
        return parsed.key
    try:
        Ed25519PublicKey.from_public_bytes(parsed.key.raw).verify(parsed.signature, signed)
    except InvalidSignature as exc:
        raise SignatureInvalid("the signature does not match the message") from exc
    return parsed.key


def _verify_with_ssh_keygen(armored: str, message: bytes, namespace: str, key: PublicKey) -> None:
    """The fallback where ``cryptography`` is not installed: ask the real tool whether the signature is good for the key it names."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "allowed").write_text(f"signer {key.text()}\n")
        (t / "sig").write_text(normalize_signature(armored) + "\n")
        try:
            r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(t / "allowed"), "-I", "signer", "-n", namespace, "-s", str(t / "sig")],
                               input=message, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SignatureInvalid("cannot verify: the cryptography package is not installed and ssh-keygen could not be run") from exc
    if r.returncode != 0:
        raise SignatureInvalid("the signature does not match the message")


# --- the messages and hashes -----------------------------------------------------------------------------------------------

def _tenant_part(tenant: str) -> str:
    return f"{len(tenant.encode('utf-8'))}:{tenant}"


def _clean(value: str, what: str) -> str:
    if "|" in value or "\n" in value or "\r" in value:
        raise AttestError("field_invalid", f"{what} cannot contain | or a line break")
    return value


def attestation_message(kind: str, tenant: str, subject: str, subject_hash: str, signer_seq: int, prev_hash: str, signed_at: str) -> str:
    """``jarvis-attest|v1|kind|len:tenant|subject|subject_hash|signer_seq|prev_attestation_hash|signed_at`` (the SQL function
    ``jarvis_attestation_message`` builds the same string)."""
    if kind not in ATTEST_KINDS:
        raise AttestError("attestation_kind_unknown", f"an attestation is one of {', '.join(ATTEST_KINDS)}")
    if not _HEX64.match(subject_hash) or not _HEX64.match(prev_hash):
        raise AttestError("field_invalid", "subject_hash and prev_hash must be 64 lowercase hex characters")
    if not _SIGNED_AT.match(signed_at):
        raise AttestError("field_invalid", "signed_at looks like 2026-10-07T12:00:00Z")
    return f"jarvis-attest|v1|{kind}|{_tenant_part(tenant)}|{_clean(subject, 'subject')}|{subject_hash}|{int(signer_seq)}|{prev_hash}|{signed_at}"


def attestation_hash(message: str, key_id: str, signature: str) -> str:
    return hashlib.sha256(f"{message}\n{key_id}\n{normalize_signature(signature)}".encode("utf-8")).hexdigest()


def trust_message(kind: str, tenant: str, key_id: str, arg: int | None, subject_hash: str | None, stmt_seq: int, prev_hash: str) -> str:
    """``jarvis-trust|v1|kind|len:tenant|key_id|arg|subject_hash|stmt_seq|prev_statement_hash`` (arg and subject_hash may be empty)."""
    if kind not in TRUST_KINDS:
        raise AttestError("trust_kind_unknown", f"a trust statement is one of {', '.join(TRUST_KINDS)}")
    if subject_hash and not _HEX64.match(subject_hash):
        raise AttestError("field_invalid", "subject_hash must be 64 lowercase hex characters")
    return (f"jarvis-trust|v1|{kind}|{_tenant_part(tenant)}|{key_id}|{'' if arg is None else int(arg)}|{subject_hash or ''}|{int(stmt_seq)}|{prev_hash}")


def statement_hash(message: str, signed_by: str, signature: str) -> str:
    return hashlib.sha256(f"{message}\n{signed_by}\n{normalize_signature(signature)}".encode("utf-8")).hexdigest()


def checkpoint_subject(covered_seq: int, covered_head: str, tip_height: int, tip_hash: str) -> str:
    return f"checkpoint:{int(covered_seq)}:{covered_head}:{int(tip_height)}:{tip_hash}"


def checkpoint_hash(tenant: str, covered_seq: int, covered_head: str, tip_height: int, tip_hash: str) -> str:
    """The digest a checkpoint attestation signs: the signing log up to ``covered_seq`` (its head hash) and the newest sealed block
    (height 0 and 64 zeros when none).  Only facts that stay verifiable later."""
    text = f"jarvis-checkpoint|v1|{_tenant_part(tenant)}|{int(covered_seq)}|{covered_head}|{int(tip_height)}|{tip_hash}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_checkpoint_subject(subject: str) -> tuple[int, str, int, str] | None:
    parts = subject.split(":")
    if len(parts) != 5 or parts[0] != "checkpoint" or not parts[1].isdigit() or not parts[3].isdigit():
        return None
    if not _HEX64.match(parts[2]) or not _HEX64.match(parts[4]):
        return None
    return int(parts[1]), parts[2], int(parts[3]), parts[4]


def parse_block_subject(subject: str) -> int | None:
    m = re.fullmatch(r"block:([1-9][0-9]*)", subject)
    return int(m.group(1)) if m else None


# --- the trust log: who may sign ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Statement:
    stmt_seq: int
    kind: str
    key_id: str
    pubkey: str | None
    arg: int | None
    subject_hash: str | None
    prev_hash: str
    signed_by: str
    signature: str
    statement_hash: str


@dataclass
class KeyAuth:
    key: PublicKey
    from_seq: int
    stmt_seq: int
    cutoff: int | None = None  # attestations with a larger signer_seq are not valid


@dataclass
class Cosign:
    checkpoint_seq: int
    checkpoint_hash: str  # the checkpoint attestation's attestation_hash
    root_id: str
    stmt_seq: int


@dataclass
class TrustState:
    roots: dict[str, PublicKey] = field(default_factory=dict)
    keys: dict[str, KeyAuth] = field(default_factory=dict)
    cosigns: list[Cosign] = field(default_factory=list)
    voids: dict[int, str] = field(default_factory=dict)  # signer_seq -> the attestation hash the root voided
    problems: list[Problem] = field(default_factory=list)
    head_seq: int = 0
    head_hash: str = GENESIS


def evaluate_trust(tenant: str, pinned_roots: dict[str, PublicKey], statements: list[Statement]) -> TrustState:
    """Replay the trust log.  A statement counts only if its position, its chain link, its hash and its root signature all hold;
    each one that fails is reported and ignored, and the log carries on from the stored hash so one break is one problem."""
    st = TrustState(roots=dict(pinned_roots))
    expected_seq, last = 1, GENESIS
    for s in sorted(statements, key=lambda x: x.stmt_seq):
        who = f"trust statement {s.stmt_seq}"
        bad: list[str] = []
        if s.stmt_seq != expected_seq:
            bad.append(f"expected statement {expected_seq} (a statement is missing or repeated)")
        if s.prev_hash != last:
            bad.append("prev_hash does not match the previous statement (one was removed, reordered or altered)")
        try:
            message = trust_message(s.kind, tenant, s.key_id, s.arg, s.subject_hash, s.stmt_seq, s.prev_hash)
        except AttestError as exc:
            message = ""
            bad.append(exc.message)
        if message and statement_hash(message, s.signed_by, s.signature) != s.statement_hash:
            bad.append("the stored statement hash does not match its contents")
        if message and not bad:
            if s.signed_by not in st.roots:
                bad.append(f"signed by {s.signed_by}, which is not a root key")
            else:
                try:
                    signer = verify_sshsig(s.signature, message.encode("utf-8"))
                    if signer.key_id != s.signed_by:
                        bad.append("the signature was made by a different key than the one named")
                except SignatureInvalid as exc:
                    bad.append(f"bad signature: {exc}")
        if not bad:
            bad.extend(_apply_statement(st, s))
        for b in bad:
            st.problems.append(_problem("trust", who, b))
        expected_seq, last = s.stmt_seq + 1, s.statement_hash
        st.head_seq, st.head_hash = s.stmt_seq, s.statement_hash
    return st


def _apply_statement(st: TrustState, s: Statement) -> list[str]:
    if s.kind in ("key", "root_add"):
        if not s.pubkey:
            return ["the statement carries no public key"]
        try:
            key = parse_public_key(s.pubkey)
        except AttestError as exc:
            return [exc.message]
        if key.key_id != s.key_id:
            return [f"the public key's fingerprint is {key.key_id}, not {s.key_id}"]
        if s.kind == "root_add":
            if s.key_id in st.roots:
                return ["that root is already trusted"]
            st.roots[s.key_id] = key
            return []
        if s.key_id in st.keys:
            return ["that key is already authorized (rotate to a new key instead)"]
        if s.arg is None or s.arg < 1:
            return ["a key is authorized from a signer_seq of at least 1"]
        st.keys[s.key_id] = KeyAuth(key=key, from_seq=s.arg, stmt_seq=s.stmt_seq)
        return []
    if s.kind == "revoke":
        auth = st.keys.get(s.key_id)
        if auth is None:
            return ["that key was never authorized"]
        if s.arg is None or s.arg < 0:
            return ["a revocation needs a cutoff signer_seq of at least 0"]
        auth.cutoff = s.arg if auth.cutoff is None else min(auth.cutoff, s.arg)
        return []
    if s.kind == "void":
        if s.signed_by != s.key_id:
            return ["a void names the root that made it as its key"]
        if s.arg is None or s.arg < 1 or not s.subject_hash:
            return ["a void names the attestation's signer_seq and its attestation hash"]
        st.voids[s.arg] = s.subject_hash
        return []
    if s.kind == "cosign":
        if s.signed_by != s.key_id:
            return ["a cosignature names the root that made it as its key"]
        if s.arg is None or s.arg < 1 or not s.subject_hash:
            return ["a cosignature names the checkpoint's signer_seq and its attestation hash"]
        st.cosigns.append(Cosign(checkpoint_seq=s.arg, checkpoint_hash=s.subject_hash, root_id=s.key_id, stmt_seq=s.stmt_seq))
        return []
    return ["unknown statement kind"]


# --- the attestation log ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Attestation:
    signer_seq: int
    kind: str
    subject: str
    subject_hash: str
    prev_hash: str
    key_id: str
    signed_at: str
    signature: str
    attestation_hash: str


class Truth(Protocol):
    """What the verifier checks an attestation's subject against (the database, or a dict in tests)."""

    def block_hash(self, height: int) -> str | None: ...
    def receipt_problem(self, eo_id: str) -> str | None: ...  # None = an intact replay receipt with exactly this id


@dataclass
class AttestationSummary:
    count: int = 0
    head_seq: int = 0
    head_hash: str = GENESIS
    blocks: set[int] = field(default_factory=set)
    receipts: set[str] = field(default_factory=set)
    newest_checkpoint_seq: int = 0
    cosigned_checkpoint_seq: int = 0
    keys_used: set[str] = field(default_factory=set)
    voided: list[int] = field(default_factory=list)
    # which VALID attestation vouches for which subject, so a caller can ask about one block or one receipt (signature levels)
    bad_seqs: set[int] = field(default_factory=set)
    block_seq: dict[int, int] = field(default_factory=dict)
    block_subject_hash: dict[int, str] = field(default_factory=dict)
    receipt_seq: dict[str, int] = field(default_factory=dict)
    valid_checkpoints: dict[int, int] = field(default_factory=dict)  # checkpoint signer_seq -> the last attestation it covers
    cosigned_checkpoints: set[int] = field(default_factory=set)      # checkpoints a root cosigned (the cosign matched the log)


def evaluate_attestations(tenant: str, trust: TrustState, rows: list[Attestation], truth: Truth) -> tuple[list[Problem], AttestationSummary]:
    problems: list[Problem] = []
    summary = AttestationSummary()
    expected, last = 1, GENESIS
    seen: dict[tuple[str, str], Attestation] = {}
    by_seq: dict[int, Attestation] = {}
    for a in sorted(rows, key=lambda r: r.signer_seq):
        who = f"attestation {a.signer_seq}"
        bad: list[str] = []
        if a.signer_seq != expected:
            bad.append(f"expected attestation {expected} (one is missing or repeated)")
        if a.prev_hash != last:
            bad.append("prev_hash does not match the previous attestation (one was removed, reordered or altered)")
        try:
            message = attestation_message(a.kind, tenant, a.subject, a.subject_hash, a.signer_seq, a.prev_hash, a.signed_at)
        except AttestError as exc:
            message = ""
            bad.append(exc.message)
        voided = False
        if message:
            if attestation_hash(message, a.key_id, a.signature) != a.attestation_hash:
                bad.append("the stored attestation hash does not match its contents")
            elif a.signer_seq in trust.voids:
                if trust.voids[a.signer_seq] == a.attestation_hash:
                    voided = True  # a root voided exactly this row: its signature and subject are no longer judged
                else:
                    bad.append("a root voided an attestation with this signer_seq but a different hash (the row was replaced)")
            if not voided:
                auth = trust.keys.get(a.key_id)
                if auth is None:
                    bad.append(f"signed by {a.key_id}, which no root authorized")
                else:
                    if a.signer_seq < auth.from_seq:
                        bad.append(f"key {a.key_id} was not authorized until attestation {auth.from_seq}")
                    if auth.cutoff is not None and a.signer_seq > auth.cutoff:
                        bad.append(f"key {a.key_id} was revoked with a cutoff of {auth.cutoff}; this attestation is after it and untrusted")
                    try:
                        signer = verify_sshsig(a.signature, message.encode("utf-8"))
                        if signer.key_id != a.key_id:
                            bad.append("the signature was made by a different key than the one named")
                    except SignatureInvalid as exc:
                        bad.append(f"bad signature: {exc}")
                bad.extend(_check_subject(tenant, a, by_seq, truth))
        prior = None if voided else seen.get((a.kind, a.subject))
        if prior is not None:
            bad.append(f"{a.subject} was already attested at {prior.signer_seq}"
                       + (" with a DIFFERENT hash (equivocation)" if prior.subject_hash != a.subject_hash else ""))
        elif not voided:
            seen[(a.kind, a.subject)] = a
        for b in bad:
            problems.append(_problem("attestation", who, b))
        if bad:
            summary.bad_seqs.add(a.signer_seq)
        if voided:
            summary.voided.append(a.signer_seq)
        elif not bad:
            summary.keys_used.add(a.key_id)
            if a.kind == "block":
                h = parse_block_subject(a.subject)
                if h is not None:
                    summary.blocks.add(h)
                    summary.block_seq[h], summary.block_subject_hash[h] = a.signer_seq, a.subject_hash
            elif a.kind == "receipt":
                summary.receipts.add(a.subject)
                summary.receipt_seq[a.subject] = a.signer_seq
            else:
                summary.newest_checkpoint_seq = max(summary.newest_checkpoint_seq, a.signer_seq)
                cp = parse_checkpoint_subject(a.subject)
                if cp is not None:
                    summary.valid_checkpoints[a.signer_seq] = cp[0]
        by_seq[a.signer_seq] = a
        expected, last = a.signer_seq + 1, a.attestation_hash
        summary.count += 1
        summary.head_seq, summary.head_hash = a.signer_seq, a.attestation_hash
    for c in trust.cosigns:
        target = by_seq.get(c.checkpoint_seq)
        if target is None or target.kind != "checkpoint" or target.attestation_hash != c.checkpoint_hash:
            problems.append(_problem("cosign", f"trust statement {c.stmt_seq}",
                                     f"the root {c.root_id} cosigned checkpoint {c.checkpoint_seq} with hash {c.checkpoint_hash[:16]}..., which is not what the log holds (a fork, or a rewritten log)"))
        else:
            summary.cosigned_checkpoint_seq = max(summary.cosigned_checkpoint_seq, c.checkpoint_seq)
            summary.cosigned_checkpoints.add(c.checkpoint_seq)
    return problems, summary


def _check_subject(tenant: str, a: Attestation, by_seq: dict[int, Attestation], truth: Truth) -> list[str]:
    if a.kind == "block":
        h = parse_block_subject(a.subject)
        if h is None:
            return ["a block attestation's subject looks like block:<height>"]
        real = truth.block_hash(h)
        if real is None:
            return [f"block {h} does not exist"]
        return [] if real == a.subject_hash else [f"it attests block {h} with hash {a.subject_hash[:16]}..., but the block's hash is {real[:16]}..."]
    if a.kind == "receipt":
        if not a.subject.startswith("eo:sha256:") or a.subject != "eo:sha256:" + a.subject_hash:
            return ["a receipt attestation's subject is the receipt's evidence id, and its hash is the same 64 hex characters"]
        problem = truth.receipt_problem(a.subject)
        return [problem] if problem else []
    cp = parse_checkpoint_subject(a.subject)
    if cp is None:
        return ["a checkpoint's subject looks like checkpoint:<seq>:<head>:<tip height>:<tip hash>"]
    covered_seq, covered_head, tip_height, tip_hash = cp
    out: list[str] = []
    if covered_seq != a.signer_seq - 1:
        out.append("a checkpoint covers the log up to the attestation just before it")
    prior = by_seq.get(covered_seq)
    if covered_seq == 0:
        if covered_head != GENESIS:
            out.append("the covered head of an empty log is all zeros")
    elif prior is None or prior.attestation_hash != covered_head:
        out.append("the covered head is not the attestation log's actual head at that point")
    if tip_height == 0:
        if tip_hash != GENESIS:
            out.append("with no block the tip hash is all zeros")
    else:
        real = truth.block_hash(tip_height)
        if real is None or real != tip_hash:
            out.append(f"the tip block {tip_height} is not what the checkpoint says")
    if checkpoint_hash(tenant, covered_seq, covered_head, tip_height, tip_hash) != a.subject_hash:
        out.append("the checkpoint's hash does not match its subject")
    return out


class DictTruth:
    """A Truth over plain dicts (tests, and offline checks of an exported bundle)."""

    def __init__(self, blocks: dict[int, str] | None = None, receipts: dict[str, str | None] | None = None):
        self.blocks, self.receipts = blocks or {}, receipts or {}

    def block_hash(self, height: int) -> str | None:
        return self.blocks.get(height)

    def receipt_problem(self, eo_id: str) -> str | None:
        if eo_id not in self.receipts:
            return f"receipt {eo_id} does not exist"
        return self.receipts[eo_id]


# --- the whole check for one tenant, against the database ------------------------------------------------------------------------

def signatures_mode() -> str:
    value = (os.getenv(MODE_ENV) or "warn").strip().lower()
    return value if value in ("off", "warn", "require") else "warn"


def grace_hours() -> float:
    try:
        return max(0.0, float(os.getenv(GRACE_ENV) or DEFAULT_GRACE_HOURS))
    except ValueError:
        return DEFAULT_GRACE_HOURS


class _DbTruth:
    def __init__(self, blocks: dict[int, str], receipts: dict[str, str | None]):
        self._b, self._r = blocks, receipts

    def block_hash(self, height: int) -> str | None:
        return self._b.get(height)

    def receipt_problem(self, eo_id: str) -> str | None:
        return self._r.get(eo_id, f"receipt {eo_id} does not exist")


def load_statements(conn: Any, tenant: str) -> list[Statement]:
    return [Statement(*r) for r in conn.execute(
        "SELECT stmt_seq, kind, key_id, pubkey, arg, subject_hash, prev_hash, signed_by, signature, statement_hash "
        "FROM trust_statements WHERE tenant_key = %s ORDER BY stmt_seq", (tenant,))]


def load_attestations(conn: Any, tenant: str) -> list[Attestation]:
    return [Attestation(*r) for r in conn.execute(
        "SELECT signer_seq, kind, subject, subject_hash, prev_hash, key_id, signed_at, signature, attestation_hash "
        "FROM attestations WHERE tenant_key = %s ORDER BY signer_seq", (tenant,))]


def load_truth(conn: Any, tenant: str) -> tuple[_DbTruth, list[tuple[int, str, Any]], list[tuple[str, Any]]]:
    """Block hashes and receipt validity as the database holds them, plus (height, hash, sealed_at) and (receipt id, created_at)."""
    from app import evidence as evidence_objects  # local import: evidence.py does not depend on this module

    blocks = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT height, block_hash, sealed_at FROM blocks WHERE tenant_key = %s ORDER BY height", (tenant,))]
    receipts: dict[str, str | None] = {}
    stamps: list[tuple[str, Any]] = []
    for row in conn.execute(
        "SELECT id, schema_id, payload, pointer, size_bytes, created_at, created_by FROM evidence_objects WHERE tenant_key = %s AND schema_id = %s",
        (tenant, evidence_objects.CES_REPLAY_RECEIPT)):
        obj = evidence_objects.EvidenceObject(id=row[0], schema_id=row[1], payload=row[2], pointer=row[3], size_bytes=row[4],
                                              created_at=row[5].isoformat(), created_by=row[6])
        damaged = evidence_objects.verify_stored(obj)
        receipts[obj.id] = f"receipt {obj.id} is damaged: {damaged[0]}" if damaged else None
        stamps.append((obj.id, row[5]))
    return _DbTruth({h: bh for h, bh, _ in blocks}, receipts), blocks, stamps


def verify_tenant(conn: Any, tenant: str, *, mode: str | None = None, now: Any = None, roots: dict[str, PublicKey] | None = None) -> dict[str, Any]:
    """Check the tenant's attestations against the pinned roots and the trust log.

    Returns {"problems": [...], "warnings": [...], "notes": [...], "summary": {...}}.  A signature that is invalid, from an
    unauthorized or revoked key, a broken or forked log, a wrong subject: always a problem.  Unsigned blocks and receipts are a
    warning after the grace period in ``warn`` mode and a problem in ``require`` mode; nothing is judged until a signing key has
    been authorized (before that, signing is simply not set up).  With no trust root configured nothing is called verified."""
    from datetime import datetime, timezone

    mode = mode or signatures_mode()
    out: dict[str, Any] = {"problems": [], "warnings": [], "notes": [], "summary": {}}
    if mode == "off":
        out["notes"].append("signatures: checking is off (JARVIS_SIGNATURES=off)")
        return out
    now = now or datetime.now(timezone.utc)
    roots = load_roots() if roots is None else roots
    statements = load_statements(conn, tenant)
    attestations = load_attestations(conn, tenant)
    if not statements and not attestations:
        out["notes"].append("signatures: signing is not set up (no key authorized, nothing signed)")
        return out
    if not roots:
        message = f"signatures not verified: no trust root is configured ({ROOTS_ENV}), so {len(statements)} statement(s) and {len(attestations)} attestation(s) cannot be checked"
        if mode == "require":
            out["problems"].append(_problem("signatures", "trust roots", message))
        else:
            out["warnings"].append(message)
        return out
    trust = evaluate_trust(tenant, roots, statements)
    truth, blocks, receipt_stamps = load_truth(conn, tenant)
    problems, summary = evaluate_attestations(tenant, trust, attestations, truth)
    out["problems"].extend(trust.problems + problems)
    out["summary"] = {"attestations": summary.count, "head_seq": summary.head_seq, "head_hash": summary.head_hash,
                      "blocks_signed": len(summary.blocks), "receipts_signed": len(summary.receipts),
                      "newest_checkpoint_seq": summary.newest_checkpoint_seq, "cosigned_checkpoint_seq": summary.cosigned_checkpoint_seq,
                      "keys_authorized": len(trust.keys), "roots": len(trust.roots), "voided": summary.voided}
    if not trust.keys:
        out["notes"].append("signatures: roots are configured but no signing key is authorized yet")
        return out
    limit = grace_hours()
    unsigned = [(f"block {h}", sealed) for h, _, sealed in blocks if h not in summary.blocks]
    unsigned += [(f"receipt {rid}", created) for rid, created in receipt_stamps if rid not in summary.receipts]
    for label, stamp in unsigned:
        age = (now - stamp).total_seconds() / 3600
        if age > limit:
            text = f"{label} is unsigned ({age:.0f}h old, grace {limit:g}h)"
            if mode == "require":
                out["problems"].append(_problem("unsigned", label, text))
            else:
                out["warnings"].append(text)
    out["notes"].append(
        f"signatures: {summary.count} attestation(s) up to {summary.head_seq}, {len(summary.blocks)} block(s) and {len(summary.receipts)} receipt(s) signed, "
        f"{len(unsigned)} unsigned{f', {len(summary.voided)} voided by a root' if summary.voided else ''}; newest checkpoint {summary.newest_checkpoint_seq or 'none'}, cosigned checkpoint {summary.cosigned_checkpoint_seq or 'none'}")
    return out


# --- signature levels for one block or receipt ----------------------------------------------------------------------------------

LEVEL_LABELS = {0: "L0 unsigned", 1: "L1 Mint-signed", 2: "L2 root-cosigned"}


def attestation_level(summary: AttestationSummary, signer_seq: int | None) -> int:
    """0 = no valid attestation, 1 = a valid attestation by an authorized, unrevoked Mint key, 2 = additionally covered by a checkpoint
    that a root cosigned (the cosign names the checkpoint's hash, which names the whole log up to it)."""
    if signer_seq is None:
        return 0
    for c in summary.cosigned_checkpoints:
        covered = summary.valid_checkpoints.get(c)  # only a checkpoint that itself verified counts
        if covered is not None and covered >= signer_seq:
            return 2
    return 1


def signature_report(conn: Any, tenant: str, *, block_height: int | None, block_hash: str | None, receipt_id: str | None = None,
                     mode: str | None = None, roots: dict[str, PublicKey] | None = None) -> dict[str, Any]:
    """How well signed is this block (and, if given, this receipt)?  Never raises for a signature problem; says so in the result.

    ``level`` is the lowest of the parts: a receipt is only as signed as its block.  A problem is always a failure: an attestation for
    this subject that does not verify, or (in ``require``) any problem anywhere in the signing logs, no trust root, or a part that is
    unsigned.  In ``warn`` an unsigned part and unrelated log problems are warnings.  No trust root: nothing is verified, level 0, and
    ``require`` fails.  No grace period applies here: asked about one subject, an unsigned one is unsigned."""
    mode = mode or signatures_mode()
    report: dict[str, Any] = {"mode": mode, "verified": False, "level": None, "label": "not checked (JARVIS_SIGNATURES=off)",
                              "block": None, "receipt": None, "problems": [], "warnings": []}
    if mode == "off":
        return report
    require = mode == "require"
    wanted = ([f"block:{block_height}"] if block_height is not None else []) + ([receipt_id] if receipt_id else [])
    parts: dict[str, dict[str, Any]] = {}
    if block_height is not None:
        parts["block"] = {"height": block_height, "subject": f"block:{block_height}", "level": 0, "attestation_seq": None}
    if receipt_id:
        parts["receipt"] = {"id": receipt_id, "subject": receipt_id, "level": 0, "attestation_seq": None}

    def finish() -> dict[str, Any]:
        level = min((p["level"] for p in parts.values()), default=0)
        for name, p in parts.items():
            p["label"] = LEVEL_LABELS[p["level"]]
            report[name] = {k: v for k, v in p.items() if k != "subject"}
        report["level"], report["label"] = level, LEVEL_LABELS[level] + ("" if report["verified"] else " (not verified)")
        return report

    def unsigned(text: str, subject: str) -> None:
        if require:
            report["problems"].append(_problem("signatures", subject, text))
        else:
            report["warnings"].append(text)

    roots = load_roots() if roots is None else roots
    conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (tenant,))
    statements = load_statements(conn, tenant)
    attestations = load_attestations(conn, tenant)
    if not roots:
        text = f"signatures not verified: no trust root is configured ({ROOTS_ENV}), so nothing can be checked and nothing is called signed"
        if require:
            report["problems"].append(_problem("signatures", "trust roots", text))
        else:
            report["warnings"].append(text)
        return finish()
    report["verified"] = True
    if not statements and not attestations:
        for name, p in parts.items():
            unsigned(f"{name} {p.get('id') or p.get('height')} is unsigned: signing is not set up (no key authorized, nothing signed)", p["subject"])
        return finish()
    trust = evaluate_trust(tenant, roots, statements)
    truth, _, _ = load_truth(conn, tenant)
    log_problems, summary = evaluate_attestations(tenant, trust, attestations, truth)
    subject_of = {a.signer_seq: a.subject for a in attestations}
    mine = [p for p in log_problems if subject_of.get(int(p["subject"].split()[-1])) in wanted] if log_problems else []
    others = [p for p in log_problems if p not in mine] + list(trust.problems)
    report["problems"].extend(_problem("signatures", p["subject"], p["problem"]) for p in mine)
    if others:
        text = f"the signing logs have {len(others)} other problem(s) (first: {others[0]['subject']}: {others[0]['problem']}); see /api/jarvis/attestations/verify"
        if require:
            report["problems"].append(_problem("signatures", "signing log", text))
        else:
            report["warnings"].append(text)
    if "block" in parts:
        seq = summary.block_seq.get(block_height)
        if seq is not None and block_hash is not None and summary.block_subject_hash.get(block_height) != block_hash:
            report["problems"].append(_problem("signatures", f"block {block_height}", "the attestation names a different block hash than the block's"))
            seq = None
        parts["block"].update(attestation_seq=seq, level=attestation_level(summary, seq))
    if "receipt" in parts:
        seq = summary.receipt_seq.get(receipt_id)
        parts["receipt"].update(attestation_seq=seq, level=attestation_level(summary, seq))
    for name, p in parts.items():
        who = f"{name} {p.get('id') or p.get('height')}"
        if p["level"] == 0:
            if p["subject"] in {subject_of[s] for s in summary.voided}:
                unsigned(f"{who} is unsigned: its attestation was voided by a root", p["subject"])
            else:
                unsigned(f"{who} is unsigned", p["subject"])
    if "block" in parts and "receipt" in parts and parts["receipt"]["level"] > 0 and parts["block"]["level"] == 0:
        unsigned(f"the receipt is signed but its block {block_height} is not; the receipt is only as signed as its block", f"block {block_height}")
    return finish()


# --- command line -------------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.attest", description="Verify the ledger's signatures (this tool never signs)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="check the attestations and trust statements of a tenant against the pinned roots")
    v.add_argument("--tenant", default="operator")
    v.add_argument("--mode", choices=("off", "warn", "require"))
    f = sub.add_parser("fingerprint", help="print the key id of an OpenSSH public key file")
    f.add_argument("file")
    args = parser.parse_args(argv)

    if args.cmd == "fingerprint":
        try:
            print(parse_public_key(Path(args.file).read_text("utf-8")).key_id)
        except (AttestError, OSError) as exc:
            print(getattr(exc, "message", str(exc)), file=sys.stderr)
            return 2
        return 0

    import psycopg
    from psycopg import sql

    from app.pg_schema import check_schema_version, validate_schema_name

    dsn = (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if not dsn:
        print("Set JARVIS_DATABASE_MIGRATE_URL (or JARVIS_DATABASE_URL).", file=sys.stderr)
        return 2
    schema = (os.getenv("JARVIS_DATABASE_SCHEMA") or "").strip() or None
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            if schema:
                conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(validate_schema_name(schema))))
            check_schema_version(conn)
            conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (args.tenant,))
            result = verify_tenant(conn, args.tenant, mode=args.mode)
    except AttestError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    for p in result["problems"]:
        print(f"PROBLEM tenant={args.tenant} {p['subject']} [{p['check']}]: {p['problem']}")
    for w in result["warnings"]:
        print(f"WARNING tenant={args.tenant}: {w}")
    for n in result["notes"]:
        print(n)
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
