#!/usr/bin/env bash
# Consistent encrypted snapshot. Writer containers are paused during copying.
set -euo pipefail
umask 077
root=$(cd "$(dirname "$0")/../.." && pwd)
compose="$root/infra/scripts/compose.sh"
backup_dir=${ZAVLIQ_BACKUP_DIR:-/var/backups/zavliq}
recipient=${ZAVLIQ_BACKUP_RECIPIENT:?Set the off-host age public recipient}
env_file=${ZAVLIQ_ENV_FILE:-"$root/infra/.${ZAVLIQ_ENVIRONMENT:-local}/compose.env"}
install -d -m 700 "$backup_dir"
exec 9>"$backup_dir/.backup.lock"
flock -n 9 || { printf 'Another backup is running.\n' >&2; exit 1; }
temporary=$(mktemp -d "${TMPDIR:-/tmp}/zavliq-snapshot.XXXXXXXX")
paused=()
started=$(python3 -c 'import time; print(time.monotonic())')
cleanup() {
  if (( ${#paused[@]} )); then docker unpause "${paused[@]}" >/dev/null || true; fi
  rm -rf "$temporary"
}
trap cleanup EXIT
for service in synapse control; do
  id=$(bash "$compose" ps -q "$service")
  [[ -n "$id" ]] || { printf 'Cannot snapshot missing service %s\n' "$service" >&2; exit 1; }
  docker pause "$id" >/dev/null
  paused+=("$id")
done
echo_id=$(bash "$compose" --profile echo ps -q echo)
if [[ -n "$echo_id" && $(docker inspect -f '{{.State.Running}}' "$echo_id") == true ]]; then
  docker pause "$echo_id" >/dev/null
  paused+=("$echo_id")
fi
bash "$compose" exec -T postgres pg_dump -U zavliq -Fc synapse > "$temporary/postgres.dump"
for service in synapse control; do
  id=$(bash "$compose" ps -q "$service")
  docker cp -a "$id:/data" "$temporary/$service"
done
bootstrap=$(bash "$compose" ps --all -q bootstrap)
docker cp -a "$bootstrap:/bootstrap" "$temporary/bootstrap"
project=$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$env_file")
echo_volume=$(docker volume ls -q --filter "label=com.docker.compose.project=$project" --filter 'label=com.docker.compose.volume=echo')
if [[ -n "$echo_volume" ]]; then
  # Use the already-present control image with a read-only tar command. This also
  # captures provisioned identities when the optional responder is stopped.
  mkdir "$temporary/echo"
  bash "$compose" run --rm --no-deps -T --user 0 --entrypoint sh echo-bootstrap -c 'tar -C /echo -cf - .' | tar -C "$temporary/echo" -xf -
fi
mkdir "$temporary/secrets"
for name in postgres_password registration_secret policy_secret control_secret compose.env; do
  cp "$(dirname "$env_file")/$name" "$temporary/secrets/$name"
done
date -u +%FT%TZ > "$temporary/created_at"
docker unpause "${paused[@]}" >/dev/null
paused=()
resumed=$(python3 -c 'import time; print(time.monotonic())')
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="$backup_dir/zavliq-$stamp.tar.age"
tar -C "$temporary" -cf - . | age -r "$recipient" -o "$file.partial"
mv "$file.partial" "$file"
sha256sum "$file" > "$file.sha256"
# Keep a local retry buffer; the S3 lifecycle independently expires remote backups.
find "$backup_dir" -maxdepth 1 -type f -name 'zavliq-*.tar.age*' -mtime +7 -delete
printf '%s\n' "$stamp" > "$backup_dir/last-success"
printf 'Encrypted backup ready: %s\n' "$(basename "$file")"
python3 - "$started" "$resumed" "$file" <<'PY'
import json,sys,time
from pathlib import Path
report={'operation':'backup','ok':True,'writer_pause_seconds':round(float(sys.argv[2])-float(sys.argv[1]),3),'elapsed_seconds':round(time.monotonic()-float(sys.argv[1]),3),'encrypted_bytes':Path(sys.argv[3]).stat().st_size}
Path(sys.argv[3]).parent.joinpath('last-report.json').write_text(json.dumps(report)+'\n')
print(json.dumps(report))
PY
