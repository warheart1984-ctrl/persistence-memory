# Signatures

**Status: built, not deployed, not enabled.** The verification side (schema v7 logs, the checks, the API, the `pg_verify` section), the host
signer, the backup, restore and drill handling, the custody guards and the PC-side witness are on `main`. **Nothing is deployed, no key
exists, no root is pinned, the sign timer is not enabled, and nothing in the live ledger is signed.** Receipts are not signed yet (a later
step). The key ceremony is in `SIGNING_RUNBOOK.md`.

## What a signature says, and what it does not

A valid attestation says: *a key that a root key authorized attested this exact digest* (a block hash, a replay receipt's evidence
id, a checkpoint over the signing log). It does **not** say the content is true, who wrote it (`source_agent` stays caller-asserted),
or when: `signed_at` is the signer's own claim; order comes from `signer_seq` and the chain.

## Formats (built)

* **Signatures** are OpenSSH signatures (`ssh-keygen -Y sign -n jarvis-ledger-v1`), Ed25519 only. Verified in pure Python
  (`app/attest.py`, `cryptography`) and cross-checked in the tests against real `ssh-keygen -Y verify`. Hardware (`sk-`) keys are
  refused until their extra signed fields are supported.
* **Attestation message** (nothing is re-serialized; only existing SHA-256 digests are signed):
  `jarvis-attest|v1|<kind>|<len>:<tenant>|<subject>|<subject_hash>|<signer_seq>|<previous attestation hash>|<signed_at>`.
  Kinds: `block` (subject `block:<height>`, the block hash), `receipt` (the receipt's evidence id), `checkpoint` (the signing log up to
  the previous attestation and the newest block). Each carries the hash of the one before it, so a removed or forked entry shows.
* **Trust statements** are signed only by a **root** key: `key` (authorize a signing key from a signer_seq), `revoke` (a cutoff
  signer_seq: later attestations by that key are untrusted, earlier ones stay valid), `root_add` (another root), `cosign` (a root
  witnessed a checkpoint), `void` (a root marks one bad attestation, named by its hash, as not counting). They form their own chain.
* **Evidence Objects** get no attestation of their own in phase 1: one cited by sealed history is covered by its block's signature
  (the block commits to the entries and so to the evidence ids they cite). An uncited object is unsigned.

## Who can do what (built)

| | can | cannot |
|---|---|---|
| **Root key** (pinned from outside the database) | authorize and revoke signing keys, add roots, cosign, void | (used rarely, kept off the Mint box) |
| **Signing key** (on the Mint box, for the signer) | make attestations | authorize its own successor, add a root, void anything |
| **The service** | verify, then store attestations and root-signed statements; never signs | store anything that does not verify |
| **The database** | enforce the chain (next `signer_seq`, previous hash), compute stored hashes, refuse changes and deletes | check Ed25519 (the service does, and every verifier does again) |

## Verifying (built)

`pg_verify` and `python -m app.attest verify` replay the trust log from the pinned roots, then check every attestation: signature,
the key it names, its authorization window and revocation cutoff, the chain, the subject (the block's real hash, an intact receipt,
a checkpoint that describes the log exactly), equivocation (one subject attested twice), and cosigns against the checkpoints they
name. `JARVIS_SIGNATURES=off|warn|require` (default `warn`):

* An invalid signature, an unauthorized or revoked key, a broken or forked log: **always a problem** (exit 1), except in `off`.
* An unsigned block or receipt older than `JARVIS_SIGNATURE_GRACE_HOURS` (default 2): a warning in `warn`, a problem in `require`.
  Nothing is judged before a signing key has been authorized ("signing is not set up").
* **No trust root configured** (`JARVIS_TRUST_ROOTS_FILE`) while signature rows exist: "signatures not verified", never success.

`jarvisctl verify` (which runs `pg_verify` in the migrate service, now given the same public roots) and the restore drill check signatures too.

Endpoints (operator key): `GET /api/jarvis/attestations[/head|/pending|/verify]`, `POST /api/jarvis/attestations`,
`GET /api/jarvis/trust`, `GET|POST /api/jarvis/trust/statements`.

## Key custody (enforced; the ceremony is in `SIGNING_RUNBOOK.md`)

1. **The Mint private key never enters a backup, a volume or a container.** It lives in `~/jarvis-ledger/keys/` (directory 700, key 600,
   owned by the box user), outside the repository, `deploy/`, the backups and `/var/lib/docker`. **The signer refuses to run** (exit 3) if the key
   or its directory is readable by others, is a symlink, is passphrase-protected, lies in a forbidden place, or if any running container
   mounts the key's directory or a parent or child of it. The compose project gives containers only a read-only file of PUBLIC keys, and a
   test fails if it ever names a key.
2. **`backup.sh` publishes nothing** if the set would contain this key (matched on the key's secret, in every base64 alignment and in hex, in the
   database's data or in any file) or any private-key header in the appdata archive, globals or exports. Key-shaped text inside the database
   is a warning only (the history is append-only and could never be cleaned). The restore drill repeats the scans on every restored set.
3. **The root private keys never touch the Mint box.** They live on the PC (software first; a hardware root later; either is enough). Only
   their public keys are pinned: `trust/roots.pub`, the PC, the service's read-only config.
4. A signing key is rotated by a root-signed statement; the old key is revoked with a cutoff at the last checkpoint a root cosigned.

## What is built where

| | |
|---|---|
| `app/attest.py` | formats, trust and attestation checks, `python -m app.attest verify` |
| `app/signer.py` | the host signer: custody checks, builds its own messages, pre-sign offline verification, self-check; `init-key`, `status`, `sign` |
| `app/witness.py` | the PC side: `verify-export` (remembers what it saw), `cosign`, ceremony `statement`s; runs without the application's packages |
| `deploy/mint/bin/attest.sh` | `jarvisctl attest status\|sign\|init-key\|install-roots\|verify` |
| `deploy/mint/bin/sigexport.py` | the `<set>.signatures.json` export that rides with every backup set and every offsite bundle |
| `deploy/mint/bin/custody.sh`, `keymarkers.py` | the custody scans |
| `jarvis-sign.service` / `.timer` | hourly at :56 (after the seal, before the backup); installed, **not enabled** |
| anchors | every attestation and trust statement is anchored like the blocks; a log that loses or rewrites one is refused |

## Limits, stated plainly

* **A taken Mint key can still sign false new entries** until the compromise is detected and the key rotated. It can also re-sign a
  rewritten history. What protects already-witnessed history is the off-box copy of what the key signed (the chain, so a rewrite shows
  as a fork or as two attestations for one subject) and root-cosigned checkpoints, not the key being off-box. History newer than the
  newest off-box copy or cosigned checkpoint can be rewritten and re-signed until the next sync.
* An off-box *public* key alone proves nothing against someone who holds the Mint key.
* **If the PC and the Mint box are both compromised, none of this protects anything.**
* The database cannot check Ed25519, so anyone who can call the store function can park an unsigned or badly signed row at the next
  position. It cannot be deleted (append-only) and the log carries on after it; verify flags it until a root voids exactly that row.
* A compromised signer program (the repository on the box) can sign what it likes; verifying from a clean clone on the PC is the check.
* This is a local, operator-held root, not the CCS Root Authority; it supplies only the "verification" link of the provenance chain.
* Losing both root keys is unrecoverable trust, which is why two are planned (the second a hardware key, added by a root-signed statement).

## Planned, not built

* Signing replay receipts in the signing pass, signature levels in `replay verify`, and the `require` switch after seven days in `warn`
  (longer if the watchdog is noisy).
* The hardware root and the statement flow to add it (the statement exists; the hardware-key signature format does not yet).
* A scripted PC routine (decrypt the offsite bundle, verify, cosign). The commands are in the runbook, run by hand.
