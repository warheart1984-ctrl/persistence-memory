#!/usr/bin/env bash
# Run the suite against a THROWAWAY Postgres: the JSON backend, then every Postgres-marked test,
# then the whole HTTP-level suite on the row store (JARVIS_TEST_BACKEND=postgres).
#
# Starts its own container (never touches any existing one), on a free 127.0.0.1 port with a
# generated password, and removes it afterwards.  Needs docker and the dev dependencies.
set -euo pipefail

NAME="jarvis-test-pg-$$"
PASSWORD="$(python - <<'PY'
import secrets; print(secrets.token_hex(12))
PY
)"
PORT="$(python - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
)"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

MSYS_NO_PATHCONV=1 docker run -d --name "$NAME" -e POSTGRES_PASSWORD="$PASSWORD" \
  -p "127.0.0.1:${PORT}:5432" --tmpfs /var/lib/postgresql/data postgres:16 >/dev/null

export JARVIS_TEST_PG_DSN="postgresql://postgres:${PASSWORD}@127.0.0.1:${PORT}/postgres"
for _ in $(seq 1 60); do
  if python -c "import psycopg,os; psycopg.connect(os.environ['JARVIS_TEST_PG_DSN'], connect_timeout=2).close()" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "== JSON backend + Postgres-specific tests =="
python -m pytest -q "$@"
echo "== Whole suite on the Postgres row store =="
JARVIS_TEST_BACKEND=postgres python -m pytest -q "$@"
