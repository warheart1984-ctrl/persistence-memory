#!/usr/bin/env bash
# The rehearsal scenario. Run INSIDE the WSL distro as the normal user, by rehearse-wsl.sh:
#   scenario.sh phase-a | phase-b | teardown
# Prints ok/FAIL per check; exit status is non-zero if anything failed.
set -uo pipefail

PHASE="${1:-}"
R="$HOME/jarvis-rehearsal"
SRC="$R/src"
export JARVIS_HOME="$R/home"
export JARVIS_APP_PORT=18001            # never 8001/8011: a real instance may own those
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export NOTIFY_CALLS="$R/notify-send.calls"
export PATH="$SRC/deploy/mint/rehearse/bin:$PATH"   # the fake notify-send wins over the real one
export DISK_MIN_FREE_PCT=1              # the disk is not what is being tested (dedicated check below)

BIN="$SRC/deploy/mint/bin"
SEC="$SRC/deploy/mint/secrets"
BK="$JARVIS_HOME/backups"
PC=jarvis-rehearsal-pc

PASS=0; FAIL=0
ok()      { PASS=$((PASS + 1)); printf '  ok    %s\n' "$*"; }
bad()     { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }
section() { printf '\n=== %s\n' "$*"; }
t()  { local d="$1"; shift; if "$@" >"$R/t.out" 2>&1; then ok "$d"; else bad "$d"; sed 's/^/        | /' "$R/t.out" | tail -8; fi; }
tn() { local d="$1"; shift; if "$@" >"$R/t.out" 2>&1; then bad "$d (it should have failed)"; sed 's/^/        | /' "$R/t.out" | tail -5; else ok "$d"; fi; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', wanted '$3')"; fi; }
has() { if grep -q -- "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (no '$2' in $3)"; fi; }
jget() { python3 -c "import sys, json; d = json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
api()  { local m="$1" p="$2"; shift 2; curl -sS -m 30 -X "$m" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' "$@" "http://127.0.0.1:$JARVIS_APP_PORT$p"; }
code() { curl -sS -m 30 -o /dev/null -w '%{http_code}' "$@"; }
psql_pg() { docker exec -i -u postgres jarvis-db psql -X -At -d jarvis "$@"; }
snapshot() { api GET "/api/jarvis/memory?limit=200" | python3 -c "
import sys, json
rows = json.load(sys.stdin)['memories']
print('\n'.join(sorted(f\"{m['id']}:v{m['version']}:{m['status']}:{m['content_sha256'][:12]}\" for m in rows)))"; }
mk() { api POST /api/jarvis/memory -d "$(printf '{"content":"%s","source_agent":"rehearsal","session_id":"rehearse-1","type":"fact","subject":"%s","tags":["rehearsal"]}' "$1" "$2")"; }
newest_set() { ls -1 "$BK" | sed -n 's/^\(jarvis-[0-9]\{8\}T[0-9]\{6\}Z\)\.sha256$/\1/p' | LC_ALL=C sort | tail -1; }
wait_for() { local i=0; while [ "$i" -lt "$2" ]; do eval "$1" >/dev/null 2>&1 && return 0; sleep 2; i=$((i + 2)); done; return 1; }
ready_code() { code "http://127.0.0.1:$JARVIS_APP_PORT/ready"; }
live_anchors() { ( pg_exec() { docker exec -i -u postgres jarvis-db "$@"; }; source "$BIN/anchors.sh"; anchors_from_db ); }
finish() { printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"; exit $(( FAIL > 0 )); }

# =================================================================================================================
prepare() {
  rm -rf "$R"; mkdir -p "$SRC" "$JARVIS_HOME"
  # the working tree minus anything private: no data/, no .git, no secrets
  tar -C "$SRC_FROM" --exclude='__pycache__' --exclude='*.pyc' --exclude='deploy/mint/secrets' \
      -cf - app mcp_server agent-hooks pyproject.toml deploy | tar -xf - -C "$SRC"
  chmod +x "$BIN"/*.sh "$BIN/jarvisctl" "$SRC/deploy/mint/db/initdb/10-roles.sh" "$SRC"/deploy/mint/rehearse/*.sh "$SRC"/deploy/mint/rehearse/bin/*
  cd "$SRC/deploy/mint" || exit 2
}

# =================================================================================================================
phase_a() {
  prepare
  section "A1. static checks (shellcheck, syntax, line endings, systemd units, retention)"
  t "shellcheck is clean on every script" shellcheck -x -S warning "$BIN"/*.sh "$BIN/jarvisctl" db/initdb/10-roles.sh rehearse/*.sh
  t "bash -n accepts every script" bash -c 'for f in '"$BIN"'/*.sh '"$BIN"'/jarvisctl; do bash -n "$f" || exit 1; done'
  tn "no script, unit or config has CRLF line endings" grep -rlI $'\r' bin db systemd docker-compose.yml app
  t "install-units renders the systemd units" "$BIN/install-units.sh" --dest "$R/units" --no-enable
  t "no placeholder is left in a rendered unit" bash -c "! grep -l '@[A-Z_]*@' $R/units/*"
  t "systemd-analyze verify accepts the rendered units" systemd-analyze verify --man=no "$R"/units/*.service "$R"/units/*.timer

  P="$R/prune-test"; mkdir -p "$P/anchors" "$P/quarantine"
  python3 - "$P" <<'PY'
import datetime, os, sys
d = sys.argv[1]
now = datetime.datetime(2026, 10, 5, 12, 0, 0)
for h in range(130 * 24):
    t = now - datetime.timedelta(hours=h)
    base = f"jarvis-{t:%Y%m%dT%H%M%SZ}"
    for ext in ("dump", "globals.sql", "data.tar", "counts", "anchors", "sha256"):
        open(f"{d}/{base}.{ext}", "w").write("x")
open(f"{d}/anchors/anchors-20260101T000000Z.txt", "w").write("# prev_sha256=0\n")
open(f"{d}/pre-restore-20260101T000000Z.dump", "w").write("x")
open(f"{d}/quarantine/jarvis-20260101T000000Z.dump", "w").write("x")
PY
  sets_before=$(ls "$P"/*.sha256 | wc -l)
  JARVIS_BACKUP_DIR="$P" "$BIN/prune.sh" --dry-run --now 20261005T120000Z >/dev/null 2>&1
  eq "prune --dry-run deletes nothing" "$(ls "$P"/*.sha256 | wc -l)" "$sets_before"
  JARVIS_BACKUP_DIR="$P" "$BIN/prune.sh" --now 20261005T120000Z >/dev/null 2>&1
  kept=$(ls "$P"/*.sha256 | wc -l)
  python3 - "$P" <<'PY' && ok "retention keeps the newest 48, one per day for 14 days, one per week for 12 weeks (kept $kept of $sets_before)" || bad "retention policy violated"
import datetime, glob, os, sys
d = sys.argv[1]
now = datetime.datetime(2026, 10, 5, 12, 0, 0)
kept = sorted(os.path.basename(p)[len("jarvis-"):-len(".sha256")] for p in glob.glob(d + "/jarvis-*.sha256"))
ts = [datetime.datetime.strptime(k, "%Y%m%dT%H%M%SZ") for k in kept]
allsets = [now - datetime.timedelta(hours=h) for h in range(130 * 24)]
assert all(now - datetime.timedelta(hours=h) in ts for h in range(48)), "a recent set was deleted"
for day in range(14):
    date = (now - datetime.timedelta(days=day)).date()
    assert max(t for t in allsets if t.date() == date) in ts, f"newest set of {date} missing"
for wk in range(12):
    iso = (now - datetime.timedelta(days=7 * wk)).isocalendar()[:2]
    assert max(t for t in allsets if t.isocalendar()[:2] == iso and (now - t).days < 84) in ts, f"newest set of week {iso} missing"
assert all((now - t).days < 84 for t in ts), "a set older than 12 weeks survived"
assert len(ts) < 130 * 24 // 4, "almost nothing was pruned"
for k in kept:
    for ext in ("dump", "globals.sql", "data.tar", "counts", "anchors", "sha256"):
        assert os.path.exists(f"{d}/jarvis-{k}.{ext}"), f"kept set {k} lost .{ext}"
assert len(glob.glob(d + "/jarvis-*.dump")) == len(kept), "orphaned dumps remain"
for f in ("anchors/anchors-20260101T000000Z.txt", "pre-restore-20260101T000000Z.dump", "quarantine/jarvis-20260101T000000Z.dump"):
    assert os.path.exists(f"{d}/{f}"), f"{f} was deleted"
PY

  section "A2. secrets, age key, first start"
  rm -rf "$SEC"
  "$BIN/gen-secrets.sh" > "$R/gen.out" 2>&1
  t "gen-secrets created the four files" test -s "$SEC/db.env" -a -s "$SEC/app.env" -a -s "$SEC/migrate.env" -a -s "$SEC/api-key"
  eq "secret files are mode 600" "$(stat -c %a "$SEC/db.env" "$SEC/app.env" "$SEC/migrate.env" "$SEC/api-key" | sort -u)" "600"
  eq "secrets directory is mode 700" "$(stat -c %a "$SEC")" "700"
  leaked=0
  while IFS= read -r v; do [ -n "$v" ] && grep -qF "$v" "$R/gen.out" && leaked=1; done < <(sed -n 's/^POSTGRES_PASSWORD=//p;s/^JARVIS_APP_PASSWORD=//p;s/^JARVIS_MIGRATOR_PASSWORD=//p' "$SEC/db.env"; cat "$SEC/api-key")
  eq "gen-secrets printed no secret value" "$leaked" "0"
  tn "gen-secrets refuses to overwrite existing secrets" "$BIN/gen-secrets.sh"
  KEY="$(cat "$SEC/api-key")"
  age-keygen -o "$R/age-key.txt" >/dev/null 2>&1
  age-keygen -y "$R/age-key.txt" > "$SEC/age_recipient.txt"
  t "age keypair created; only the PUBLIC key is in secrets/" grep -q '^age1' "$SEC/age_recipient.txt"
  tn "the private age key is not anywhere under the deploy tree" grep -rq "AGE-SECRET""-KEY" "$SRC/deploy/mint"
  t "docker compose config is valid" docker compose config -q
  "$BIN/jarvisctl" up > "$R/up.out" 2>&1; rc=$?
  eq "jarvisctl up succeeds (db -> migrate -> app)" "$rc" "0"
  [ "$rc" -ne 0 ] && { tail -30 "$R/up.out"; docker logs jarvis-db 2>&1 | tail -20; docker logs jarvis-migrate 2>&1 | tail -20; }
  eq "migrate exited 0" "$(docker inspect -f '{{.State.ExitCode}}' jarvis-migrate 2>/dev/null)" "0"
  has "migrate reported schema version 4" "version 4" <(docker logs jarvis-migrate 2>&1)

  section "A3. acceptance checks (smoke.sh) on the fresh stack"
  "$BIN/smoke.sh" > "$R/smoke.out" 2>&1; rc=$?
  eq "smoke.sh passes" "$rc" "0"
  grep -E '^(PASS|FAIL)' "$R/smoke.out" | sed 's/^/        /'

  section "A4. hooks talk to the protected server (API key, loopback only)"
  export JARVIS_MEMORYBOARD_URL="http://127.0.0.1:$JARVIS_APP_PORT"
  tn "ping without a key is refused (401)" env -u JARVIS_API_KEY -u JARVIS_API_KEY_FILE python3 "$SRC/agent-hooks/ping_memoryboard.py"
  JARVIS_API_KEY_FILE="$SEC/api-key" python3 "$SRC/agent-hooks/ping_memoryboard.py" > "$R/ping.out" 2>&1
  t "ping with JARVIS_API_KEY_FILE works" grep -q "OK: service is live" "$R/ping.out"
  tn "hooks refuse to send the key over plain http to a non-loopback host" env JARVIS_API_KEY_FILE="$SEC/api-key" JARVIS_MEMORYBOARD_URL=http://192.0.2.9:8001 python3 -c "
import sys; sys.path.insert(0, '$SRC/agent-hooks'); import jarvis_common as c
payload, err = c.try_http_json('GET', '/health'); print(err); sys.exit(0 if payload else 1)"
  unset JARVIS_MEMORYBOARD_URL

  section "A5. seed some history"
  ids=()
  for i in $(seq 1 25); do
    id="$(mk "rehearsal record number $i about gardening and soil" "topic-$((i % 5))" | jget "d['memory']['id']")"; ids+=("$id")
  done
  eq "25 records created" "${#ids[@]}" "25"
  for i in 0 1 2 3 4; do api PATCH "/api/jarvis/memory/${ids[$i]}" -d '{"confidence":0.9,"expected_version":1}' >/dev/null; done
  eq "a stale expected_version is a 409" "$(code -X PATCH -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"confidence":0.1,"expected_version":1}' "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory/${ids[0]}")" "409"
  api DELETE "/api/jarvis/memory/${ids[24]}" >/dev/null; api DELETE "/api/jarvis/memory/${ids[23]}" >/dev/null
  eq "the AMUL anchor call writes its file to the appdata volume" "$(code -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"anchor_all":true}' "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory/amul/anchor")" "200"
  api POST /api/jarvis/memory/emr/excite -d '{"query":"gardening soil","session_key":"rehearse","theta_promote":0.0}' >/dev/null
  eq "23 records remain" "$(api GET '/api/jarvis/memory?limit=200' | jget "len(d['memories'])")" "23"
  eq "history verifies" "$(api GET /api/jarvis/memory/history/verify | jget "d['ok']")" "True"

  section "A6. backups"
  "$BIN/backup.sh" > "$R/b1.out" 2>&1; eq "backup #1 succeeds" "$?" "0"
  s1="$(newest_set)"
  for part in dump globals.sql data.tar counts anchors sha256; do t "set has .$part" test -s "$BK/$s1.$part"; done
  t "checksums verify" bash -c "cd '$BK' && sha256sum -c --quiet '$s1.sha256'"
  eq "dump file starts with the pg_dump custom-format magic" "$(head -c 5 "$BK/$s1.dump")" "PGDMP"
  has "counts file: 23 memories" "^memories=23$" "$BK/$s1.counts"
  t "anchors file has chain heads" grep -q '^head|operator|' "$BK/$s1.anchors"
  eq "one entry in the long-lived anchors log" "$(ls "$BK"/anchors | wc -l)" "1"
  eq "backup files are private (mode 600)" "$(stat -c %a "$BK/$s1.dump" "$BK/$s1.globals.sql")" "$(printf '600\n600')"
  eq "no temp directory left behind" "$(ls -a "$BK" | grep -c '^\.tmp-')" "0"
  t "the data archive holds the AMUL field file" bash -c "tar -tf '$BK/$s1.data.tar' | grep -q amul-field.jsonl"
  sleep 1
  for i in 1 2 3; do mk "second wave record $i about irrigation" "topic-irrigation" >/dev/null; done
  api PATCH "/api/jarvis/memory/${ids[5]}" -d '{"subject":"revised"}' >/dev/null
  sleep 1
  "$BIN/backup.sh" > "$R/b2.out" 2>&1; eq "backup #2 succeeds (a legitimate successor of #1)" "$?" "0"
  s2="$(newest_set)"
  has "counts file: 26 memories" "^memories=26$" "$BK/$s2.counts"
  eq "anchors log grew" "$(ls "$BK"/anchors | wc -l)" "2"
  t "the anchors log chain verifies" bash -c "source $BIN/anchors.sh; anchors_verify_chain '$BK/anchors'"

  section "A7. tamper detection on the way in"
  head_row="$(psql_pg -c "select id || '|' || last_seq from jarvis.chain_heads where last_seq > 3 order by id limit 1")"
  hid="${head_row%%|*}"; hseq="${head_row##*|}"
  psql_pg -c "update jarvis.chain_heads set last_seq = last_seq - 1 where id = '$hid'" >/dev/null
  tn "pg_verify sees the regressed chain head" "$BIN/jarvisctl" verify operator
  sleep 1
  "$BIN/backup.sh" > "$R/b3.out" 2>&1; rc=$?
  eq "backup REFUSES to bless a regressed ledger (non-zero exit)" "$([ $rc -ne 0 ] && echo refused || echo accepted)" "refused"
  t "the suspicious dump was quarantined, not kept as a good set" bash -c "ls '$BK/quarantine'/jarvis-*.dump"
  eq "no new good set was published" "$(newest_set)" "$s2"
  has "alerts.log recorded the regression" "ANCHORS REGRESSED" "$JARVIS_HOME/logs/alerts.log"
  has "a desktop notification was raised (notify-send stand-in)" "ANCHORS REGRESSED" "$NOTIFY_CALLS"
  n_before="$(grep -c 'ANCHORS REGRESSED' "$NOTIFY_CALLS")"
  "$BIN/backup.sh" > /dev/null 2>&1
  eq "an identical alert is not shown twice (deduplicated), but is still logged" "$(grep -c 'ANCHORS REGRESSED' "$NOTIFY_CALLS")" "$n_before"
  psql_pg -c "update jarvis.chain_heads set last_seq = $hseq where id = '$hid'" >/dev/null
  t "after repairing the head, pg_verify is clean again" "$BIN/jarvisctl" verify operator
  sleep 1
  "$BIN/backup.sh" > "$R/b4.out" 2>&1; eq "backup succeeds again once the ledger is sound" "$?" "0"
  s4="$(newest_set)"

  section "A8. offsite copy to the (stand-in) Windows PC"
  docker build -q -t "$PC" "$SRC/deploy/mint/rehearse/pc" >/dev/null || bad "could not build the PC stand-in"
  docker rm -f "$PC" >/dev/null 2>&1
  docker run -d --name "$PC" -p 127.0.0.1:2222:22 "$PC" >/dev/null
  ssh-keygen -q -t ed25519 -N '' -f "$R/offsite-key" >/dev/null
  docker exec -i "$PC" sh -c 'cat > /home/pcuser/.ssh/authorized_keys && chown pcuser:pcuser /home/pcuser/.ssh/authorized_keys && chmod 600 /home/pcuser/.ssh/authorized_keys' < "$R/offsite-key.pub"
  sleep 2
  ssh-keyscan -p 2222 127.0.0.1 > "$SEC/offsite_known_hosts" 2>/dev/null
  cat > "$SEC/offsite.conf" <<EOF
OFFSITE_HOST=127.0.0.1
OFFSITE_USER=pcuser
OFFSITE_PATH=/home/pcuser/jarvis-backups
OFFSITE_KEY=$R/offsite-key
OFFSITE_PORT=2222
OFFSITE_OS=linux
EOF
  "$BIN/offsite.sh" > "$R/o1.out" 2>&1; eq "offsite copy succeeds" "$?" "0"
  t "the bundle arrived on the PC" docker exec "$PC" test -s "/home/pcuser/jarvis-backups/$s4.bundle.tar.age"
  docker cp "$PC:/home/pcuser/jarvis-backups/$s4.bundle.tar.age" "$R/bundle.age" >/dev/null
  tn "the bundle is ciphertext (no PGDMP, no plaintext)" bash -c "grep -aq PGDMP '$R/bundle.age' || grep -aq 'gardening' '$R/bundle.age'"
  tn "the box itself cannot decrypt it (no private key on the box)" age -d -i "$SEC/age_recipient.txt" "$R/bundle.age"
  age -d -i "$R/age-key.txt" "$R/bundle.age" 2>/dev/null | tar -t > "$R/bundle.list"
  for part in dump globals.sql data.tar counts anchors sha256; do has "decrypted bundle contains .$part" "$s4.$part" "$R/bundle.list"; done
  has "decrypted bundle contains the anchors log" "anchors/anchors-" "$R/bundle.list"
  tn "the plaintext bundle is never left on the box" bash -c "ls $JARVIS_HOME/state/offsite-tmp/* 2>/dev/null | grep -q ."
  "$BIN/offsite.sh" > "$R/o2.out" 2>&1; eq "a second run with nothing new is a no-op success" "$?" "0"
  mk "third wave record about composting" "topic-compost" >/dev/null; sleep 1; "$BIN/backup.sh" >/dev/null 2>&1
  docker stop "$PC" >/dev/null
  tn "offsite fails when the PC is off" "$BIN/offsite.sh"
  has "the failure was logged to alerts.log" "offsite" "$JARVIS_HOME/logs/alerts.log"
  docker start "$PC" >/dev/null; sleep 3
  cp "$SEC/offsite_known_hosts" "$R/known_hosts.good"
  ssh-keygen -q -t ed25519 -N '' -f "$R/otherhost" >/dev/null && printf '[127.0.0.1]:2222 %s\n' "$(cut -d' ' -f1-2 "$R/otherhost.pub")" > "$SEC/offsite_known_hosts"
  tn "offsite refuses a host whose key does not match the pinned one" "$BIN/offsite.sh"
  cp "$R/known_hosts.good" "$SEC/offsite_known_hosts"
  "$BIN/offsite.sh" > "$R/o3.out" 2>&1; eq "offsite succeeds again once the PC is back and the key matches" "$?" "0"

  section "A9. real systemd user timers"
  printf 'JARVIS_APP_PORT=%s\nDISK_MIN_FREE_PCT=1\n' "$JARVIS_APP_PORT" > "$JARVIS_HOME/systemd.env"
  t "the user manager is reachable" systemctl --user show -p Version
  t "user services can run docker (the manager has the docker group)" systemd-run --user --wait --collect --quiet --pipe docker ps -q
  "$BIN/install-units.sh" > "$R/iu.out" 2>&1; eq "install-units enables the timers" "$?" "0"
  for tm in backup offsite drill watchdog; do t "timer jarvis-$tm.timer is active" systemctl --user is-active "jarvis-$tm.timer"; done
  eq "all four timers have a next run scheduled" "$(systemctl --user list-timers 'jarvis-*' --no-legend 2>/dev/null | grep -c jarvis)" "4"
  sets_before=$(ls "$BK"/*.sha256 | wc -l); sleep 1
  systemctl --user start jarvis-backup.service; rc=$?
  eq "the backup unit runs to success under systemd" "$rc/$(systemctl --user show -p Result --value jarvis-backup.service)" "0/success"
  eq "...and published a new set" "$(ls "$BK"/*.sha256 | wc -l)" "$((sets_before + 1))"
  systemctl --user start jarvis-offsite.service; rc=$?
  eq "the offsite unit runs to success under systemd" "$rc/$(systemctl --user show -p Result --value jarvis-offsite.service)" "0/success"

  section "A10. watchdog"
  "$BIN/watchdog.sh" > "$R/w1.out" 2>&1; eq "watchdog reports exactly one thing: the drill has never run" "$?" "1"
  has "...the restore drill" "restore drill has never succeeded" "$R/w1.out"
  echo $(( $(date +%s) - 20000 )) > "$JARVIS_HOME/state/backup.last_ok"
  "$BIN/watchdog.sh" > "$R/w2.out" 2>&1
  has "a stale backup is reported" "hourly backup last succeeded" "$R/w2.out"
  date +%s > "$JARVIS_HOME/state/backup.last_ok"

  section "A11. restore drill (scratch database; the live one is untouched)"
  before_live="$(psql_pg -c 'select count(*) from jarvis.memories')"
  "$BIN/drill.sh" --prove-detection > "$R/d1.out" 2>&1; eq "drill passes" "$?" "0"
  has "drill proved the tamper alarm fires" "tampering with the scratch copy was detected" "$R/d1.out"
  eq "no scratch container or network is left" "$(docker ps -a --format '{{.Names}}' | grep -c jarvis-drill)$(docker network ls --format '{{.Name}}' | grep -c jarvis-drill)" "00"
  DISK_MIN_FREE_PCT=100 "$BIN/watchdog.sh" > "$R/wdisk.out" 2>&1
  has "the watchdog flags a nearly-full backup disk" "backup disk" "$R/wdisk.out"
  eq "the live database is unchanged by the drill" "$(psql_pg -c 'select count(*) from jarvis.memories')" "$before_live"
  "$BIN/watchdog.sh" > "$R/w3.out" 2>&1; eq "watchdog is now fully quiet" "$?" "0"

  section "A12. outage: database killed hard (SIGKILL)"
  n_before="$(api GET '/api/jarvis/memory?limit=200' | jget "len(d['memories'])")"
  docker kill jarvis-db >/dev/null
  sleep 2
  hdr="$(curl -sS -m 20 -D - -o "$R/outage.body" -H "X-API-Key: $KEY" "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory")"
  echo "$hdr" | head -1 | grep -q ' 503' && ok "reads return 503 during the outage" || bad "reads during the outage: $(echo "$hdr" | head -1)"
  echo "$hdr" | grep -qi '^retry-after: 5' && ok "503 carries Retry-After" || bad "no Retry-After on the 503"
  has "503 body has the ledger_unavailable code" '"code":"ledger_unavailable"' "$R/outage.body"
  tn "the outage body leaks no host/user/password" grep -Eiq 'jarvis-db|jarvis_app|postgresql://|password' "$R/outage.body"
  eq "a write during the outage is a 503" "$(code -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"content":"written during an outage","source_agent":"t","session_id":"s","type":"fact"}' "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory")" "503"
  eq "/ready is 503 during the outage" "$(ready_code)" "503"
  eq "/health (liveness) stays 200 during the outage" "$(code "http://127.0.0.1:$JARVIS_APP_PORT/health")" "200"
  if wait_for '[ "$(docker inspect -f "{{.State.Running}}" jarvis-db)" = true ]' 30; then ok "docker restarted the killed database by itself (restart policy)"; else echo "        note: docker did not auto-restart an explicitly killed container; the operator would run: jarvisctl up"; docker start jarvis-db >/dev/null; fi
  t "database is healthy again" wait_for '[ "$(docker inspect -f "{{.State.Health.Status}}" jarvis-db)" = healthy ]' 120
  t "the app's /ready recovers on its own" wait_for '[ "$(ready_code)" = 200 ]' 120
  eq "no partial write: the record count is exactly what it was" "$(api GET '/api/jarvis/memory?limit=200' | jget "len(d['memories'])")" "$n_before"
  t "history still verifies after the crash" "$BIN/jarvisctl" verify operator

  section "A13. stop and start"
  before="$(snapshot)"
  "$BIN/jarvisctl" down >/dev/null 2>&1
  eq "after down, the app is unreachable" "$(code -m 3 "http://127.0.0.1:$JARVIS_APP_PORT/health" 2>/dev/null)" "000"
  "$BIN/jarvisctl" up >/dev/null 2>&1
  eq "after up, every record is still there" "$(snapshot)" "$before"
  t "smoke passes after a restart" "$BIN/smoke.sh" --no-write

  section "A14. snapshot taken for the reboot"
  mk "written just before the reboot" "topic-reboot" >/dev/null
  snapshot > "$R/snap-reboot.txt"; live_anchors > "$R/anchors-reboot.txt"
  docker exec jarvis-app sh -c 'sha256sum /data/amul-field.jsonl' > "$R/amul-reboot.txt"
  uptime -s > "$R/boot-time-before.txt"
  eq "snapshot holds the records" "$(wc -l < "$R/snap-reboot.txt")" "$(api GET '/api/jarvis/memory?limit=200' | jget "len(d['memories'])")"
  echo "        records: $(wc -l < "$R/snap-reboot.txt"), anchor lines: $(wc -l < "$R/anchors-reboot.txt"); distro boot before: $(cat "$R/boot-time-before.txt")"
  finish
}

# =================================================================================================================
phase_b() {
  cd "$SRC/deploy/mint" || exit 2
  KEY="$(cat "$SEC/api-key")"
  section "B1. after the reboot: everything must have come back BY ITSELF (nothing started by hand)"
  neq() { if [ "$2" != "$3" ]; then ok "$1"; else bad "$1 (both '$2')"; fi; }
  neq "the distro really rebooted (boot time changed)" "$(uptime -s)" "$(cat "$R/boot-time-before.txt")"
  t "docker.service started at boot" systemctl is-active docker
  t "the database came back and is healthy" wait_for '[ "$(docker inspect -f "{{.State.Health.Status}}" jarvis-db 2>/dev/null)" = healthy ]' 240
  t "the app came back and is ready" wait_for '[ "$(ready_code)" = 200 ]' 240
  boot_epoch="$(date -d "$(uptime -s)" +%s)"
  started="$(date -d "$(docker inspect -f '{{.State.StartedAt}}' jarvis-db)" +%s)"
  [ "$started" -ge "$boot_epoch" ] && ok "the database container was started by docker after boot (restart policy), not by hand" || bad "the database container predates the boot"
  has "postgres performed crash recovery after the hard stop" "recovery" <(docker logs jarvis-db 2>&1)
  for tm in backup offsite drill watchdog; do t "timer jarvis-$tm.timer is active again (linger)" wait_for "systemctl --user is-active jarvis-$tm.timer" 120; done
  eq "every record, version and content hash survived the hard stop" "$(snapshot)" "$(cat "$R/snap-reboot.txt")"
  eq "chain-head anchors are identical" "$(live_anchors)" "$(cat "$R/anchors-reboot.txt")"
  eq "the AMUL field file is byte-identical" "$(docker exec jarvis-app sh -c 'sha256sum /data/amul-field.jsonl')" "$(cat "$R/amul-reboot.txt")"
  t "history verifies" "$BIN/jarvisctl" verify operator
  t "smoke passes after the reboot" "$BIN/smoke.sh" --no-write
  docker start "$PC" >/dev/null 2>&1; sleep 3
  sleep 1; "$BIN/backup.sh" > "$R/bb1.out" 2>&1; eq "a backup works after the reboot" "$?" "0"
  "$BIN/offsite.sh" > "$R/ob1.out" 2>&1; eq "the offsite copy works after the reboot" "$?" "0"
  "$BIN/drill.sh" > "$R/db1.out" 2>&1; eq "a restore drill works after the reboot" "$?" "0"
  "$BIN/watchdog.sh" > "$R/wb1.out" 2>&1; eq "watchdog is quiet after the reboot" "$?" "0"

  section "B2. credential rotation"
  old_app_url="$(sed -n 's/^JARVIS_DATABASE_URL=//p' "$SEC/app.env")"; old_key="$KEY"
  old_app_pw="$(echo "$old_app_url" | sed 's#.*://[^:]*:\([^@]*\)@.*#\1#')"
  "$BIN/rotate-password.sh" app > "$R/r1.out" 2>&1; eq "rotate app succeeds" "$?" "0"
  tn "rotation printed no secret" grep -qF "$old_app_pw" "$R/r1.out"
  tn "the OLD app password no longer works" docker exec -e OLD="$old_app_url" jarvis-app python -c "import os, psycopg; psycopg.connect(os.environ['OLD'], connect_timeout=5)"
  eq "the app reconnected with the new password" "$(ready_code)" "200"
  "$BIN/rotate-password.sh" migrator > "$R/r2.out" 2>&1; eq "rotate migrator succeeds" "$?" "0"
  t "verify (which uses the migrator) works with the new password" "$BIN/jarvisctl" verify operator
  "$BIN/rotate-password.sh" api-key > "$R/r3.out" 2>&1; eq "rotate api-key succeeds" "$?" "0"
  KEY="$(cat "$SEC/api-key")"
  eq "the OLD api key is refused" "$(code -H "X-API-Key: $old_key" "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory")" "401"
  eq "the NEW api key works" "$(code -H "X-API-Key: $KEY" "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory")" "200"
  eq "env files are still mode 600 after rotation" "$(stat -c %a "$SEC/db.env" "$SEC/app.env" "$SEC/migrate.env" "$SEC/api-key" | sort -u)" "600"

  section "B3. DESTROY and RESTORE"
  mk "written just before the disaster" "topic-final" >/dev/null
  sleep 1
  "$BIN/backup.sh" > "$R/bf.out" 2>&1; eq "final backup before the disaster" "$?" "0"
  sf="$(newest_set)"
  snap_before="$(snapshot)"; anchors_before="$(live_anchors)"
  amul_before="$(docker exec jarvis-app sh -c 'sha256sum /data/amul-field.jsonl')"
  files_before="$(docker exec jarvis-app sh -c 'ls /data | sort | tr "\n" " "')"
  echo "        before: $(echo "$snap_before" | wc -l) records, $(echo "$anchors_before" | wc -l) anchor lines, files: $files_before"

  tn "restore refuses to run without --yes-destroy-current-data" "$BIN/restore.sh"
  rm -rf "$R/badbk"; mkdir -p "$R/badbk"; cp "$BK/$sf".* "$R/badbk/"
  printf 'X' | dd of="$R/badbk/$sf.dump" bs=1 seek=100 conv=notrunc 2>/dev/null
  JARVIS_BACKUP_DIR="$R/badbk" "$BIN/restore.sh" --yes-destroy-current-data > "$R/rbad.out" 2>&1; rc=$?
  eq "restore rejects a damaged backup set" "$([ $rc -ne 0 ] && echo rejected || echo accepted)" "rejected"
  has "...with a checksum message" "checksum mismatch" "$R/rbad.out"
  eq "...and destroyed NOTHING (records intact, app still ready)" "$(snapshot | wc -l)/$(ready_code)" "$(echo "$snap_before" | wc -l)/200"

  rm -rf "$R/badbk2"; mkdir -p "$R/badbk2"; cp "$BK/$sf".* "$R/badbk2/"
  sed -i '0,/^head|/s/^\(head|[^|]*|[^|]*|\)[0-9]*|/\199999|/' "$R/badbk2/$sf.anchors"
  ( cd "$R/badbk2" && sha256sum "$sf.dump" "$sf.globals.sql" "$sf.data.tar" "$sf.counts" "$sf.anchors" > "$sf.sha256" )
  JARVIS_BACKUP_DIR="$R/badbk2" "$BIN/restore.sh" --yes-destroy-current-data > "$R/rbad2.out" 2>&1; rc=$?
  eq "restore stops at the anchors gate for a doctored set" "$([ $rc -ne 0 ] && echo stopped || echo went-through)" "stopped"
  has "...naming the anchors" "anchors after restore differ" "$R/rbad2.out"
  app_state="$(docker inspect -f '{{.State.Running}}' jarvis-app 2>/dev/null || echo gone)"
  [ "$app_state" != "true" ] && ok "...and the app was NOT started (state: $app_state)" || bad "the app was started despite the failed gate"
  ls "$R"/badbk2/pre-restore-*.dump >/dev/null 2>&1 && ok "a safety dump of the pre-restore state was taken first" || bad "no pre-restore safety dump"

  "$BIN/jarvisctl" down >/dev/null 2>&1
  docker compose -f "$SRC/deploy/mint/docker-compose.yml" down -v >/dev/null 2>&1
  docker volume rm -f jarvis-ledger_pgdata jarvis-ledger_appdata >/dev/null 2>&1
  eq "all ledger volumes are gone" "$(docker volume ls --format '{{.Name}}' | grep -c '^jarvis-ledger_')" "0"
  eq "the service is gone" "$(code -m 3 "http://127.0.0.1:$JARVIS_APP_PORT/health" 2>/dev/null)" "000"

  "$BIN/restore.sh" --yes-destroy-current-data > "$R/restore.out" 2>&1; rc=$?
  eq "restore from the newest set succeeds" "$rc" "0"
  [ "$rc" -ne 0 ] && tail -15 "$R/restore.out"
  has "restore reports the gates passed" "gates passed" "$R/restore.out"
  eq "/ready is 200 after the restore" "$(ready_code)" "200"
  eq "every record, version and content hash is identical" "$(snapshot)" "$snap_before"
  eq "chain-head anchors are identical" "$(live_anchors)" "$anchors_before"
  eq "the AMUL field file is byte-identical" "$(docker exec jarvis-app sh -c 'sha256sum /data/amul-field.jsonl')" "$amul_before"
  eq "the other appdata files came back" "$(docker exec jarvis-app sh -c 'ls /data | sort | tr "\n" " "')" "$files_before"
  eq "history verifies after the restore" "$(api GET /api/jarvis/memory/history/verify | jget "d['ok']")" "True"
  eq "the same API key still works" "$(code -H "X-API-Key: $KEY" "http://127.0.0.1:$JARVIS_APP_PORT/api/jarvis/memory")" "200"
  rid="$(mk "written after the restore" "topic-after" | jget "d['memory']['id']")"
  eq "a new write works after the restore" "$(api GET "/api/jarvis/memory/$rid/history" | jget "[e['op'] for e in d['history']]")" "['create']"
  t "hardening survived the restore (smoke passes)" "$BIN/smoke.sh"
  sleep 1
  "$BIN/backup.sh" > "$R/bafter.out" 2>&1; eq "the first backup after the restore is accepted as a legitimate successor" "$?" "0"

  section "B4. final state"
  "$BIN/watchdog.sh" > "$R/wfinal.out" 2>&1; eq "watchdog is quiet" "$?" "0"
  tn "no 'read-only file system' errors in the app log" bash -c "docker logs jarvis-app 2>&1 | grep -qi 'read-only file system'"
  tn "the app log contains no secret" bash -c "docker logs jarvis-app 2>&1 | grep -qF \"\$(cat $SEC/api-key)\""
  tn "the database log contains no role password" bash -c "docker logs jarvis-db 2>&1 | grep -qF \"\$(sed -n 's/^JARVIS_APP_PASSWORD=//p' $SEC/db.env)\""
  tn "alerts.log contains no secret" bash -c "grep -qF \"\$(cat $SEC/api-key)\" $JARVIS_HOME/logs/alerts.log"
  finish
}

# =================================================================================================================
teardown() {
  cd "$SRC/deploy/mint" 2>/dev/null || true
  systemctl --user disable --now jarvis-backup.timer jarvis-offsite.timer jarvis-drill.timer jarvis-watchdog.timer >/dev/null 2>&1
  systemctl --user stop jarvis-backup.service jarvis-offsite.service jarvis-drill.service jarvis-watchdog.service >/dev/null 2>&1
  rm -f "$HOME"/.config/systemd/user/jarvis-*.service "$HOME"/.config/systemd/user/jarvis-*.timer
  systemctl --user daemon-reload >/dev/null 2>&1
  docker compose -f "$SRC/deploy/mint/docker-compose.yml" down -v >/dev/null 2>&1
  docker rm -f "$PC" jarvis-db jarvis-app jarvis-migrate jarvis-drill-db >/dev/null 2>&1
  docker network rm jarvis-ledger_ledger jarvis-drill-net >/dev/null 2>&1
  docker volume rm -f jarvis-ledger_pgdata jarvis-ledger_appdata >/dev/null 2>&1
  docker rmi -f "$PC" jarvis-ledger-app:local jarvis-ledger-db:16 >/dev/null 2>&1
  rm -rf "$R"
  echo "rehearsal environment removed"
}

case "$PHASE" in
  phase-a)  phase_a ;;
  phase-b)  phase_b ;;
  teardown) teardown ;;
  *) echo "usage: scenario.sh phase-a|phase-b|teardown" >&2; exit 2 ;;
esac
