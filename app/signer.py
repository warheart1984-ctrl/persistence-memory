"""The signer: runs on the Mint box as the box user, never in a container, and signs attestations of sealed blocks and checkpoints.

    python3 -m app.signer sign [--dry-run]     sign what is pending (blocks, then one checkpoint)
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
  verify cleanly: it will not extend a log that already has problems.
* **It checks its own work**: each signature is verified (against the key it came from) before it is posted.

Receipts are signed by a later step (PR C); this one signs blocks and checkpoints.  Standard library only (plus ``ssh-keygen``), so it
runs on the host without the application's dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import attest, blocks

EXIT_OK, EXIT_USAGE, EXIT_CUSTODY, EXIT_UNHEALTHY, EXIT_API = 0, 2, 3, 4, 5
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


def default_verify_block(height: int, block_hash: str) -> None:
    """The independent check before signing: replay the ledger up to this block from the RAW rows, in a one-off container (never
    the service's own SQL), and require the block to be exactly the one about to be signed."""
    script = repo_root() / "deploy" / "mint" / "bin" / "replay.sh"
    r = subprocess.run([str(script), "verify", "--at-block", str(height), "--expect-block-hash", block_hash], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        raise SignerError("pre_sign_verify_failed", f"the offline replay does not accept block {height}: {' | '.join(tail)}", EXIT_UNHEALTHY)


def status(api: Api, key: KeyInfo) -> dict[str, Any]:
    trust = api.get("/api/jarvis/trust")
    pending = api.get("/api/jarvis/attestations/pending")
    mine = next((k for k in trust["keys"] if k["key_id"] == key.public.key_id), None)
    return {"key_id": key.public.key_id, "authorized": mine is not None and mine["revoked_after_signer_seq"] is None, "authorization": mine,
            "trust_roots_configured": trust["trust_roots_configured"], "pending_blocks": [b["height"] for b in pending["blocks"]],
            "pending_receipts": len(pending["receipts"]), "head_seq": pending["head"]["head_seq"], "next_signer_seq": pending["head"]["next_signer_seq"]}


def run_sign(api: Api, key: KeyInfo, *, dry_run: bool = False, verify_block: Callable[[int, str], None] = default_verify_block,
             now: Callable[[], str] = _now) -> dict[str, Any]:
    """Sign every pending sealed block (oldest first), then one checkpoint if anything new was signed.  Returns what was done."""
    st = status(api, key)
    if not st["trust_roots_configured"]:
        raise SignerError("no_trust_root", "the service has no trust root configured, so it cannot verify (or accept) anything; run: jarvisctl attest install-roots", EXIT_UNHEALTHY)
    auth = st["authorization"]
    if auth is None:
        raise SignerError("key_not_authorized", f"{key.public.key_id} is not authorized by a root; the key ceremony (docs/SIGNING_RUNBOOK.md) has not been done for this key", EXIT_UNHEALTHY)
    if auth["revoked_after_signer_seq"] is not None:
        raise SignerError("key_revoked", f"{key.public.key_id} has been revoked (cutoff {auth['revoked_after_signer_seq']}); rotate to a new key", EXIT_UNHEALTHY)
    check = api.get("/api/jarvis/attestations/verify")
    if not check["ok"] or any("not verified" in w for w in check.get("warnings", [])):
        why = (check["problems"][0]["problem"] if check["problems"] else "; ".join(check.get("warnings", [])))
        raise SignerError("log_unhealthy", f"the signing log does not verify, so it will not be extended: {why}", EXIT_UNHEALTHY)
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
    result: dict[str, Any] = {"tenant": tenant, "signed_blocks": [], "checkpoint": None, "dry_run": dry_run, "planned_blocks": [p["height"] for p in plan],
                              "skipped_receipts": len(pending["receipts"])}
    if dry_run or not plan:
        if not plan:
            result["note"] = "nothing pending"
        return result

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m app.signer", description="Sign sealed blocks and checkpoints (host only; the private key never leaves it)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sign")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
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
        key = preflight_key(key_path)
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
    if result["dry_run"]:
        print(f"dry run: would sign blocks {result['planned_blocks'] or 'none'} and a checkpoint; {result['skipped_receipts']} receipt(s) wait for a later step")
    elif result["signed_blocks"]:
        print("signed blocks " + ",".join(str(b["height"]) for b in result["signed_blocks"])
              + f"; checkpoint {result['checkpoint']['signer_seq']} covers {result['checkpoint']['covers']}")
    else:
        print(f"nothing to sign ({result.get('note', '')})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
