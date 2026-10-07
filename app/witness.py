"""The witness: runs on the PC (and on the box, for the restore drill), holds the root key, and checks the box from the outside.

    python -m app.witness verify-export FILE --roots roots.pub [--state witness-state.json] [--update-state]
    python -m app.witness cosign FILE --roots roots.pub --root-key ~/.ssh/jarvis_root [--state S] [--post | --out F]
    python -m app.witness statement key|revoke|root-add|void ... --root-key PATH [--post | --out F]

``FILE`` is the ``<set>.signatures.json`` that rides with every backup set (it is inside every daily offsite bundle).  Verification
needs nothing from the box except that file: the pinned root public keys, ``ssh-keygen`` (or the ``cryptography`` package) and this
code.  What it adds to the box's own ``pg_verify``:

* **it remembers**.  With a state file it requires the new export to *extend* what it saw last time: every attestation, trust
  statement and block it has already seen must still be there, byte for byte.  A history rewritten and re-signed with a taken signing
  key passes the box's own checks, but not this one: it shows up as an entry that changed or vanished.
* **it cosigns**.  After a clean check, the root key (kept off the box) signs the newest checkpoint.  That witnessed point is what a
  later rewrite is compared against.

The root private key is only ever used here, by ``ssh-keygen``, on the machine that holds it; nothing in this module reads its bytes.
Standard library only (plus ``ssh-keygen``).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app import attest
from app.signer import Api, SignerError

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE = 0, 1, 2


class WitnessError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.message, self.exit_code = message, exit_code


def load_export(path: str | os.PathLike) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise WitnessError(f"cannot read the signatures export {path}: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("tenants"), dict):
        raise WitnessError(f"{path} is not a signatures export (format 1)")
    return data


def _rows(tenant_data: dict[str, Any]) -> tuple[list[attest.Statement], list[attest.Attestation]]:
    try:
        statements = [attest.Statement(**s) for s in tenant_data["statements"]]
        attestations = [attest.Attestation(**a) for a in tenant_data["attestations"]]
    except (KeyError, TypeError) as exc:
        raise WitnessError(f"the export has a malformed row: {exc}") from None
    return statements, attestations


def load_state(path: str | os.PathLike | None) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {"format": 1, "tenants": {}}
    try:
        state = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise WitnessError(f"cannot read the witness state {path}: {exc}") from None
    if state.get("format") != 1:
        raise WitnessError(f"{path} is not a witness state file")
    return state


def check_tenant(tenant: str, tenant_data: dict[str, Any], roots: dict[str, attest.PublicKey], seen: dict[str, Any] | None) -> dict[str, Any]:
    """Verify one tenant's export and, if ``seen`` (what an earlier run recorded) is given, that the export extends it."""
    statements, attestations = _rows(tenant_data)
    blocks = {b["height"]: b["block_hash"] for b in tenant_data["blocks"]}
    trust = attest.evaluate_trust(tenant, roots, statements)
    problems, summary = attest.evaluate_attestations(tenant, trust, attestations, attest.DictTruth(blocks, {r: None for r in tenant_data["receipts"]}))
    problems = list(trust.problems) + problems
    if seen:
        have_att = {str(a.signer_seq): a.attestation_hash for a in attestations}
        have_stmt = {str(s.stmt_seq): s.statement_hash for s in statements}
        for label, was, now in (("attestation", seen.get("attestations", {}), have_att), ("trust statement", seen.get("statements", {}), have_stmt),
                                ("block", seen.get("blocks", {}), {str(h): v for h, v in blocks.items()})):
            for key, value in sorted(was.items(), key=lambda kv: int(kv[0])):
                if key not in now:
                    problems.append({"check": "witness", "subject": f"{label} {key}", "problem": f"{label} {key} was seen on an earlier run and is gone now (the log was cut or rewritten)"})
                elif now[key] != value:
                    problems.append({"check": "witness", "subject": f"{label} {key}", "problem": f"{label} {key} changed since an earlier run (a rewrite, re-signed or not)"})
        if seen.get("cosigned_checkpoint_seq", 0) > summary.cosigned_checkpoint_seq and not any(p["check"] == "cosign" for p in problems):
            problems.append({"check": "witness", "subject": "cosign", "problem": f"checkpoint {seen['cosigned_checkpoint_seq']} was cosigned before and its cosignature is gone"})
    return {
        "problems": problems,
        "summary": {"attestations": summary.count, "head_seq": summary.head_seq, "head_hash": summary.head_hash, "blocks_signed": len(summary.blocks),
                    "newest_checkpoint_seq": summary.newest_checkpoint_seq, "cosigned_checkpoint_seq": summary.cosigned_checkpoint_seq,
                    "statements": len(statements), "keys_authorized": len(trust.keys), "voided": summary.voided},
        "snapshot": {"attestations": {str(a.signer_seq): a.attestation_hash for a in attestations},
                     "statements": {str(s.stmt_seq): s.statement_hash for s in statements},
                     "blocks": {str(h): v for h, v in blocks.items()}, "cosigned_checkpoint_seq": summary.cosigned_checkpoint_seq},
        "trust": trust, "attestations": attestations, "statements": statements,
    }


def verify_export(export: dict[str, Any], roots: dict[str, attest.PublicKey], state: dict[str, Any], tenant: str | None = None) -> dict[str, Any]:
    if not roots:
        raise WitnessError("no trust roots: give --roots a file with the root public keys (without them nothing can be verified)")
    results: dict[str, Any] = {}
    names = [tenant] if tenant else sorted(export["tenants"])
    for name in names:
        if name not in export["tenants"]:
            raise WitnessError(f"the export has no tenant {name!r}")
        results[name] = check_tenant(name, export["tenants"][name], roots, state["tenants"].get(name))
    return results


def write_state(path: str | os.PathLike, state: dict[str, Any], results: dict[str, Any]) -> None:
    for name, r in results.items():
        state["tenants"][name] = r["snapshot"]
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", "utf-8")
    os.replace(tmp, path)


# --- root signing (ssh-keygen, on the machine that holds the root key) -----------------------------------------------------------

def _root_public(root_key: str) -> attest.PublicKey:
    pub = Path(str(root_key) + ".pub")
    try:
        return attest.parse_public_key(pub.read_text("utf-8"))
    except (OSError, attest.AttestError) as exc:
        raise WitnessError(f"cannot read the root public key {pub}: {getattr(exc, 'message', exc)}") from None


def root_sign(root_key: str, message: str) -> str:
    """Sign with the root key through ssh-keygen (it may ask for the key's passphrase on the terminal); checked before it is returned."""
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        f = Path(tmp) / "m"
        f.write_bytes(message.encode("utf-8"))
        r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(root_key), "-n", attest.NAMESPACE, str(f)], stdout=subprocess.DEVNULL)
        if r.returncode != 0:
            raise WitnessError("ssh-keygen could not sign with the root key (wrong passphrase, or the key is not readable)", EXIT_PROBLEMS)
        sig = attest.normalize_signature((Path(tmp) / "m.sig").read_text())
    try:
        signer = attest.verify_sshsig(sig, message.encode("utf-8"))
    except attest.SignatureInvalid as exc:
        raise WitnessError(f"the signature just made does not verify: {exc}", EXIT_PROBLEMS) from None
    if signer.key_id != _root_public(root_key).key_id:
        raise WitnessError("the signature was not made by the root key named", EXIT_PROBLEMS)
    return sig


def statement_body(root_key: str, roots: dict[str, attest.PublicKey], tenant: str, kind: str, *, key_id: str, pubkey: str | None, arg: int | None,
                   subject_hash: str | None, stmt_seq: int, prev_hash: str) -> dict[str, Any]:
    root = _root_public(root_key)
    if root.key_id not in roots:
        raise WitnessError(f"the root key {root.key_id} is not in the trust roots you gave; the service would refuse the statement")
    message = attest.trust_message(kind, tenant, key_id, arg, subject_hash, stmt_seq, prev_hash)
    sig = root_sign(root_key, message)
    return {"kind": kind, "key_id": key_id, "pubkey": pubkey, "arg": arg, "subject_hash": subject_hash, "stmt_seq": stmt_seq,
            "prev_hash": prev_hash, "signed_by": root.key_id, "signature": sig}


def newest_checkpoint(attestations: list[attest.Attestation]) -> attest.Attestation | None:
    cps = [a for a in attestations if a.kind == "checkpoint"]
    return max(cps, key=lambda a: a.signer_seq) if cps else None


# --- command line ------------------------------------------------------------------------------------------------------------------------

def _api() -> Api:
    base = os.getenv("JARVIS_MEMORYBOARD_URL")
    key_file = os.getenv("JARVIS_API_KEY_FILE")
    if not base or not key_file:
        raise WitnessError("to post, set JARVIS_MEMORYBOARD_URL and JARVIS_API_KEY_FILE (the tunnel must be up)")
    return Api(base, key_file)


def _head(api: Api | None, args: argparse.Namespace) -> tuple[int, str]:
    if args.stmt_seq is not None and args.prev_hash:
        return args.stmt_seq, args.prev_hash
    if api is None:
        raise WitnessError("give --stmt-seq and --prev-hash, or let the tool ask the service (--post)")
    h = api.get("/api/jarvis/attestations/head")
    return h["next_stmt_seq"], h["trust_head_hash"]


def _emit(body: dict[str, Any], api: Api | None, args: argparse.Namespace) -> int:
    if args.post:
        assert api is not None
        status, resp = api.request("POST", "/api/jarvis/trust/statements", body)
        if status != 200:
            print(f"the service refused the statement: HTTP {status} {resp.get('detail', '') if isinstance(resp, dict) else ''}", file=sys.stderr)
            return EXIT_PROBLEMS
        print(f"stored statement {resp['stmt_seq']} ({resp['kind']}) hash {resp['statement_hash']}")
        return EXIT_OK
    text = json.dumps(body, indent=1, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, "utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return EXIT_OK


def _print_result(name: str, r: dict[str, Any]) -> None:
    for p in r["problems"]:
        print(f"PROBLEM tenant={name} {p['subject']} [{p['check']}]: {p['problem']}")
    s = r["summary"]
    if not r["problems"]:
        print(f"ok: tenant {name}: {s['attestations']} attestation(s) to {s['head_seq']}, {s['blocks_signed']} block(s) signed, "
              f"{s['statements']} trust statement(s), newest checkpoint {s['newest_checkpoint_seq'] or 'none'}, cosigned checkpoint {s['cosigned_checkpoint_seq'] or 'none'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.witness", description="Check the box's signatures from outside it, and sign as a root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--roots", required=True, help="file with the root public keys (the repository's trust/roots.pub, or your copy)")
        p.add_argument("--tenant")
        p.add_argument("--state", help="witness state file: what earlier runs saw; a later export must extend it")

    v = sub.add_parser("verify-export")
    v.add_argument("file")
    common(v)
    v.add_argument("--update-state", action="store_true", help="after a clean check, record what was seen in --state")

    c = sub.add_parser("cosign")
    c.add_argument("file")
    common(c)
    c.add_argument("--root-key", required=True, help="path of the root private key (ssh-keygen signs with it; its bytes are never read here)")
    c.add_argument("--post", action="store_true", help="send the cosignature to the service (needs the tunnel)")
    c.add_argument("--out")
    c.add_argument("--stmt-seq", type=int)
    c.add_argument("--prev-hash")
    c.add_argument("--update-state", action="store_true")

    st = sub.add_parser("statement")
    st.add_argument("kind", choices=("key", "revoke", "root-add", "void"))
    st.add_argument("--roots", required=True)
    st.add_argument("--tenant", default="operator")
    st.add_argument("--root-key", required=True)
    st.add_argument("--mint-pub", help="key: the signing key's public key file")
    st.add_argument("--from-seq", type=int, help="key: the signer_seq it may sign from")
    st.add_argument("--key-id", help="revoke: the key id to revoke")
    st.add_argument("--cutoff", type=int, help="revoke: attestations after this signer_seq are untrusted")
    st.add_argument("--new-root-pub", help="root-add: the new root's public key file")
    st.add_argument("--seq", type=int, help="void: the attestation's signer_seq")
    st.add_argument("--hash", help="void: the attestation's attestation_hash")
    st.add_argument("--post", action="store_true")
    st.add_argument("--out")
    st.add_argument("--stmt-seq", type=int)
    st.add_argument("--prev-hash")
    args = parser.parse_args(argv)

    try:
        roots = attest.load_roots(args.roots)
        if args.cmd == "verify-export":
            state = load_state(args.state)
            results = verify_export(load_export(args.file), roots, state, args.tenant)
            for name, r in results.items():
                _print_result(name, r)
            bad = any(r["problems"] for r in results.values())
            if args.update_state and args.state and not bad:
                write_state(args.state, state, results)
            return EXIT_PROBLEMS if bad else EXIT_OK

        if args.cmd == "cosign":
            state = load_state(args.state)
            export = load_export(args.file)
            results = verify_export(export, roots, state, args.tenant)
            for name, r in results.items():
                _print_result(name, r)
            if any(r["problems"] for r in results.values()):
                print("not cosigning: the export does not verify", file=sys.stderr)
                return EXIT_PROBLEMS
            name = args.tenant or (next(iter(results)) if len(results) == 1 else None)
            if name is None:
                raise WitnessError("the export has several tenants; name one with --tenant")
            cp = newest_checkpoint(results[name]["attestations"])
            if cp is None:
                print("nothing to cosign: no checkpoint has been signed yet")
                return EXIT_OK
            if cp.signer_seq <= results[name]["summary"]["cosigned_checkpoint_seq"]:
                print(f"checkpoint {cp.signer_seq} is already cosigned")
                return EXIT_OK
            api = _api() if args.post else None
            seq, prev = _head(api, args)
            root = _root_public(args.root_key)
            body = statement_body(args.root_key, roots, name, "cosign", key_id=root.key_id, pubkey=None, arg=cp.signer_seq, subject_hash=cp.attestation_hash,
                                  stmt_seq=seq, prev_hash=prev)
            rc = _emit(body, api, args)
            if rc == EXIT_OK and args.update_state and args.state:
                results[name]["snapshot"]["cosigned_checkpoint_seq"] = cp.signer_seq
                write_state(args.state, state, results)
            return rc

        # statement
        api = _api() if args.post else None
        seq, prev = _head(api, args)
        if args.kind == "key":
            if not args.mint_pub or args.from_seq is None:
                raise WitnessError("key needs --mint-pub and --from-seq")
            mint = attest.parse_public_key(Path(args.mint_pub).read_text("utf-8"))
            body = statement_body(args.root_key, roots, args.tenant, "key", key_id=mint.key_id, pubkey=mint.text(), arg=args.from_seq, subject_hash=None, stmt_seq=seq, prev_hash=prev)
        elif args.kind == "revoke":
            if not args.key_id or args.cutoff is None:
                raise WitnessError("revoke needs --key-id and --cutoff")
            body = statement_body(args.root_key, roots, args.tenant, "revoke", key_id=args.key_id, pubkey=None, arg=args.cutoff, subject_hash=None, stmt_seq=seq, prev_hash=prev)
        elif args.kind == "root-add":
            if not args.new_root_pub:
                raise WitnessError("root-add needs --new-root-pub")
            new = attest.parse_public_key(Path(args.new_root_pub).read_text("utf-8"))
            body = statement_body(args.root_key, roots, args.tenant, "root_add", key_id=new.key_id, pubkey=new.text(), arg=None, subject_hash=None, stmt_seq=seq, prev_hash=prev)
        else:
            if args.seq is None or not args.hash:
                raise WitnessError("void needs --seq and --hash")
            root = _root_public(args.root_key)
            body = statement_body(args.root_key, roots, args.tenant, "void", key_id=root.key_id, pubkey=None, arg=args.seq, subject_hash=args.hash, stmt_seq=seq, prev_hash=prev)
        return _emit(body, api, args)
    except WitnessError as exc:
        print(exc.message, file=sys.stderr)
        return exc.exit_code
    except attest.AttestError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_USAGE
    except SignerError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
