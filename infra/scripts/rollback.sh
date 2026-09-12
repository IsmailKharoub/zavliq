#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { printf 'Run on the deployment host as root.\n' >&2; exit 2; }
[[ ${ZAVLIQ_SCHEMA_ROLLBACK_VERIFIED:-} == yes ]] || { printf 'Verify database compatibility or restore a pre-release backup; then set ZAVLIQ_SCHEMA_ROLLBACK_VERIFIED=yes.\n' >&2; exit 2; }
previous=$(cat /etc/zavliq/previous-release)
[[ "$previous" =~ ^/opt/zavliq/releases/[0-9a-f]{40}$ && -d "$previous" ]] || { printf 'Previous release missing.\n' >&2; exit 1; }
cp /etc/zavliq/previous-compose.env /etc/zavliq/compose.env
ln -sfn "$previous" /opt/zavliq/current.next
mv -Tf /opt/zavliq/current.next /opt/zavliq/current
export ZAVLIQ_ENVIRONMENT=production ZAVLIQ_ENV_FILE=/etc/zavliq/compose.env
bash "$previous/infra/scripts/compose.sh" up -d --no-build --wait --wait-timeout 180
origin=$(sed -n 's/^ZAVLIQ_PUBLIC_URL=//p' /etc/zavliq/compose.env)
python3 "$previous/infra/scripts/healthcheck.py" --origin "$origin"
printf 'Previous release restored and healthy.\n'
