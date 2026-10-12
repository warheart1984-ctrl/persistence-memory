"""The signer: runs on the Mint box as the box user, never in a container, and signs attestations of sealed blocks, replay receipts
(which exist only at sealed points) and checkpoints.

    python3 -m app.signer sign [--dry-run]     sign what is pending (blocks, then receipts, then one checkpoint)
    python3 -m app.signer status               what it would do, and the state of the key (never prints a secret)
    python3 -m app.signer init-key             create the signing key in the custody directory (refuses to overwrite)

Rules this program enforces (docs/SIGNATURES.md, docs/SIGNING_RUNBOOK.md):

* **The private key stays on the host.**  It must live in a directory owned by this user, mode 700, as a regular file (not a symlink)
  owned by this user, mode 600; outside the repository, the deploy directory, the backup directory and ``/var/lib/docker``; and no
  running container may mount any path above or below it.  Anything else and it refuses to run (exit 3).
* **It signs only what it built itself.**  Every message is assembled here from fields it has checked (a block hash recomputed from the
  block's own fields, the chain position from the log's head); it never signs text the service hands it.
* **It signs only what an independent check accepted.**  Before it signs blocks it requires the offline replay verifier (raw rows, in a
  one-off container, as the migrate role) to accept the newest pending block and its hash, and requires the existing signing log to
  verify cleanly: it will not extend a log that already has problems.  Before it signs a receipt it re-derives that receipt the same
  way (``replay verify --receipt``, raw rows), checks that the receipt's sealed block is the real block (hash recomputed from the
  block's own fields) and covers the receipt's point, and signs it only after that block's attestation (the same pass or earlier).
  A receipt that fails is refused and reported (exit 4); it never stops the others from being signed, and is never signed.
* **It checks its own work**: each signature is verified (against the key it came from) before it is posted.

Standard library only (plus ``ssh-keygen``), so it
runs on the host without the application's dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import attest, blocks

EXIT_OK, EXIT_USAGE, EXIT_CUSTODY, EXIT_UNHEALTHY, EXIT_API = 0, 2, 3, 4, 5
OPERATOR_TENANT = "operator"
Transport = Callable[[str, str, "dict[str, Any] | None"], "tuple[int, Any]"]


class SignerError(Exception):
    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code


def jarvis_home() -> Path:
    return Path(os.getenv("JARVIS_HOME") or Path.home() / "jarvis-ledger")


def default_key_path() -> Path:
    return Path(os.getenv("JARVIS_SIGN_KEY") or jarvis_home() / "keys" / "jarvis-sign-ed25519").expanduser()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def forbidden_roots() -> list[Path]:
    """Places the private key must never be under: the repository, the deploy directory, the backups, and Docker's own storage."""
    roots = [repo_root(), repo_root() / "deploy", Path(os.getenv("JARVIS_BACKUP_DIR") or jarvis_home() / "backups"), Path("/var/lib/docker"),
             jarvis_home() / "backups", jarvis_home() / "logs"]
    extra = os.getenv("JARVIS_SIGNER_FORBIDDEN")
    if extra:
        roots += [Path(p) for p in extra.split(os.pathsep) if p]
    return roots


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def container_mount_sources() -> list[Path]:
    """Host paths that running containers mount (best effort: empty if Docker cannot be asked)."""
    try:
        ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=30)
        if ids.returncode != 0 or not ids.stdout.split():
            return []
        out = subprocess.run(["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}{{println}}{{end}}", *ids.stdout.split()],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(line) for line in out.stdout.splitlines() if line.strip()]


@dataclass
class KeyInfo:
    path: Path
    public: attest.PublicKey


def preflight_key(path: Path, *, mounts: Callable[[], list[Path]] | None = None, forbidden: list[Path] | None = None) -> KeyInfo:
    """Refuse (exit 3) unless the private key is where and how the custody rules say.  Never reads or prints the key itself."""
    def refuse(message: str) -> SignerError:
        return SignerError("custody", message, EXIT_CUSTODY)

    path = Path(path).expanduser()
    try:
        st = path.lstat()
    except OSError:
        raise refuse(f"no signing key at {path} (create one with: jarvisctl attest init-key)") from None
    if stat.S_ISLNK(st.st_mode):
        raise refuse(f"{path} is a symlink; the key must be a regular file in the custody directory")
    if not stat.S_ISREG(st.st_mode):
        raise refuse(f"{path} is not a regular file")
    if st.st_uid != os.getuid():
        raise refuse(f"{path} is not owned by the user running the signer")
    if st.st_mode & 0o077:
        raise refuse(f"{path} is readable or writable by others (mode {stat.S_IMODE(st.st_mode):o}); it must be 600")
    parent = path.parent.lstat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise refuse(f"the key's directory {path.parent} must be owned by this user and mode 700 (it is {stat.S_IMODE(parent.st_mode):o})")
    for root in (forbidden if forbidden is not None else forbidden_roots()):
        if _inside(path, root):
            raise refuse(f"{path} is inside {root}; the private key must never be in the repository, the deploy directory, a backup location or Docker storage")
    for source in (mounts or container_mount_sources)():
        if _inside(path, source) or _inside(source, path.parent):
            raise refuse(f"a running container mounts {source}, which contains or lies inside the key's directory; the private key must never enter a container")
    pub_file = Path(str(path) + ".pub")
    try:
        public = attest.parse_public_key(pub_file.read_text("utf-8"))
    except (OSError, attest.AttestError) as exc:
        raise refuse(f"cannot read the public key {pub_file}: {getattr(exc, 'message', exc)}") from None
    try:
        derived = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise refuse(f"cannot run ssh-keygen: {exc}") from None
    if derived.returncode != 0:
        raise refuse("the private key cannot be read without a passphrase (unattended signing needs an unencrypted key, protected by the file's mode)")
    try:
        if attest.parse_public_key(derived.stdout).key_id != public.key_id:
            raise refuse(f"{pub_file} is not the public half of {path}")
    except attest.AttestError as exc:
        raise refuse(f"the signing key is not an Ed25519 key: {exc.message}") from None
    return KeyInfo(path=path, public=public)


def sign_message(key: KeyInfo, message: str) -> str:
    """An armored SSHSIG over the message, from ssh-keygen, checked against the key before it is returned."""
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        f = Path(tmp) / "m"
        f.write_bytes(message.encode("utf-8"))
        r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key.path), "-n", attest.NAMESPACE, str(f)], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise SignerError("sign_failed", "ssh-keygen could not sign (is the key readable and unencrypted?)", EXIT_CUSTODY)
        sig = attest.normalize_signature((Path(tmp) / "m.sig").read_text())
    try:
        signer = attest.verify_sshsig(sig, message.encode("utf-8"))
    except attest.SignatureInvalid as exc:
        raise SignerError("self_check_failed", f"the signature just made does not verify: {exc}", EXIT_UNHEALTHY) from None
    if signer.key_id != key.public.key_id:
        raise SignerError("self_check_failed", "the signature was not made by the expected key", EXIT_UNHEALTHY)
    return sig


class Api:
    """The service, over HTTP with the operator key (read from a file, never printed)."""

    def __init__(self, base: str, key_file: str, transport: Transport | None = None):
        self.base, self._key_file, self._transport = base.rstrip("/"), key_file, transport

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        if self._transport is not None:
            return self._transport(method, path, body)
        key = Path(self._key_file).read_text("utf-8").strip()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"X-API-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError) as exc:
            raise SignerError("api_unreachable", f"cannot reach the ledger at {self.base}: {exc}", EXIT_API) from None

    def get(self, path: str) -> Any:
        status, body = self.request("GET", path)
        if status != 200:
            raise SignerError("api_error", f"GET {path} returned HTTP {status}: {body.get('detail', '') if isinstance(body, dict) else ''}", EXIT_API)
        return body


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


RECEIPT_SCHEMA = "CES.Local.ReplayReceipt.v1"
EO_PREFIX = "eo:sha256:"
DEFAULT_MAX_RECEIPTS = 25


def max_receipts() -> int:
    try:
        return max(0, int(os.getenv("JARVIS_SIGN_MAX_RECEIPTS") or DEFAULT_MAX_RECEIPTS))
    except ValueError:
        return DEFAULT_MAX_RECEIPTS


def _replay_args(tenant: str) -> list[str]:
    """``replay.sh verify`` defaults to the operator tenant; another tenant is named explicitly. For the operator the command line is exactly what it was."""
    return [] if tenant == OPERATOR_TENANT else ["--tenant", tenant]


def default_verify_block(height: int, block_hash: str, tenant: str = "operator") -> None:
    """The independent check before signing: replay the ledger up to this block from the RAW rows, in a one-off container (never
    the service's own SQL), and require the block to be exactly the one about to be signed."""
    script = repo_root() / "deploy" / "mint" / "bin" / "replay.sh"
    # --signatures off: the question is whether the block is what the raw rows say, not whether it is signed yet
    r = subprocess.run([str(script), "verify", *_replay_args(tenant), "--at-block", str(height), "--expect-block-hash", block_hash, "--signatures", "off"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        raise SignerError("pre_sign_verify_failed", f"the offline replay does not accept block {height}: {' | '.join(tail)}", EXIT_UNHEALTHY)


def default_verify_receipt(receipt_id: str, tenant: str = "operator") -> None:
    """Re-derive the receipt from the RAW rows in a one-off container (``replay verify --receipt``): the stored object must be
    intact and the replay at its sealed point must give its state root, counts and block."""
    script = repo_root() / "deploy" / "mint" / "bin" / "replay.sh"
    r = subprocess.run([str(script), "verify", *_replay_args(tenant), "--receipt", receipt_id, "--signatures", "off"], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        raise SignerError("pre_sign_verify_failed", f"the offline replay does not re-derive receipt {receipt_id}: {' | '.join(tail)}", EXIT_UNHEALTHY)


def _receipt_to_sign(api: Api, tenant: str, receipt_id: str) -> str:
    """Checks the signer makes itself on a receipt before it spends a replay on it.  Returns "" if fine, else why it is refused."""
    if not (receipt_id.startswith(EO_PREFIX) and len(receipt_id) == len(EO_PREFIX) + 64 and all(c in "0123456789abcdef" for c in receipt_id[len(EO_PREFIX):])):
        return "not an evidence object id"
    status_code, body = api.request("GET", f"/api/jarvis/replay/receipts/{receipt_id}")
    if status_code != 200:
        return f"the service does not return it (HTTP {status_code})"
    obj = body.get("receipt") or {}
    p = obj.get("payload") or {}
    if obj.get("id") != receipt_id or obj.get("schema_id") != RECEIPT_SCHEMA:
        return "the service returned a different object than asked for, or one that is not a replay receipt"
    if p.get("tenant") != tenant:
        return f"it is for tenant {p.get('tenant')!r}, not {tenant!r}"
    height, at_seq = p.get("block_height"), p.get("at_seq")
    if not isinstance(height, int) or not isinstance(at_seq, int):
        return "its sealed point is malformed"
    status_code, bbody = api.request("GET", f"/api/jarvis/blocks/{height}")
    if status_code != 200:
        return f"its block {height} does not exist (HTTP {status_code}): a receipt is signed only at a sealed point"
    block = bbody["block"]
    recomputed = blocks.block_hash(tenant=tenant, height=block["height"], first_seq=block["first_seq"], last_seq=block["last_seq"],
                                   entry_count=block["entry_count"], prev_block_hash=block["prev_block_hash"], entries_root=block["entries_root"],
                                   fmt=block["format"])
    if recomputed != block["block_hash"] or block["block_hash"] != p.get("block_hash"):
        return f"block {height} is not the block the receipt names (hash {str(p.get('block_hash'))[:16]}...)"
    if not block["first_seq"] <= at_seq <= block["last_seq"]:
        return f"seq {at_seq} is not inside block {height} ({block['first_seq']}..{block['last_seq']}), so it is not a sealed point"
    return ""


def status(api: Api, key: KeyInfo) -> dict[str, Any]:
    trust = api.get("/api/jarvis/trust")
    pending = api.get("/api/jarvis/attestations/pending")
    mine = next((k for k in trust["keys"] if k["key_id"] == key.public.key_id), None)
    return {"key_id": key.public.key_id, "authorized": mine is not None and mine["revoked_after_signer_seq"] is None, "authorization": mine,
            "trust_roots_configured": trust["trust_roots_configured"], "pending_blocks": [b["height"] for b in pending["blocks"]],
            "pending_receipts": len(pending["receipts"]), "head_seq": pending["head"]["head_seq"], "next_signer_seq": pending["head"]["next_signer_seq"]}


def require_signing_log_catchup_safe(api: Api) -> None:
    """Reject integrity failures, but let the signer process overdue unsigned work.

    In ``JARVIS_SIGNATURES=require`` the verify endpoint reports pending,
    overdue signatures as problems. Those are precisely what this signer must
    catch up; every other problem and every "not verified" warning remains a
    hard stop.
    """
    check = api.get("/api/jarvis/attestations/verify")
    if not isinstance(check, dict) or not isinstance(check.get("problems"), list):
        raise SignerError("log_unhealthy", "the signing log verification response is malformed", EXIT_UNHEALTHY)
    problems = check["problems"]
    warnings = check.get("warnings", [])
    if not isinstance(warnings, list):
        raise SignerError("log_unhealthy", "the signing log verification warnings are malformed", EXIT_UNHEALTHY)
    blockers = [p for p in problems if not isinstance(p, dict) or p.get("check") != "unsigned"]
    not_verified = [w for w in warnings if isinstance(w, str) and "not verified" in w.lower()]
    unsigned_only = bool(problems) and len(blockers) == 0
    if blockers or not_verified or (check.get("ok") is not True and not unsigned_only):
        details = blockers or problems
        why = (details[0].get("problem", "signing log problem") if details and isinstance(details[0], dict)
               else "; ".join(not_verified or ["verification did not succeed"]))
        raise SignerError("log_unhealthy", f"the signing log does not verify, so it will not be extended: {why}", EXIT_UNHEALTHY)


def run_sign(api: Api, key: KeyInfo, *, dry_run: bool = False, verify_block: Callable[[int, str], None] = default_verify_block,
             verify_receipt: Callable[[str], None] = default_verify_receipt, limit_receipts: int | None = None,
             now: Callable[[], str] = _now) -> dict[str, Any]:
    """Sign every pending sealed block (oldest first), then every pending receipt that re-derives (oldest first, at most
    ``limit_receipts`` per pass), then one checkpoint if anything new was signed.  Returns what was done and what was refused."""
    st = status(api, key)
    if not st["trust_roots_configured"]:
        raise SignerError("no_trust_root", "the service has no trust root configured, so it cannot verify (or accept) anything; run: jarvisctl attest install-roots", EXIT_UNHEALTHY)
    auth = st["authorization"]
    if auth is None:
        raise SignerError("key_not_authorized", f"{key.public.key_id} is not authorized by a root; the key ceremony (docs/SIGNING_RUNBOOK.md) has not been done for this key", EXIT_UNHEALTHY)
    if auth["revoked_after_signer_seq"] is not None:
        raise SignerError("key_revoked", f"{key.public.key_id} has been revoked (cutoff {auth['revoked_after_signer_seq']}); rotate to a new key", EXIT_UNHEALTHY)
    require_signing_log_catchup_safe(api)
    pending = api.get("/api/jarvis/attestations/pending")
    head = pending["head"]
    tenant = head["tenant"]
    if head["next_signer_seq"] < auth["from_signer_seq"]:
        raise SignerError("key_not_yet_valid", f"the key is authorized from attestation {auth['from_signer_seq']}, the next one is {head['next_signer_seq']}", EXIT_UNHEALTHY)

    todo = sorted(pending["blocks"], key=lambda b: b["height"])
    plan: list[dict[str, Any]] = []
    for item in todo:
        block = api.get(f"/api/jarvis/blocks/{item['height']}")["block"]
        recomputed = blocks.block_hash(tenant=tenant, height=block["height"], first_seq=block["first_seq"], last_seq=block["last_seq"],
                                       entry_count=block["entry_count"], prev_block_hash=block["prev_block_hash"], entries_root=block["entries_root"],
                                       fmt=block["format"])
        if recomputed != block["block_hash"] or block["block_hash"] != item["block_hash"]:
            raise SignerError("block_inconsistent", f"block {item['height']}'s hash does not match its own fields; refusing to sign it", EXIT_UNHEALTHY)
        plan.append({"height": block["height"], "block_hash": block["block_hash"]})
    # Every block is either already attested (the log verified clean above) or in `plan`, which is signed before any receipt below, so a
    # receipt is never attested ahead of its block; _receipt_to_sign refuses a receipt whose block does not exist.
    cap = max_receipts() if limit_receipts is None else limit_receipts
    rplan, refused = [], []
    for item in sorted(pending["receipts"], key=lambda r: (r["created_at"], r["id"])):
        why = _receipt_to_sign(api, tenant, item["id"])
        if why:
            refused.append({"id": item["id"], "why": why})
        elif len(rplan) < cap:
            rplan.append(item["id"])
    result: dict[str, Any] = {"tenant": tenant, "signed_blocks": [], "signed_receipts": [], "checkpoint": None, "dry_run": dry_run,
                              "planned_blocks": [p["height"] for p in plan], "planned_receipts": list(rplan), "refused_receipts": refused,
                              "deferred_receipts": max(0, len(pending["receipts"]) - len(refused) - len(rplan))}
    if dry_run or not (plan or rplan):
        if not (plan or rplan):
            result["note"] = "nothing pending" if not refused else "nothing signable"
        return result

    if plan:
        verify_block(plan[-1]["height"], plan[-1]["block_hash"])  # the newest block's check covers every block before it (the chain and the replay)
    seq, prev = head["next_signer_seq"], head["prev_hash"]
    for p in plan:
        message = attest.attestation_message("block", tenant, f"block:{p['height']}", p["block_hash"], seq, prev, now())
        signed_at = message.rsplit("|", 1)[1]
        sig = sign_message(key, message)
        body = {"kind": "block", "subject": f"block:{p['height']}", "subject_hash": p["block_hash"], "signer_seq": seq, "prev_hash": prev,
                "key_id": key.public.key_id, "signed_at": signed_at, "signature": sig}
        status_code, resp = api.request("POST", "/api/jarvis/attestations", body)
        if status_code != 200:
            raise SignerError("store_refused", f"the service refused the attestation of block {p['height']}: HTTP {status_code} {resp.get('detail', '')}", EXIT_API)
        if resp["signer_seq"] != seq:
            raise SignerError("store_unexpected", "the service stored the attestation at an unexpected position", EXIT_API)
        result["signed_blocks"].append({"height": p["height"], "signer_seq": seq})
        seq, prev = seq + 1, resp["attestation_hash"]

    for rid in rplan:
        try:
            verify_receipt(rid)  # re-derived from the raw rows now, not trusted from when it was issued
        except SignerError as exc:
            refused.append({"id": rid, "why": exc.message})
            continue
        subject_hash = rid[len(EO_PREFIX):]
        message = attest.attestation_message("receipt", tenant, rid, subject_hash, seq, prev, now())
        sig = sign_message(key, message)
        status_code, resp = api.request("POST", "/api/jarvis/attestations", {
            "kind": "receipt", "subject": rid, "subject_hash": subject_hash, "signer_seq": seq, "prev_hash": prev,
            "key_id": key.public.key_id, "signed_at": message.rsplit("|", 1)[1], "signature": sig})
        if status_code != 200:
            raise SignerError("store_refused", f"the service refused the attestation of receipt {rid}: HTTP {status_code} {resp.get('detail', '')}", EXIT_API)
        if resp["signer_seq"] != seq:
            raise SignerError("store_unexpected", "the service stored the attestation at an unexpected position", EXIT_API)
        result["signed_receipts"].append({"id": rid, "signer_seq": seq})
        seq, prev = seq + 1, resp["attestation_hash"]

    if not (result["signed_blocks"] or result["signed_receipts"]):
        result["note"] = "nothing signable"
        return result
    tip = api.get("/api/jarvis/attestations/head")
    covered_seq, covered_head = seq - 1, prev
    subject = attest.checkpoint_subject(covered_seq, covered_head, tip["tip_height"], tip["tip_block_hash"])
    subject_hash = attest.checkpoint_hash(tenant, covered_seq, covered_head, tip["tip_height"], tip["tip_block_hash"])
    message = attest.attestation_message("checkpoint", tenant, subject, subject_hash, seq, prev, now())
    sig = sign_message(key, message)
    status_code, resp = api.request("POST", "/api/jarvis/attestations", {
        "kind": "checkpoint", "subject": subject, "subject_hash": subject_hash, "signer_seq": seq, "prev_hash": prev,
        "key_id": key.public.key_id, "signed_at": message.rsplit("|", 1)[1], "signature": sig})
    if status_code != 200:
        raise SignerError("store_refused", f"the service refused the checkpoint: HTTP {status_code} {resp.get('detail', '')}", EXIT_API)
    result["checkpoint"] = {"signer_seq": seq, "attestation_hash": resp["attestation_hash"], "covers": covered_seq, "tip_height": tip["tip_height"]}
    return result


def init_key(path: Path) -> dict[str, str]:
    """Create the signing key in the custody directory with the right modes.  Refuses to overwrite and refuses a forbidden location."""
    path = Path(path).expanduser()
    if path.exists() or Path(str(path) + ".pub").exists():
        raise SignerError("custody", f"{path} already exists; refusing to overwrite a signing key (rotate by creating a new one elsewhere)", EXIT_CUSTODY)
    for root in forbidden_roots():
        if _inside(path.parent, root) or _inside(path, root):
            raise SignerError("custody", f"{path} is inside {root}; the private key must never be there", EXIT_CUSTODY)
    old = os.umask(0o077)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"jarvis-ledger-signer@{os.uname().nodename}", "-f", str(path)],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.umask(old)
    if r.returncode != 0:
        raise SignerError("keygen_failed", "ssh-keygen could not create the key", EXIT_CUSTODY)
    os.chmod(path, 0o600)
    info = preflight_key(path)
    return {"key_id": info.public.key_id, "public_key": info.public.text(), "path": str(path)}


def _tenant_api_timeout() -> float:
    """Seconds one per-tenant API answer may take (the HTTP transport's bound is 60): JARVIS_SIGNER_TENANT_API_TIMEOUT, finite and positive or the default."""
    try:
        value = float(os.getenv("JARVIS_SIGNER_TENANT_API_TIMEOUT", "") or 60)
    except ValueError:
        return 60.0
    return value if 0 < value < float("inf") else 60.0


class ContainerTransport:
    """An API for ONE tenant, served by ``app.tenant_jobs serve`` in a one-off migrate container (tenant-api.sh): JSON lines in, JSON lines out.

    The tenant is bound by the container process the operator started, never by the request, so the public API still cannot select one. The signer's
    logic is unchanged: it asks for the same paths and posts the same attestations; only the way they are reached differs.  Close it when done."""

    def __init__(self, tenant: str, command: list[str] | None = None, timeout: float | None = None):
        self.tenant = tenant
        self._timeout = timeout if timeout is not None else _tenant_api_timeout()
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._container: str | None = None
        env = None
        if command is None:
            # a name of our own for the one-off container, so a stuck one can be removed by name (killing the compose client does not stop the container)
            self._container = f"jarvis-tenant-api-{re.sub(r'[^A-Za-z0-9_.-]', '_', tenant)[:40]}-{os.getpid()}-{secrets.token_hex(4)}"
            env = {**os.environ, "JARVIS_TENANT_API_NAME": self._container}
        cmd = command or [str(repo_root() / "deploy" / "mint" / "bin" / "tenant-api.sh"), tenant]
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        except OSError as exc:
            raise SignerError("api_unreachable", f"cannot start the per-tenant API for {tenant!r}: {exc}", EXIT_API) from None
        threading.Thread(target=self._pump, daemon=True).start()      # a reader thread, so that a read can be bounded: readline() itself cannot time out

    def _pump(self) -> None:
        try:
            for line in self._proc.stdout:        # type: ignore[union-attr]
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put("")                       # end of output: the process exited (or was killed)

    def __call__(self, method: str, path: str, body: "dict[str, Any] | None" = None) -> "tuple[int, Any]":
        proc = self._proc
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"method": method, "path": path, "body": body}) + "\n")
            proc.stdin.flush()
            line = self._lines.get(timeout=self._timeout)
        except queue.Empty:
            # a container that is alive but stuck would otherwise hold this tenant, and every tenant after it, forever
            self._kill()
            raise SignerError("api_timeout", f"the per-tenant API for {self.tenant!r} did not answer within {self._timeout:g}s", EXIT_API) from None
        except (OSError, ValueError):
            line = ""
        if not line:
            raise SignerError("api_unreachable", f"the per-tenant API for {self.tenant!r} ended unexpectedly", EXIT_API)
        answer = json.loads(line)
        return int(answer["status"]), answer["body"]

    def _kill(self) -> None:
        """End the process AND the container it started: the compose client's death does not stop a container that is stuck."""
        self._proc.kill()
        if self._container:
            try:
                subprocess.run(["docker", "rm", "-f", self._container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self._kill()


def sign_tenants(tenants: list[str], key: KeyInfo, *, dry_run: bool, open_api: "Callable[[str], tuple[Api, Callable[[], None]]] | None" = None,
                 verify_block: "Callable[[int, str, str], None] | None" = None, verify_receipt: "Callable[[str, str], None] | None" = None) -> list[dict[str, Any]]:
    """Sign each tenant's chain on its own.  One tenant's failure (no authorized key for it, an unhealthy log, an unreachable API, anything) is recorded
    and the next tenant still runs; every failure is in the returned list and nothing is skipped silently.

    Returns one dict per tenant: {"tenant", "ok": True, "result": run_sign's} or {"tenant", "ok": False, "code", "message", "exit_code"}."""
    def default_open(tenant: str):
        transport = ContainerTransport(tenant)
        return Api("tenant:" + tenant, "", transport=transport), transport.close

    open_api = open_api or default_open
    out: list[dict[str, Any]] = []
    for tenant in tenants:
        closer: Callable[[], None] = lambda: None
        try:
            api, closer = open_api(tenant)
            vb = (lambda h, b, _t=tenant: (verify_block or default_verify_block)(h, b, _t))
            vr = (lambda r, _t=tenant: (verify_receipt or default_verify_receipt)(r, _t))
            out.append({"tenant": tenant, "ok": True, "result": run_sign(api, key, dry_run=dry_run, verify_block=vb, verify_receipt=vr)})
        except SignerError as exc:
            out.append({"tenant": tenant, "ok": False, "code": exc.code, "message": exc.message, "exit_code": exc.exit_code})
        except Exception as exc:  # noqa: BLE001 - one tenant must never stop the others, whatever went wrong with it
            out.append({"tenant": tenant, "ok": False, "code": "unexpected", "message": f"{type(exc).__name__}: {str(exc)[:300]}", "exit_code": EXIT_UNHEALTHY})
        finally:
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
    return out


def report_sign(result: dict[str, Any], prefix: str = "") -> int:
    """Print what a sign pass did (the exact text the signer has always printed; ``prefix`` is only used for a tenant other than the operator)."""
    if result["dry_run"]:
        print(f"{prefix}dry run: would sign blocks {result['planned_blocks'] or 'none'}, {len(result['planned_receipts'])} receipt(s) and a checkpoint; "
              f"{len(result['refused_receipts'])} receipt(s) would be refused, {result['deferred_receipts']} deferred")
    elif result["signed_blocks"] or result["signed_receipts"]:
        print(f"{prefix}signed blocks " + (",".join(str(b["height"]) for b in result["signed_blocks"]) or "none") + f", {len(result['signed_receipts'])} receipt(s)"
              + f"; checkpoint {result['checkpoint']['signer_seq']} covers {result['checkpoint']['covers']}")
    else:
        print(f"{prefix}nothing to sign ({result.get('note', '')})")
    for r in result["refused_receipts"]:
        print(f"REFUSED {prefix}receipt {r['id']}: {r['why']}", file=sys.stderr)
    return EXIT_UNHEALTHY if result["refused_receipts"] else EXIT_OK


def report_tenants(outcomes: list[dict[str, Any]]) -> int:
    """The per-tenant report: for the operator alone exactly the text ``sign`` prints; another tenant's lines carry its key; failures are listed and make the
    exit code non-zero (the highest of theirs), so a failed tenant can never be mistaken for a quiet one."""
    code, failed = EXIT_OK, []
    for o in outcomes:
        prefix = "" if o["tenant"] == OPERATOR_TENANT else f"tenant {o['tenant']}: "
        if o["ok"]:
            rc = report_sign(o["result"], prefix)
            code = max(code, rc)
            if rc != EXIT_OK and not (o["tenant"] == OPERATOR_TENANT and len(outcomes) == 1):
                failed.append(o["tenant"])     # (refused receipts): a tenant that signed with errors is not "ok". The operator ALONE keeps today's exact output.
        else:
            failed.append(o["tenant"])
            code = max(code, o["exit_code"])
            print(f"{prefix}{o['code']}: {o['message']}", file=sys.stderr)
    if failed:
        print(f"FAILED tenants: {', '.join(failed)} ({len(outcomes) - len(failed)} of {len(outcomes)} ok)", file=sys.stderr)
    elif len(outcomes) > 1:
        print(f"all {len(outcomes)} tenants ok")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m app.signer", description="Sign sealed blocks and checkpoints (host only; the private key never leaves it)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sign")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--tenants", help="comma-separated tenant keys: sign each tenant's own chain (through the per-tenant API of the migrate container)")
    st = sub.add_parser("status")
    st.add_argument("--tenants", help="comma-separated tenant keys: report each tenant's state")
    sub.add_parser("init-key")
    args = parser.parse_args(argv)

    key_path = default_key_path()
    try:
        if args.cmd == "init-key":
            info = init_key(key_path)
            print(f"created {info['path']} (mode 600, in a mode 700 directory)")
            print(f"key id: {info['key_id']}")
            print(f"public key (give this to the root ceremony; it is not secret): {info['public_key']}")
            return EXIT_OK
        supplied = getattr(args, "tenants", None)
        tenants = _tenant_list(supplied)
        if supplied is not None and not tenants:
            # an empty selection is not "no option": falling through would sign the operator ledger when the caller asked for specific tenants (and none were given)
            print("no_tenants: --tenants was given but names no tenant", file=sys.stderr)
            return EXIT_USAGE
        key = preflight_key(key_path)
        if tenants:
            if args.cmd == "status":
                return report_status(tenants, key)
            return report_tenants(sign_tenants(tenants, key, dry_run=args.dry_run))
        port = os.getenv("JARVIS_APP_PORT") or "8011"
        api = Api(os.getenv("JARVIS_API_BASE") or f"http://127.0.0.1:{port}",
                  os.getenv("JARVIS_API_KEY_FILE") or str(repo_root() / "deploy" / "mint" / "secrets" / "api-key"))
        if args.cmd == "status":
            print(json.dumps(status(api, key), indent=2))
            return EXIT_OK
        result = run_sign(api, key, dry_run=args.dry_run)
    except SignerError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    return report_sign(result)


def _tenant_list(text: str | None) -> list[str]:
    """The operator first, the rest in the order given, no blanks or duplicates (the same rule as app.tenant_jobs.parse_tenants)."""
    seen: list[str] = []
    for part in (text or "").split(","):
        if part.strip() and part.strip() not in seen:
            seen.append(part.strip())
    return sorted(seen, key=lambda t: (t != OPERATOR_TENANT, seen.index(t)))


def report_status(tenants: list[str], key: KeyInfo, open_api: "Callable[[str], tuple[Api, Callable[[], None]]] | None" = None) -> int:
    code, failed, rows = EXIT_OK, [], {}
    for tenant in tenants:
        closer: Callable[[], None] = lambda: None
        try:
            if open_api:
                api, closer = open_api(tenant)
            else:
                transport = ContainerTransport(tenant)
                api, closer = Api("tenant:" + tenant, "", transport=transport), transport.close
            rows[tenant] = status(api, key)
        except SignerError as exc:
            failed.append(tenant)
            rows[tenant] = {"error": f"{exc.code}: {exc.message}"}
            code = max(code, exc.exit_code)
        except Exception as exc:  # noqa: BLE001
            failed.append(tenant)
            rows[tenant] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            code = max(code, EXIT_UNHEALTHY)
        finally:
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
    print(json.dumps(rows[tenants[0]] if len(tenants) == 1 and tenants[0] == OPERATOR_TENANT else rows, indent=2))
    if failed:
        print(f"FAILED tenants: {', '.join(failed)} ({len(tenants) - len(failed)} of {len(tenants)} ok)", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
