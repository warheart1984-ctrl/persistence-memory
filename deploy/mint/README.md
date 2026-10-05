# Jarvis ledger on the Mint box

The PostgreSQL row store plus the app, in Docker, on one Linux box. Two database roles, nothing reachable from the
network, verified hourly backups, an encrypted daily copy on your Windows PC, and a restore you have actually
rehearsed. Everything runs through `bin/jarvisctl`.

```
Windows PC ──ssh -L──►  127.0.0.1:8011  ─►  app (non-root, read-only fs, no capabilities)
(hooks, agents)                                  │ jarvis_app (ordinary role)
                                                 ▼
                                      db: postgres:16, no published port, scram only
                                                 ▲ postgres superuser: local socket only, via `docker exec`
   backups (hourly, verified) ──► ~/jarvis-ledger/backups ──► age-encrypted daily ──► Windows PC (G:)
```

## What is already on the box (found by a read-only check on 2026-10-05)

* **Linux Mint 22.3** (Ubuntu 24.04 base), 4 CPUs, 15 GiB RAM, root filesystem `/dev/sda2` ext4, 168 GB free.
  Connected by **Wi-Fi** only. `ufw` is installed but **not enabled**. The disk is **not encrypted**.
  SSH listens on all interfaces (key-only).
* **Docker is not installed.** `age` is not installed. `notify-send` is (Cinnamon desktop session).
* **An older `persistence-memory` service already runs** (user unit `persistence-memory.service`, uvicorn on
  `127.0.0.1:8001`, from `~/persistence-memory`, JSON store, code from before the red-team fixes). It also runs
  Grafana (3000), `llm-gateway` (8080/9090) and Prometheus (9091). **None of that is touched.** This stack uses
  port **8011** and its own directories so both can run side by side until you decide to switch.

## One-time setup

Steps that need `sudo` are yours to run; nothing here asks for or stores a sudo password.

### 1. Install Docker Engine, compose and age (from Ubuntu's own archive, no extra repository)
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 age
sudo usermod -aG docker "$USER"      # the docker group is root-equivalent: keep it to your own user
```
Log out and back in (or reboot) so the group applies to your session *and* to your systemd user manager.
Check: `docker run --rm hello-world` and `docker compose version`.

### 2. Firewall and updates (recommended, not required by the stack)
`ufw` is installed but disabled, so nothing filters the box. The stack itself exposes only `127.0.0.1`, but SSH is
open to the whole network. Before enabling, make sure the SSH rule is in place or you will lock yourself out:
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```
Note that a Docker **published** port bypasses ufw (Docker edits iptables itself). That is why this stack binds
`127.0.0.1` explicitly and publishes nothing for the database. Security updates:
`sudo apt-get install -y unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades`.

### 3. Get the code (a separate checkout; the old service's `~/persistence-memory` is left alone)
```bash
git clone https://github.com/warheart1984-ctrl/persistence-memory.git ~/jarvis-ledger-src
cd ~/jarvis-ledger-src/deploy/mint
cp .env.example .env            # port 8011, local tuning, no secrets
bin/jarvisctl secrets           # random passwords + API key, mode 600, never printed
```

### 4. The offsite copy to your Windows PC (the part the rehearsal could not test against real Windows)
The rehearsal used a Linux SSH server as the PC. `OFFSITE_OS=windows` changes only how the upload is hash-checked
(PowerShell `Get-FileHash`); **that path is untested**. Run `bin/jarvisctl offsite` once by hand and confirm it
reports the hash verified before trusting the timer.

1. **age keypair, on the Windows PC (not on the Mint box):** `winget install FiloSottile.age`, then
   `age-keygen -o jarvis-age-key.txt`. Put the file on your USB stick, print a copy, and **remove it from the PC**.
   Copy only the line starting `age1…` (the public key) to the Mint box as `deploy/mint/secrets/age_recipient.txt`.
   Without the private key nobody - including this box - can read the backups, and **you cannot restore from an
   offsite copy without it.**
2. **SSH server on Windows:** Settings → Optional features → OpenSSH Server, then start the `sshd` service and set it
   to Automatic. Create the folder `G:\jarvis-backups`.
3. **A dedicated key for this one job**, on the Mint box: `ssh-keygen -t ed25519 -N '' -f ~/.ssh/jarvis_offsite_ed25519`
   (no passphrase because it runs unattended). Put the **public** half in the Windows account's
   `authorized_keys` (for an administrator account that is `C:\ProgramData\ssh\administrators_authorized_keys`).
   A key that can write to the PC is a risk, so use a normal, non-admin Windows user that owns only the backup folder.
4. **Pin the PC's host key** (this is what stops someone impersonating the PC): on the Mint box
   `ssh-keyscan -t ed25519 <PC address> > deploy/mint/secrets/offsite_known_hosts`, then compare the fingerprint
   (`ssh-keygen -lf deploy/mint/secrets/offsite_known_hosts`) with the one on the PC
   (`ssh-keygen -lf C:\ProgramData\ssh\ssh_host_ed25519_key.pub`) before trusting it.
5. `cp offsite.conf.example secrets/offsite.conf` and fill it in (`OFFSITE_OS=windows`, `OFFSITE_PATH=G:/jarvis-backups`).

### 5. Start it, check it, schedule it
```bash
bin/jarvisctl up                  # db -> migrate -> app; builds the images the first time
bin/jarvisctl smoke               # the acceptance checklist; every line must say PASS
bin/install-units.sh              # hourly backup, daily offsite, weekly drill, 15-minute watchdog (systemd user timers)
bin/jarvisctl backup && bin/jarvisctl drill --prove-detection
bin/jarvisctl offsite
```
`loginctl show-user $USER -p Linger` already says `yes` on this box, so the timers run while you are logged out.

## Using it from your Windows PC
```powershell
ssh -N -L 8011:127.0.0.1:8011 jon@192.168.1.102          # keep this open (or use autossh / a scheduled task)
scp jon@192.168.1.102:jarvis-ledger-src/deploy/mint/secrets/api-key $HOME\.jarvis-api-key
$env:JARVIS_MEMORYBOARD_URL = "http://127.0.0.1:8011"
$env:JARVIS_API_KEY_FILE    = "$HOME\.jarvis-api-key"
python agent-hooks\ping_memoryboard.py                   # Health / Ready / memory count / "OK: service is live"
```
The hooks send the key as `X-API-Key`, and only over `https` or to `127.0.0.1`, so it cannot leak onto the LAN.

## Day to day

| Command | What it does |
|---|---|
| `jarvisctl status` | containers, and how long ago the last backup / offsite copy / drill succeeded |
| `jarvisctl smoke [--no-write]` | the acceptance checks |
| `jarvisctl backup` / `prune [--dry-run]` | take a set now / apply retention |
| `jarvisctl drill [--prove-detection]` | restore the newest set into a scratch database and verify it |
| `jarvisctl restore --yes-destroy-current-data [--backup SET]` | **destructive**: see below |
| `jarvisctl offsite` | encrypt and send the newest set to the PC |
| `jarvisctl verify [tenant]` | recompute the history hash chain |
| `jarvisctl psql` | admin session inside the database container |
| `jarvisctl rotate app\|migrator\|api-key` | new random credential, never shown; api-key then needs copying to the PC |
| `jarvisctl logs [db\|app]`, `down`, `restart` | the obvious |

Alerts go to `~/jarvis-ledger/logs/alerts.log` and, when you are logged in to the desktop, a notification
(the same alert at most once every 6 hours). `~/jarvis-ledger/logs/` has one log per job.

## Backups, precisely

* **Hourly set** in `~/jarvis-ledger/backups/jarvis-<UTC>.{dump,globals.sql,data.tar,counts,anchors,sha256}`.
  The dump is taken as the **superuser** because the ledger uses forced row-level security: an ordinary role would
  either fail or, with `--enable-row-security`, silently write **zero rows**. Each dump is verified (table of
  contents, per-table row counts read out of the dump itself, shrink alarm).
* **Anchors** - where each tenant's history hash chain ends - are written next to every set, in a hash-chained log
  that is never pruned, and inside every offsite bundle. A new set must be a legitimate successor of the last: if a
  record vanished or moved backwards the dump is **quarantined** and an alert is raised instead of being kept as
  "good". This is the interim form of the "export chain-head hashes outside the database" follow-up.
* **Retention:** newest 48, one per day for 14 days, one per week for 12 weeks. Anchors, `quarantine/` and
  `pre-restore-*` dumps are never pruned.
* **Offsite:** daily, age-encrypted, to the PC; only the public key is on this box.
* **`/data` (AMUL field, STM overlay, RAG files)** is archived in every set; those files are not in Postgres.
* **Restore drill** weekly, with `--prove-detection`: restores into a scratch database, verifies counts, anchors and
  the hash chain, then tampers with the copy and requires the verifier to fail.

### Restoring (destructive)
```bash
bin/jarvisctl restore --yes-destroy-current-data            # newest set; or --backup jarvis-20261005T120000Z
```
It verifies the set's checksums, takes a safety dump (`pre-restore-*.dump`), recreates both volumes empty,
restores in one transaction, and **leaves the app stopped** unless restored row counts, anchors and the history
chain (every tenant) all match; only then does it start the app and wait for `/ready`. From an offsite bundle:
`age -d -i jarvis-age-key.txt jarvis-<UTC>.bundle.tar.age | tar -x -C ~/jarvis-ledger/backups` first. That
unpacks the files and the `anchors/` log; then run restore as above.

## Honest limits

* **One box, one disk.** The hourly sets live on the same disk as the database. The offsite copy is what survives
  the box. The PC being off for more than 48 h raises an alert.
* **Not encrypted at rest.** The root filesystem is plain ext4: the database volume and the local backups
  (mode 600, but plaintext) are readable by anyone who takes the disk. Offsite copies are encrypted.
* **Wi-Fi** is the box's only network interface; an unreliable link delays the offsite copy, not the local backups.
* **RPO is one hour** (hourly dumps). Point-in-time recovery is a later option.
* A compromised **database owner** can rewrite history, heads and counters consistently; the anchors outside the
  database are what would expose it, as long as the PC's copies are intact.
* Docker's `docker` group is root-equivalent; the secrets are in env files readable by that group and by
  `docker inspect`.
* `emr_upsert` is still not atomic, and AMUL/STM/overlay/RAG state is per-instance (see `docs/POSTGRES.md`).

## Switching from the old instance later
The old service keeps running on 8001 with its own JSON file. When you are ready: stop writing to it, do a
`pg_import` **dry run first** against its `data/jarvis-store.json` (`docs/POSTGRES.md`), apply, verify, point your
hooks at 8011, and only then retire the old unit. Its data file is never modified by any of this.

## Rehearsal
`rehearse/rehearse-wsl.sh` (Git Bash on the Windows PC) runs all of the above in a WSL Ubuntu 24.04 with its own
Docker Engine and real systemd: hardening, hooks, backups, the anchor-regression alarm, offsite copy (PC off, wrong
host key), real systemd timers, a SIGKILL of the database, a `wsl --terminate` reboot, credential rotation, a
deliberate destruction of every volume, and a restore that must reproduce every record, hash, anchor and file.
