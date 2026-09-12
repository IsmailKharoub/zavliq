#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
mode=${ZAVLIQ_ENVIRONMENT:-local}
case "$mode" in local|production) ;; *) printf 'Unsupported environment\n' >&2; exit 2 ;; esac
environment=${ZAVLIQ_ENV_FILE:-"$root/infra/.$mode/compose.env"}
if [[ ! -f "$environment" ]]; then
  printf 'Missing private environment. Run python3 infra/scripts/init-environment.py first.\n' >&2
  exit 2
fi
exec docker compose --env-file "$environment" -f "$root/infra/compose.yaml" -f "$root/infra/compose.$mode.yaml" "$@"
