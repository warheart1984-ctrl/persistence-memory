# Signing runbook: the key ceremony, custody, the daily routine, rotation and compromise

Applies once the signatures build (schema v7) is deployed. **Nothing here has been done on the live ledger**: no key exists, no root is
pinned, the sign timer is not enabled. Everything below was rehearsed end to end on a throwaway copy of the stack on the Mint box
(Linux): the ceremony, signing, backup with its export, the witness, the restore drill, a real restore, and the custody refusals. The
PC-side commands are the same Python and `ssh-keygen` calls; **they have not been run on Windows**, so the PowerShell lines below are
the part to try first, and to fix here if anything differs.

## The rules (read these first)

1. **The Mint private key never enters a backup, a volume or a container.**
   * It lives only in `~/jarvis-ledger/keys/` on the Mint box: that directory mode 700, the key mode 600, owned by the box user.
     It is created by `jarvisctl attest init-key`, which refuses to overwrite a key and refuses any other place.
   * It is not under the repository, `deploy/`, `~/jarvis-ledger/backups` or `/var/lib/docker`, and no running container may mount
     the key's directory or any parent or child of it. **The signer checks all of this on every run and refuses (exit 3) if any is false.**
   * The compose project hands containers only a read-only file of PUBLIC keys. A test fails if the compose file ever names a key.
   * `backup.sh` stops, and publishes nothing, if the set would contain this key (matched on the key's secret in any base64 alignment
     or hex, in the database's data or in any file) or any private-key-looking header in the appdata archive, globals or exports. Key-shaped
     text inside the database (a note *about* keys) is only a warning, because the history is append-only and could never be cleaned.
     The restore drill repeats these scans on every restored set.
2. **The root private keys never touch the Mint box.** They live on the PC (software first, a hardware key as a second root later; either
   is enough). Only their public keys are pinned: `trust/roots.pub` in the repository, the PC, and the service's read-only config.
3. **Nobody signs from the app.** The app verifies and stores; only the host signer (outside every container) holds the signing key, and
   it signs only messages it built itself, after the offline replay verifier accepted the newest block.
4. Do not copy the signing key anywhere "for safety". If it is lost, rotate (below); that is what the root is for.

## One-time ceremony (needs you at the PC and at the box)

Deploy order when you decide to go live: take a backup and drill it, pull `main`, `jarvisctl up` (migrates v6 to v7, adds the roots config;
v6 code refuses v7, so the rollback is the pre-upgrade backup), then the steps below. Enabling the timer is a separate OK.

**1. On the PC: make the first root key** (a passphrase is strongly advised):

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\jarvis_root -C "jarvis-root-1"
```

Keep an encrypted copy of `jarvis_root` offline (USB). Losing the only root means the signatures can never be re-authorized; that is why
a second root (hardware) is planned.

**2. In the repository: pin its public key.** Put the line from `jarvis_root.pub` into `trust/roots.pub` (comments allowed, **public keys
only**), commit it through a PR, and pull it on the box and the PC. A test fails if a private key is ever committed.

**3. On the box: create the signing key and install the roots.**

```bash
deploy/mint/bin/jarvisctl attest init-key       # prints the key id and the PUBLIC key; creates ~/jarvis-ledger/keys (700) / key (600)
deploy/mint/bin/jarvisctl attest install-roots  # copies trust/roots.pub to the service (public keys only) and restarts the app
```

**4. On the PC: authorize the signing key** (the tunnel must be up; `JARVIS_MEMORYBOARD_URL` and `JARVIS_API_KEY_FILE` as for the hooks).
Copy the signing key's **public** file (`jarvis-sign-ed25519.pub`, not the private one) to the PC, then:

```powershell
python -m app.witness statement key --roots trust\roots.pub --root-key $HOME\.ssh\jarvis_root --tenant operator `
  --mint-pub .\jarvis-sign-ed25519.pub --from-seq 1 --post
```

**5. On the box: check, then one manual signing pass.** A pass signs every pending block (after the offline replay accepted the newest),
then every pending replay receipt (each re-derived from the raw rows first, at most 25 per pass; `JARVIS_SIGN_MAX_RECEIPTS`), then one checkpoint.

```bash
deploy/mint/bin/jarvisctl attest status            # authorized: true
deploy/mint/bin/jarvisctl attest sign --dry-run
deploy/mint/bin/jarvisctl attest sign
deploy/mint/bin/jarvisctl verify                   # now reports the signatures too
```

**6. Only with your explicit OK:** `systemctl --user enable --now jarvis-sign.timer` (hourly at :56, after the seal and before the backup).
Run in `warn` mode for 7 days (`JARVIS_SIGNATURES=warn`, the default); if the watchdog was quiet, switch to `require`
(`JARVIS_SIGNATURES=require` in the service's environment). If it was noisy, stay in `warn` another week and find out why.

## Receipts and the signing pass

* The signer signs a receipt only at a **sealed point**: the receipt's block must exist, have the hash the receipt names (recomputed from the
  block's own fields), and cover the receipt's seq. It builds the message itself from the receipt's id, runs `replay verify --receipt` on the
  raw rows in a one-off container, and checks its own signature. Blocks are always signed first, so a receipt is never attested ahead of its block.
* A receipt that fails any of that is **refused, never signed, and reported** (`REFUSED receipt ...` on stderr, exit 4, so the unit fails and the
  watchdog notices); the other receipts and the checkpoint still go ahead. Find out why before anything else: a receipt that does not re-derive
  means the ledger or the receipt differs from what was issued.
* `jarvisctl replay check|verify` now print **L0 unsigned / L1 Mint-signed / L2 root-cosigned** for the receipt and its block. L2 appears once you
  have cosigned a checkpoint that covers them (the daily routine below). A receipt is only as signed as its block.

## Switching to `require` (and back)

`JARVIS_SIGNATURES` is `warn` unless you set it, and shipping it switched on is blocked by a test. Do not switch until **all** of these hold:

1. The sign timer has run for seven days in `warn` (it only runs after your separate OK), the watchdog has not complained about the signer or the
   cosign age in that time, and `jarvisctl attest verify` shows no warnings except for the newest hour or two.
2. `jarvisctl attest status` shows the key authorized, `jarvisctl verify` is clean, and a drill passed since the last signing pass.
3. You have cosigned at least one checkpoint (the daily routine) and know how to do it again.

To switch: put `JARVIS_SIGNATURES=require` in `deploy/mint/.env`, then `deploy/mint/bin/jarvisctl up` (recreates the app and the verifier with it).
What changes: verification **fails** for an unsigned block or receipt (after the 2-hour grace in `verify`/`attest verify`; **immediately** for a
single `replay check`, so a receipt issued a minute ago fails until the next :56 pass), for any signing-log problem, and with no trust root. What does
not change: writes, sealing, and issuing receipts still work. To switch back, set `warn` (or delete the line) and run `jarvisctl up`. If the
watchdog or `verify` starts failing right after the switch, that is the switch doing its job: look at `jarvisctl attest verify` before reverting.

## The daily routine on the PC (witness and cosign)

The daily offsite copy already arrives on the PC. After it does:

```powershell
# decrypt with the offline age key (the one that is never on the box), unpack the set and its export
age -d -i <age-private-key-file> jarvis-<UTC>.bundle.tar.age | tar -x -C $HOME\jarvis-offsite
python -m app.witness verify-export $HOME\jarvis-offsite\jarvis-<UTC>.signatures.json --roots trust\roots.pub `
  --state $HOME\jarvis-witness-state.json --update-state
python -m app.witness cosign $HOME\jarvis-offsite\jarvis-<UTC>.signatures.json --roots trust\roots.pub `
  --root-key $HOME\.ssh\jarvis_root --state $HOME\jarvis-witness-state.json --update-state --post
```

`verify-export` needs only the export and the root PUBLIC keys. It checks every signature and link, and that the export **extends**
what it saw last time. `cosign` signs the newest checkpoint with the root key (it asks for the passphrase), only after a clean check.
The witness state file is what turns a rewrite into a visible event: **keep it on the PC, and back it up.**
The watchdog warns if no cosign has been made for 72 hours.

## If `verify-export` reports a problem

| Message | Meaning | What to do |
|---|---|---|
| `attestation N ... changed since an earlier run` / `block N changed` | History was rewritten and re-signed (possibly with a valid, taken signing key) | **Treat the signing key as taken.** Do not cosign. See "Compromise" |
| `attestation N was seen on an earlier run and is gone now` | The signing log was cut | Same |
| `bad signature`, `no root authorized`, `revoked ... cutoff` | A forged, unauthorized or post-revocation attestation | Find how it got into the database; revoke if the key may be exposed |
| `cosign ... a fork` | A cosigned checkpoint is not what the log holds | Same as a rewrite |
| `no trust roots` | Wrong or empty `--roots` file | Fix the file; nothing was verified |

On the box, `jarvisctl replay check <receipt>` reporting **L0** after the signer ran usually means the receipt was created after the last pass (wait
for :56), the signer refused it (see its stderr), or the roots file is empty (then it says "not verified").

## Rotation (planned, or after a scare)

1. On the box: `jarvisctl attest init-key` cannot overwrite, so set `JARVIS_SIGN_KEY=~/jarvis-ledger/keys/jarvis-sign-ed25519-2` and run it
   (still under `keys/`), then copy the new public file to the PC.
2. On the PC: authorize the new key from the next position, then revoke the old key with a cutoff at the last checkpoint a root cosigned:
   `python -m app.witness statement key ... --mint-pub new.pub --from-seq N` then `statement revoke --key-id <old id> --cutoff <that seq>`.
3. Point the signer at the new key (`JARVIS_SIGN_KEY`) and run a signing pass. The chain continues: attestations keep counting.
4. Shred the old private key on the box.

A signing key cannot authorize its own successor: only a root can.

## Compromise of the signing key (the box was taken, or the key may have leaked)

What an attacker with the Mint key can do: sign false new entries, and re-sign a rewritten history. What they cannot: add a root, authorize
another key, or change what an off-box witness already recorded.

1. Stop the sign timer (`systemctl --user disable --now jarvis-sign.timer`) and the seal timer if the box is suspect.
2. On the PC: find the **last checkpoint you cosigned** (`witness verify-export` prints it). History up to there is witnessed.
3. Revoke the key with that checkpoint's `signer_seq` as the cutoff: later attestations stop counting (`statement revoke ... --cutoff N`).
4. Review everything after the cutoff by hand (records written, blocks sealed, receipts issued). It is not deleted, because the history is
   append-only; mark bad rows with a root-signed `void` (`statement void --seq N --hash H`) and supersede bad records with new ones.
5. Rotate to a new key as above, from a clean box if the box itself was taken. Rotate the operator API key, the database passwords and the
   age/offsite keys too if the box was.
6. If the PC may also be compromised, none of this can be trusted: rebuild the roots from a clean machine and re-pin them.

## Loss

* **Signing key lost:** rotate (a root authorizes a new one). Nothing already signed is affected.
* **One root lost:** use the other (once a second exists); add a replacement with `statement root-add`.
* **All roots lost:** the signatures can no longer be re-authorized or revoked. Start a new trust chain from a new root and re-pin it.
* **Witness state lost:** the next `verify-export` has no memory, so it cannot notice a rewrite of what was seen before. Recreate it from
  the oldest offsite bundle you still have (verify it, then `--update-state`), and keep a copy next time.

## Checks you can run yourself

```bash
ls -ld ~/jarvis-ledger/keys ~/jarvis-ledger/keys/*     # drwx------ and -rw------- , owned by you
docker ps -q | xargs -r docker inspect -f '{{.Name}}: {{range .Mounts}}{{.Source}} {{end}}'   # nothing under ~/jarvis-ledger/keys
deploy/mint/bin/jarvisctl attest status                # the key id and whether a root authorized it
deploy/mint/bin/jarvisctl attest sign --dry-run        # runs every custody check, signs nothing
```

## What this does not give you

See `SIGNATURES.md`: a valid signature is not truth and not authorship; a taken signing key can sign false new entries until the
compromise is noticed and it is revoked; history newer than the last off-box copy or cosign can be rewritten and re-signed until the next
one; if the PC and the box are both compromised nothing here helps.
