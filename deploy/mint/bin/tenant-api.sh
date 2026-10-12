#!/usr/bin/env bash
# The per-tenant API of one tenant, for the host signer: JSON lines on stdin and stdout, served by `python -m app.tenant_jobs serve` in a one-off migrate
# container. The tenant is bound HERE, by the process the operator started on the box; the public API never selects a tenant.
#
#   tenant-api.sh TENANT
set -Eeuo pipefail
export LOG_NAME=tenant-api
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: tenant-api.sh TENANT" >&2; exit 2; }
exec "${COMPOSE[@]}" run --rm --no-deps -T migrate python -m app.tenant_jobs serve --tenant "$1"
