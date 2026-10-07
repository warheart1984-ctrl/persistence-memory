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

## Where things stand (2026-10-05)

* **Running:** the stack on `127.0.0.1:8011`, on Linux Mint 22.3 (4 CPUs, 15 GiB RAM, Wi-Fi only, disk not
  encrypted). Docker 29.1.3, compose 2.40.3 and age 1.1.1 come from Ubuntu's own archive.
* **Data:** 59 records imported from the PC's `jarvis-store.json` with `pg_import` (manifest in
  `~/jarvis-ledger/state/import-manifest.json`), plus one record migrated from the old service
  (`mem-da3ddefda22a`, copied from `mem-79f9aa7e7e44`). 60 live records.
* **The old service on 8001 is retired:** `persistence-memory.service` is stopped and disabled. Its data
  (`~/.local/share/persistence-memory/`) and `~/.config/persistence-memory/memory.env` are untouched, and
  `~/persistence-memory` is pinned at `d5157ba`. To bring it back:
  `systemctl --user enable --now persistence-memory.service`.
* **Firewall:** `ufw` is on and allows SSH only from `192.168.1.0/24`. Grafana, `llm-gateway` and Prometheus are
  on the box and untouched.
* **Timers (systemd user units, linger on):** hourly backup, daily offsite copy, weekly restore drill, 15-minute
  watchdog, 1-minute self-heal.
* **Checked on this hardware:** `jarvisctl smoke` passes every check; a `docker kill` of the database was back in
  48 s with `/ready` 200 and no lost record; a crash (`pg_ctl stop -m immediate`) was restarted by Docker itself in
  3 s; the restore drill passes; the offsite copy was verified by hash on the Windows side and decrypts.

## Using it from your Windows PC

The hooks and the MCP stdio proxy have **no default address**. Without `JARVIS_MEMORYBOARD_URL` they refuse with a
clear error, send nothing and never use the API key. (On the PC, port 8001 belongs to another program.) They send the
key only over `https` or to `127.0.0.1`, so it cannot leak onto the LAN. You need current code (`main`) for that, for
example the clean clone `C:\Users\randj\persistence-memory-main`.

**Set up permanently on the PC (verified 2026-10-05, including after a sign-out and sign-in):**

* **A tunnel that starts at logon.** The scheduled task `JarvisTunnel` runs `ssh -N -L 127.0.0.1:8011:127.0.0.1:8011`
  in a loop that retries every 10 seconds, so a killed or dropped tunnel is back in about 13 seconds. It listens on
  the PC's loopback only.
* **A dedicated tunnel key** (`~\.ssh\jarvis_tunnel_ed25519`). On the box it is restricted in
  `~/.ssh/authorized_keys` to forwarding `127.0.0.1:8011` and nothing else: no shell, no commands.
  ```
  restrict,port-forwarding,permitopen="127.0.0.1:8011",command="/bin/false" ssh-ed25519 AAAA... jarvis-tunnel@pc
  ```
* **The box's host key is pinned** in `~\.ssh\jarvis_known_hosts` (`StrictHostKeyChecking=yes`). Check the fingerprint
  against `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the box before trusting it. On Windows,
  `ssh-keyscan.exe` can hang; a plain `ssh` connection into a temporary `UserKnownHostsFile` captures the same key.
* **User environment variables** `JARVIS_MEMORYBOARD_URL=http://127.0.0.1:8011` and
  `JARVIS_API_KEY_FILE=C:\Users\randj\.jarvis-api-key`. The key file is readable only by that user and read-only on
  purpose, so after `jarvisctl rotate api-key` delete it before copying the new one:
  ```powershell
  scp jon@192.168.1.102:jarvis-ledger-src/deploy/mint/secrets/api-key $HOME\.jarvis-api-key
  icacls $HOME\.jarvis-api-key /inheritance:r /grant:r "${env:USERNAME}:R"
  ```

Check it any time:
```powershell
Get-NetTCPConnection -LocalPort 8011 -State Listen          # the tunnel
python $HOME\persistence-memory-main\agent-hooks\ping_memoryboard.py   # Health / Ready / memory count / "OK: service is live"
```
`ping_memoryboard.py` asks for up to 200 records and says when the list is capped; the API pages at 50 by default.

**Creating the task.** Do not nest double quotes inside `-Command "..."`: Windows splits the command line wrongly and
PowerShell fails to parse it (the task then exits 1 without starting a tunnel). Use an encoded command:
```powershell
$loop = @'
$a = @('-N','-L','127.0.0.1:8011:127.0.0.1:8011','-i',"$env:USERPROFILE\.ssh\jarvis_tunnel_ed25519",'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',"UserKnownHostsFile=$env:USERPROFILE\.ssh\jarvis_known_hosts",'-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-o','ExitOnForwardFailure=yes','jon@192.168.1.102')
while ($true) { & "$env:SystemRoot\System32\OpenSSH\ssh.exe" @a; Start-Sleep -Seconds 10 }
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($loop))
$act = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $enc"
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName JarvisTunnel -Action $act -Trigger $trg -Settings $set -Principal (New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited) -Force
```
A console window may flash at logon. Only one process can hold port 8011, so close any manual `ssh -L 8011:...` first.

**Undoing it.**
```powershell
Stop-ScheduledTask JarvisTunnel; Unregister-ScheduledTask JarvisTunnel -Confirm:$false
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -match 'jarvis_tunnel_ed25519' } | ForEach-Object { Stop-Process -Id $_.ProcessId }
[Environment]::SetEnvironmentVariable("JARVIS_MEMORYBOARD_URL",$null,"User")
[Environment]::SetEnvironmentVariable("JARVIS_API_KEY_FILE",$null,"User")
Remove-Item $HOME\.jarvis-api-key, $HOME\.ssh\jarvis_tunnel_ed25519*, $HOME\.ssh\jarvis_known_hosts
```
and on the box remove the tunnel key's line: `sed -i '/jarvis-tunnel@pc/d' ~/.ssh/authorized_keys`.

## Connecting an MCP agent (Grok, Codex, ...)

`mcp_server/ledger_stdio.py` is a small stdio MCP server for the ledger's plain API. Run it from a checkout of `main`
(on the PC: `C:\Users\randj\persistence-memory-main`, with the tunnel up). It fails closed: without
`JARVIS_MEMORYBOARD_URL` (no default address) or the key it refuses every tool and sends nothing, and the key is never
printed, returned or sent over plain http to another host.

| Tool (host adds the server name, e.g. `jarvis-ledger__recall`) | |
|---|---|
| `health` | liveness and readiness |
| `recall` | live memories, default 50, up to 200 (`limit`), optional `query` / `type` / `status` / `subject`, and `truth_scope` (`live` by default, which leaves archived records out; `all` or `archived` to see them); content shortened to `content_chars` (default 400, 0 = full); says when the result is capped |
| `get` | one memory by id |
| `write` | **off unless `JARVIS_LEDGER_MCP_WRITE=1`**, and then not even listed otherwise. Stores one draft `decision` (only decisions: the ledger refuses preferences, tasks and unevidenced facts under Clause V) with `source_agent` `grok-bot` (`JARVIS_LEDGER_MCP_SOURCE` renames it), needs the user's own words in `user_requested`, and refuses anything that looks like a credential |

For Grok, MCP servers live in `~/.grok/config.toml` (`C:\Users\randj\.grok\config.toml`):
```toml
[mcp_servers.jarvis-ledger]
command = 'C:\Users\randj\AppData\Local\Programs\Python\Python312\python.exe'
args = ['-B', 'C:\Users\randj\persistence-memory-main\mcp_server\ledger_stdio.py']
startup_timeout_sec = 15
tool_timeout_sec = 30
env = { JARVIS_MEMORYBOARD_URL = "http://127.0.0.1:8011", JARVIS_API_KEY_FILE = 'C:\Users\randj\.jarvis-api-key' }
```
Grok's `permission_mode = "always-approve"` runs tool calls without asking, so the guard on writing is the server's own
switch: add `JARVIS_LEDGER_MCP_WRITE = "1"` to `env` only for as long as you want Grok to be able to store things.
To undo it, delete the `[mcp_servers.jarvis-ledger]` block. Which Grok surfaces read `~/.grok/config.toml` is for the
Grok docs to say; the Grok CLI does, and `~/.grokbot/settings.json` has its own (empty) `mcpBoxServers` list.

## Day to day

| Command | What it does |
|---|---|
| `jarvisctl status` | containers, and how long ago the last backup / offsite copy / drill succeeded |
| `jarvisctl smoke [--no-write]` | the acceptance checks |
| `jarvisctl backup` / `prune [--dry-run]` | take a set now / apply retention |
| `jarvisctl drill [--prove-detection]` | restore the newest set into a scratch database and verify it |
| `jarvisctl restore --yes-destroy-current-data [--backup SET]` | **destructive**: see below |
| `jarvisctl offsite` | encrypt and send the newest set to the PC |
| `jarvisctl verify [tenant]` | recompute the history hash chain and every sealed block |
| `jarvisctl replay state\|receipt\|receipts\|check\|verify` | Replay Contracts (RC.Ledger.v1): replay the ledger at a point, issue receipts at **sealed** points, re-derive them (`check` asks the service, `verify` replays the raw rows in a one-off container). Needs the build that has Replay Contracts; see `docs/REPLAY_CONTRACTS.md` |
| `jarvisctl seal [--force\|--status]` | seal new history into Continuity Blocks now (or show the unsealed tail); the hourly timer is **not enabled** by default, see below |
| `jarvisctl attest status\|sign [--dry-run]\|init-key\|install-roots\|verify` | the host signer (schema v7 only): the signing key lives in `~/jarvis-ledger/keys/` and **never enters a backup, a volume or a container**; ceremony and routines in `docs/SIGNING_RUNBOOK.md`. `jarvis-sign.timer` is installed, **not enabled** |
| `jarvisctl watchdog` / `heal` | run the health checks / the self-heal once |
| `jarvisctl psql` | admin session inside the database container |
| `jarvisctl rotate app\|migrator\|api-key` | new random credential, never shown; api-key then needs copying to the PC |
| `jarvisctl logs [db\|app]`, `up`, `down`, `restart` | the obvious |

Alerts go to `~/jarvis-ledger/logs/alerts.log` and, when you are logged in to the desktop, a notification
(the same alert at most once every 6 hours). `~/jarvis-ledger/logs/` has one log per job.

### If the database stops
`restart: unless-stopped` makes Docker restart the database after a crash, an out-of-memory kill or a reboot. Docker
never restarts a container that was stopped or killed through its API (`docker kill`, `docker stop`), so a one-minute
timer (`jarvis-heal.timer`, `bin/heal.sh`) starts the database or app again if it finds it stopped, and raises a
notification. `jarvisctl down` leaves a marker so a stop on purpose is respected until the next `jarvisctl up`.

## Backups, precisely

* **Hourly set** in `~/jarvis-ledger/backups/jarvis-<UTC>.{dump,globals.sql,data.tar,counts,anchors,sha256}`.
  The dump is taken as the **superuser** because the ledger uses forced row-level security: an ordinary role would
  either fail or, with `--enable-row-security`, silently write **zero rows**. Each dump is verified (table of
  contents, per-table row counts read out of the dump itself, shrink alarm).
* **Anchors** - where each tenant's history hash chain ends - are written next to every set, in a hash-chained log
  that is never pruned, and inside every offsite bundle. A new set must be a legitimate successor of the last: if a
  record vanished or moved backwards the dump is **quarantined** and an alert is raised instead of being kept as
  "good". This is the interim form of the "export chain-head hashes outside the database" follow-up.
* **Block anchors (schema v6):** the anchors also carry one line per sealed Continuity Block (`block|<tenant>|<height>|<last_seq>|<block_hash>`).
  A new set must still contain every block of the last set with the same hash: a removed block, or blocks re-sealed after
  a rewritten history, quarantines the dump and raises the same alert. The database alone cannot see either of those.
  `jarvisctl drill --prove-detection` proves it too, by removing the newest block from the scratch copy.
* **Retention:** newest 48, one per day for 14 days, one per week for 12 weeks. Anchors, `quarantine/` and
  `pre-restore-*` dumps are never pruned.
* **`/data` (AMUL field, STM overlay, RAG files)** is archived in every set; those files are not in Postgres.
* **Restore drill** weekly, with `--prove-detection`: restores into a scratch database, verifies counts, anchors and
  the hash chain, **replays the copy at each tenant's last anchored block against the set's own anchors** (skipped with a warning
  when the app image predates Replay Contracts or the set has no sealed blocks), then tampers with the copy and requires the verifier,
  the anchors and the replay to fail.

### The offsite copy
Daily, the newest complete set is packed, encrypted with age and sent by `scp` to `G:\jarvis-backups` on the PC,
then hashed again on the PC (PowerShell `Get-FileHash`) and compared. Only the age **public** key is on this box
(`secrets/age_recipient.txt`), so a stolen backup directory is unreadable. The PC's host key is pinned in
`secrets/offsite_known_hosts`; settings are in `secrets/offsite.conf`.

* **G: must be plugged in and the PC on.** If not, the copy fails and the watchdog alerts after 48 h.
* **The age private key lives only on a USB stick** (`E:\jarvis-keys\`), off the backup drive. It was generated on
  this box, copied to the stick, checked by hash and then deleted from the box. It is the only way to read an
  offsite copy, and there is **one** copy of it: make a second (a printout works; it is 189 bytes of text).
* **Setting up again:** on the PC, enable OpenSSH Server and create `G:\jarvis-backups`; make a dedicated key on the
  box (`ssh-keygen -t ed25519 -N '' -f ~/.ssh/jarvis_offsite_ed25519`) and put its public half in the Windows
  account's `authorized_keys` (for an administrator that is `C:\ProgramData\ssh\administrators_authorized_keys`);
  pin the host key with `ssh-keyscan -t ed25519 <PC> > secrets/offsite_known_hosts` and compare
  `ssh-keygen -lf` with `ssh-keygen -lf C:\ProgramData\ssh\ssh_host_ed25519_key.pub` on the PC; then
  `cp offsite.conf.example secrets/offsite.conf`, fill it in, and run `jarvisctl offsite` once by hand.
* **The offsite SSH user is currently an administrator** on the PC, so that key can run commands there. A normal
  Windows user that owns only `G:\jarvis-backups` would be safer. Not done yet.

### Sealing blocks

`jarvisctl seal` asks the ledger (operator key, `POST /api/jarvis/blocks/seal`) to seal whatever is due: a block is made when
500 history entries are waiting or the oldest waiting entry is an hour old; `--force` seals what is waiting, `--status` only
shows the tail. `jarvis-seal.timer` (five minutes before each hourly backup, so the backup's anchors include the newest
block) is **installed by `install-units.sh` but not enabled**. Enable it once the ledger is at schema v6:
`systemctl --user enable --now jarvis-seal.timer`. The watchdog only watches the seal after it has succeeded once
(`state/seal.last_ok`); remove that file after turning the timer off on purpose. `jarvisctl verify` reports how many entries
are still unsealed and warns when the oldest is more than 24 hours old.

### Upgrading to schema v6 and rolling back

Backup first (`jarvisctl backup`, then `jarvisctl drill`), then pull and `jarvisctl up` (v5 to v6, additive). The code checks the
schema version exactly, so **v5 code refuses a v6 database and v6 code refuses a v5 one: rolling back means checking out the old
code and restoring the backup taken before the upgrade** (writes made after it are lost). After such a rollback the newer
backup sets and anchors no longer fit the restored database (their blocks "vanished"), so move the newer `jarvis-*` sets and the
newer `anchors/anchors-*.txt` files aside before the next backup, or it will quarantine its dump.

### Signatures (schema v7)

Backup sets carry `<set>.signatures.json` (the attestations, trust statements and blocks, for the PC witness) and the anchors carry
`att|` and `trust|` lines: a signing log that loses or rewrites an entry is quarantined like a block would be. `backup.sh` refuses to
publish a set containing the signing key (exact secret in any encoding, or a private-key header in a file); key-shaped text inside the
database only warns. The drill re-checks restored signatures with the pinned roots (`secrets/trust-roots.pub`, public keys only,
created empty by `jarvisctl up`) and, with `--prove-detection`, proves a damaged signature is caught. The watchdog watches
`state/sign.last_ok` (once the signer has succeeded) and warns when no cosign has been posted for 72 hours. Rollback: v6 code refuses
v7 and the reverse, so, as for v6, roll back by checking out the old code and restoring the backup taken before the upgrade.

### Restoring (destructive)
```bash
bin/jarvisctl restore --yes-destroy-current-data            # newest set; or --backup jarvis-20261005T120000Z
```
It verifies the set's checksums, takes a safety dump (`pre-restore-*.dump`), recreates both volumes empty,
restores in one transaction, and **leaves the app stopped** unless restored row counts, anchors and the history
chain (every tenant) all match; only then does it start the app and wait for `/ready`. From an offsite bundle:
`age -d -i <private-key-file> jarvis-<UTC>.bundle.tar.age | tar -x -C ~/jarvis-ledger/backups` first. That
unpacks the files and the `anchors/` log; then run restore as above.

## Setting it up from scratch

Steps that need `sudo` are yours to run; nothing here asks for or stores a sudo password.

1. **Docker, compose and age** (from Ubuntu's archive), then reboot so the docker group reaches your systemd user
   manager:
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 age
   sudo usermod -aG docker "$USER"      # the docker group is root-equivalent: keep it to your own user
   ```
2. **Firewall.** Put the SSH rule in before enabling, or you will lock yourself out:
   ```bash
   sudo ufw default deny incoming && sudo ufw default allow outgoing
   sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
   sudo ufw show added && sudo ufw enable
   ```
   A Docker **published** port bypasses ufw, which is why this stack binds `127.0.0.1` explicitly and publishes
   nothing for the database.
3. **The code and secrets:**
   ```bash
   git clone https://github.com/warheart1984-ctrl/persistence-memory.git ~/jarvis-ledger-src
   cd ~/jarvis-ledger-src/deploy/mint
   cp .env.example .env            # port 8011, local tuning, no secrets
   bin/jarvisctl secrets           # random passwords + API key, mode 600, never printed
   ```
4. **The offsite copy**, as described above (age public key in `secrets/age_recipient.txt`).
5. **Start, check, schedule:**
   ```bash
   bin/jarvisctl up                  # db -> migrate -> app; builds the images the first time
   bin/jarvisctl smoke               # every line must say PASS
   bin/install-units.sh              # the five timers (+ the seal and sign timers, installed but not enabled); the offsite timer needs secrets/offsite.conf first
   bin/jarvisctl backup && bin/jarvisctl drill --prove-detection
   bin/jarvisctl offsite
   ```
6. **Importing an existing JSON ledger** (`docs/POSTGRES.md`): the target tenant must be empty. Dry run first
   (the default; it rolls back), then `--apply --manifest <path not next to the source>`, then `--verify` and
   `bin/jarvisctl verify`. The source file is only read and its hash is re-checked.

## Honest limits

* **One box, one disk.** The hourly sets live on the same disk as the database. The offsite copy is what survives
  the box.
* **Not encrypted at rest.** The root filesystem is plain ext4: the database volume and the local backups
  (mode 600, but plaintext) are readable by anyone who takes the disk. Offsite copies are encrypted.
* **Wi-Fi** is the box's only network interface; an unreliable link delays the offsite copy, not the local backups.
* **RPO is one hour** (hourly dumps). Point-in-time recovery is a later option.
* **Automatic security updates are not set up** (`unattended-upgrades` is not installed).
* A compromised **database owner** can rewrite history, heads, counters and blocks consistently; the anchors outside the
  database are what would expose it, as long as the PC's copies are intact. Blocks are **not signed**.
* **No LLM adapter on port 8011.** The old service on 8001 called llm-gateway (tenant `memory`, `JARVIS_LLM_*`
  in `~/.config/persistence-memory/memory.env`) for the AMUL LLM adapter. The 8011 stack sets no `JARVIS_LLM_*`
  variables, so that adapter is off. Recall, writes, history, backups and the rest do not use it. To get it back,
  the app container needs `JARVIS_LLM_URL`, `JARVIS_LLM_API`, `JARVIS_LLM_MODEL` and a key reachable from inside
  Docker, which means llm-gateway listening beyond `127.0.0.1` or a host-gateway route. That is a deliberate
  decision, so it is not done by default. (2026-10-05: the owner does not use the adapter.)
* Docker's `docker` group is root-equivalent; the secrets are in env files readable by that group and by
  `docker inspect`.
* `emr_upsert` is still not atomic, and AMUL/STM/overlay/RAG state is per-instance (see `docs/POSTGRES.md`).

## Rehearsal
`rehearse/rehearse-wsl.sh` (Git Bash on the Windows PC) runs the whole thing in a WSL Ubuntu 24.04 with its own
Docker Engine and real systemd: hardening, hooks, backups, the anchor-regression alarm, offsite copy (PC off, wrong
host key), real systemd timers, a `docker kill` of the database and the self-heal, a crash restarted by Docker's own
policy, a `wsl --terminate` reboot, credential rotation, a deliberate destruction of every volume, and a restore
that must reproduce every record, hash, anchor and file. The self-heal and crash checks were added after the last
full rehearsal run; they were run on the real box, not re-run in WSL.
