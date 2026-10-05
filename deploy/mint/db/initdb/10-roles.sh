#!/bin/bash
# Runs once, on the first start of an empty data volume (docker-entrypoint-initdb.d).
# Creates the two ordinary roles the ledger runs as. Passwords come from the container environment
# (db.env); they are handed to psql through its own backtick variables, never on a command line,
# and statement logging is switched off for this session so no ALTER/CREATE ROLE text reaches the log.
set -Eeuo pipefail

: "${JARVIS_APP_PASSWORD:?JARVIS_APP_PASSWORD is not set (secrets/db.env)}"
: "${JARVIS_MIGRATOR_PASSWORD:?JARVIS_MIGRATOR_PASSWORD is not set (secrets/db.env)}"

psql -X -q -v ON_ERROR_STOP=1 --username postgres --dbname jarvis <<'SQL'
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET log_min_duration_statement = -1;

\set app_pw `printenv JARVIS_APP_PASSWORD`
\set mig_pw `printenv JARVIS_MIGRATOR_PASSWORD`

CREATE ROLE jarvis_app      LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'app_pw';
CREATE ROLE jarvis_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'mig_pw';

-- Nobody connects by default; the migrator may create the ledger schema, the app may only connect.
REVOKE ALL ON DATABASE jarvis FROM PUBLIC;
GRANT CONNECT ON DATABASE jarvis TO jarvis_app;
GRANT CONNECT, CREATE ON DATABASE jarvis TO jarvis_migrator;
SQL

echo "jarvis roles created: jarvis_app (ordinary), jarvis_migrator (DDL)"
