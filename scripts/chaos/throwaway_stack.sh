#!/usr/bin/env bash
# A throwaway clone of the Mint stack for CL_CHAOS_100x: its own compose project, container names, images, volumes, network, port
# and secrets, built from this repository. It never touches the live stack (jarvis-ledger / jarvis-db / jarvis-app, port 8011,
# ~/jarvis-ledger, deploy/mint/secrets).
#
#   throwaway_stack.sh up        copy deploy/mint, rename everything, make secrets and TEST keys, build, start
#                                JARVIS_CHAOS_PGDATA_MB=256 throwaway_stack.sh up   puts the database on a size-capped tmpfs volume (held mounted by a
#                                `chaos100x-keeper` container so the data survives a database restart): the full-disk fault fills THAT, never a real disk
#   throwaway_stack.sh rebuild   rebuild and recreate the running throwaway from this checkout (`jarvisctl up` in its copy). REFUSES unless the stack's own
#                                /ready answers and says chaos-throwaway:...; a missing /ready, or jarvis-live, is a refusal. Nothing else may run `jarvisctl up`.
#   throwaway_stack.sh down      stop it, remove its volumes, network, images and its directory
#   throwaway_stack.sh status    what exists
#
# Environment: JARVIS_CHAOS_DIR (default ${TMPDIR:-/tmp}/jarvis-chaos100x), JARVIS_CHAOS_PORT (default 18017; never 8011),
# JARVIS_CHAOS_PGDATA_MB (optional; 64..2048).
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHAOS_DIR="${JARVIS_CHAOS_DIR:-${TMPDIR:-/tmp}/jarvis-chaos100x}"
PORT="${JARVIS_CHAOS_PORT:-18017}"
PROJECT=jarvis-chaos100x
DB_C=chaos100x-db APP_C=chaos100x-app MIG_C=chaos100x-migrate
DB_IMG=chaos100x-db:test APP_IMG=chaos100x-app:test
STACK_ID="chaos-throwaway:$PROJECT"
PGDATA_MB="${JARVIS_CHAOS_PGDATA_MB:-}"
KEEPER_C=chaos100x-keeper
MARKER=".jarvis-chaos100x"
MINT="$CHAOS_DIR/mint"

die() { echo "throwaway_stack: $*" >&2; exit 1; }

live_guard() {
  [ "$PORT" != 8011 ] || die "port 8011 is the live stack's port"
  case "$PORT" in ''|*[!0-9]*) die "bad port $PORT" ;; esac
  # the path is normalised WITHOUT needing it (or its parent) to exist: a guard that depends on the live directory being there is no guard
  local dir; dir="$(python3 -c 'import os,sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' "$CHAOS_DIR")"
  local home_ledger; home_ledger="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$HOME/jarvis-ledger")"
  case "$dir" in
    "$REPO"|"$REPO"/*) die "refusing a directory inside the repository: $CHAOS_DIR" ;;
    "$home_ledger"|"$home_ledger"/*) die "refusing a directory inside the live ledger home: $CHAOS_DIR" ;;
  esac
}

compose() { docker compose -p "$PROJECT" -f "$MINT/docker-compose.yml" "$@"; }

up() {
  live_guard
  [ ! -e "$CHAOS_DIR" ] || die "$CHAOS_DIR already exists; run: $0 down"
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then die "port $PORT is already in use"; fi
  command -v ssh-keygen >/dev/null || die "ssh-keygen is needed for the test keys"
  if [ -n "$PGDATA_MB" ]; then
    case "$PGDATA_MB" in *[!0-9]*) die "JARVIS_CHAOS_PGDATA_MB must be a number of megabytes" ;; esac
    [ "$PGDATA_MB" -ge 64 ] && [ "$PGDATA_MB" -le 2048 ] || die "JARVIS_CHAOS_PGDATA_MB must be between 64 and 2048"
  fi
  mkdir -p "$CHAOS_DIR" && chmod 700 "$CHAOS_DIR" && : > "$CHAOS_DIR/$MARKER"
  mkdir -p "$MINT" "$CHAOS_DIR/home"
  # a copy of the deploy directory, without the live secrets, local env or state
  (cd "$REPO/deploy/mint" && tar --exclude=./secrets --exclude=./.env --exclude=./.env.example --exclude=./rehearse -cf - .) | tar -xf - -C "$MINT"
  # the rehearsal scripts name the live containers and kill them on purpose: they are never copied
  python3 - "$MINT" "$REPO" "$PORT" "$PROJECT" "$DB_C" "$APP_C" "$MIG_C" "$DB_IMG" "$APP_IMG" "$PGDATA_MB" "$KEEPER_C" <<'PY' || { rm -rf "$CHAOS_DIR"; die "the copy still names the live stack; nothing was created"; }
import re, sys
from pathlib import Path
mint, repo, port, project, db_c, app_c, mig_c, db_img, app_img, pgdata_mb, keeper_c = sys.argv[1:]
compose = Path(mint, "docker-compose.yml")
s = compose.read_text()
for old, new in (("name: jarvis-ledger\n", f"name: {project}\n"), ("container_name: jarvis-db", f"container_name: {db_c}"),
                 ("container_name: jarvis-migrate", f"container_name: {mig_c}"), ("container_name: jarvis-app", f"container_name: {app_c}"),
                 ("image: jarvis-ledger-db:16", f"image: {db_img}"), ("image: jarvis-ledger-app:local", f"image: {app_img}"),
                 ("context: ../..", f"context: {repo}"), ("${JARVIS_APP_PORT:-8011}", "${JARVIS_APP_PORT:-" + port + "}"),
                 ("${JARVIS_STACK_ID:-jarvis-live}", "${JARVIS_STACK_ID:-chaos-throwaway:" + project + "}")):
    assert old in s, f"compose: {old!r} not found"
    s = s.replace(old, new)
if pgdata_mb:
    # the database volume becomes a size-capped tmpfs (RAM, not the host disk).  A tmpfs volume vanishes when its last container stops, so a
    # keeper container mounts it too and the data survives a restart of the database.
    keeper = (f"  keeper:\n    <<: *common\n    image: {db_img}\n    container_name: {keeper_c}\n    entrypoint: [\"sleep\", \"infinity\"]\n"
              "    user: root\n    volumes:\n      - pgdata:/keep\n    networks: [ledger]\n    depends_on:\n      db: { condition: service_healthy }\n\n")
    assert "networks:\n  ledger: {}\n" in s and "\n  pgdata: {}\n" in s, "compose layout changed"
    s = s.replace("networks:\n  ledger: {}\n", keeper + "networks:\n  ledger: {}\n", 1)
    s = s.replace("\n  pgdata: {}\n", f"\n  pgdata:\n    driver: local\n    driver_opts:\n      type: tmpfs\n      device: tmpfs\n      o: size={pgdata_mb}m,uid=999,gid=999,mode=0700\n", 1)
compose.write_text(s)
lib = Path(mint, "bin", "lib.sh")
t = lib.read_text()
for old, new in (("PROJECT=jarvis-ledger\n", f"PROJECT={project}\n"), ("DB_CONTAINER=jarvis-db\n", f"DB_CONTAINER={db_c}\n"),
                 ("APP_CONTAINER=jarvis-app\n", f"APP_CONTAINER={app_c}\n"), ("APP_IMAGE=jarvis-ledger-app:local\n", f"APP_IMAGE={app_img}\n"),
                 ("DB_IMAGE=jarvis-ledger-db:16\n", f"DB_IMAGE={db_img}\n"), ('APP_PORT="${JARVIS_APP_PORT:-8011}"', 'APP_PORT="${JARVIS_APP_PORT:-' + port + '}"')):
    assert old in t, f"lib.sh: {old!r} not found"
    t = t.replace(old, new)
lib.write_text(t)
# nothing in the copy may still name the live stack
left = []
for p in list(Path(mint).rglob("*")):
    if p.is_file() and p.stat().st_size < 400_000:
        try:
            txt = p.read_text()
        except UnicodeDecodeError:
            continue
        for needle in ("jarvis-db", "jarvis-app", "jarvis-migrate", "jarvis-ledger-app", "jarvis-ledger-db", "8011"):
            for i, line in enumerate(txt.splitlines(), 1):
                if needle in line and not line.lstrip().startswith(("#", "//")):
                    left.append(f"{p.relative_to(mint)}:{i}: {line.strip()[:90]}")
allowed = ("README", "docs")
left = [x for x in left if not x.startswith(allowed)]
if left:
    print("live names left in the copy:\n  " + "\n  ".join(left[:20]), file=sys.stderr)
    sys.exit(1)
PY
  printf 'JARVIS_APP_PORT=%s\nJARVIS_HOME=%s\nJARVIS_STACK_ID=%s\n' "$PORT" "$CHAOS_DIR/home" "$STACK_ID" > "$MINT/.env"
  # test keys (never the live custody directory): a root, a signing key and a stranger
  mkdir -p "$CHAOS_DIR/keys" && chmod 700 "$CHAOS_DIR/keys"
  for k in root mint stranger; do ssh-keygen -q -t ed25519 -N "" -C "chaos100x-$k" -f "$CHAOS_DIR/keys/$k"; done
  grep -q '^JARVIS_STACK_ID=chaos-throwaway:' "$MINT/.env" || die "the copy's .env does not carry a throwaway stack identity; refusing to run jarvisctl up"
  "$MINT/bin/jarvisctl" secrets >/dev/null
  mkdir -p "$MINT/secrets"
  { echo "# TEST root for the CL_CHAOS_100x throwaway stack only"; cat "$CHAOS_DIR/keys/root.pub"; } > "$MINT/secrets/trust-roots.pub"
  chmod 644 "$MINT/secrets/trust-roots.pub"
  "$MINT/bin/jarvisctl" up
  python3 - "$CHAOS_DIR" "$PORT" "$PROJECT" "$STACK_ID" "$DB_C" "$APP_C" "$MIG_C" "$DB_IMG" "$APP_IMG" "$PGDATA_MB" "$KEEPER_C" <<'PY'
import json, sys
d, port, project, sid, db_c, app_c, mig_c, db_img, app_img, pgdata_mb, keeper_c = sys.argv[1:]
containers = {"db": db_c, "app": app_c, "migrate": mig_c}
if pgdata_mb:
    containers["keeper"] = keeper_c
json.dump({"dir": d, "url": f"http://127.0.0.1:{port}", "port": int(port), "project": project, "stack_id": sid,
           "pgdata_mb": int(pgdata_mb) if pgdata_mb else None,
           "containers": containers, "images": [db_img, app_img],
           "compose_file": f"{d}/mint/docker-compose.yml", "secrets_dir": f"{d}/mint/secrets", "keys_dir": f"{d}/keys",
           "network": f"{project}_ledger", "volumes": [f"{project}_pgdata", f"{project}_appdata"]},
          open(f"{d}/stack.json", "w"), indent=2)
PY
  chmod 600 "$CHAOS_DIR/stack.json"
  echo "throwaway stack is up: http://127.0.0.1:$PORT (project $PROJECT, directory $CHAOS_DIR)"
}

rebuild() {
  live_guard
  [ -f "$CHAOS_DIR/$MARKER" ] || die "$CHAOS_DIR is not a throwaway stack directory (no $MARKER)"
  grep -q "^name: $PROJECT\$" "$MINT/docker-compose.yml" 2>/dev/null || die "$MINT/docker-compose.yml is not the throwaway project; not touching it"
  local port ready
  port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$CHAOS_DIR/stack.json" 2>/dev/null)" || die "no readable $CHAOS_DIR/stack.json"
  [ "$port" != 8011 ] || die "the stack's port is 8011, the live stack's"
  ready="$(curl -s -m 5 "http://127.0.0.1:$port/ready" 2>/dev/null || true)"
  python3 -c 'import json,sys; sys.exit(0 if str(json.loads(sys.argv[1]).get("stack","")).startswith("chaos-throwaway:") else 1)' "${ready:-null}" 2>/dev/null \
    || die "refusing to run jarvisctl up: http://127.0.0.1:$port/ready is missing, or does not report a chaos-throwaway: identity (jarvis-live and unlabelled stacks are refused)"
  JARVIS_HOME="$CHAOS_DIR/home" "$MINT/bin/jarvisctl" up
}

down() {
  live_guard
  if [ -f "$MINT/docker-compose.yml" ]; then
    grep -q "^name: $PROJECT\$" "$MINT/docker-compose.yml" || die "$MINT/docker-compose.yml is not the throwaway project; not touching it"
    compose down -v --remove-orphans 2>&1 | tail -3 || true
  fi
  docker rm -f "$DB_C" "$APP_C" "$MIG_C" "$KEEPER_C" >/dev/null 2>&1 || true
  docker volume rm "${PROJECT}_pgdata" "${PROJECT}_appdata" >/dev/null 2>&1 || true
  docker network rm "${PROJECT}_ledger" >/dev/null 2>&1 || true
  docker image rm "$DB_IMG" "$APP_IMG" >/dev/null 2>&1 || true
  if [ -d "$CHAOS_DIR" ]; then
    [ -f "$CHAOS_DIR/$MARKER" ] || die "$CHAOS_DIR is not a throwaway stack directory (no $MARKER); not deleting it"
    rm -rf "$CHAOS_DIR"
  fi
  echo "throwaway stack removed"
}

status() {
  echo "directory: $CHAOS_DIR $([ -d "$CHAOS_DIR" ] && echo present || echo absent)"
  docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}} {{.Status}}'
  docker volume ls -q | grep "^${PROJECT}_" || true
}

case "${1:-}" in
  up) up ;; rebuild) rebuild ;; down) down ;; status) status ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
