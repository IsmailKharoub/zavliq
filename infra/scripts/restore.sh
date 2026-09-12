#!/usr/bin/env bash
# Restore into a fresh isolated deployment. Requires an explicitly acknowledged target.
set -euo pipefail
umask 077
[[ $# -eq 2 ]] || { printf 'Usage: restore.sh BACKUP.tar.age AGE_PRIVATE_KEY_FILE\n' >&2; exit 2; }
[[ ${ZAVLIQ_RESTORE_FRESH_ENVIRONMENT:-} == yes ]] || { printf 'Set ZAVLIQ_RESTORE_FRESH_ENVIRONMENT=yes only for the isolated recovery target.\n' >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
started=$(python3 -c 'import time; print(time.monotonic())')
compose="$root/infra/scripts/compose.sh"
env_file=${ZAVLIQ_ENV_FILE:-"$root/infra/.${ZAVLIQ_ENVIRONMENT:-local}/compose.env"}
[[ -f "$env_file" ]] || { printf 'Initialize the fresh target environment first.\n' >&2; exit 2; }
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
age -d -i "$2" -o "$temporary/backup.tar" "$1"
python3 - "$temporary/backup.tar" "$temporary" <<'PY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    for member in archive.getmembers():
        p = pathlib.PurePosixPath(member.name)
        if p.is_absolute() or '..' in p.parts or member.issym() or member.islnk() or member.isdev():
            raise SystemExit('Unsafe backup archive member; restore aborted.')
    archive.extractall(sys.argv[2], filter='data')
PY
for required in postgres.dump synapse/homeserver.yaml control/control.sqlite bootstrap/admin_token secrets/compose.env; do
  [[ -f "$temporary/$required" ]] || { printf 'Incomplete archive; missing %s\n' "$required" >&2; exit 1; }
done
if [[ -n $(bash "$compose" ps --all -q) ]]; then
  printf 'Fresh restore requires an environment with no existing containers.\n' >&2
  exit 1
fi
project=$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$env_file")
[[ -n "$project" ]] || { printf 'The target must have an explicit isolated Compose project name.\n' >&2; exit 1; }
if [[ -n $(docker volume ls -q --filter "label=com.docker.compose.project=$project") ]]; then
  printf 'Fresh restore requires no pre-existing project volumes; select a new project name.\n' >&2
  exit 1
fi
# Preserve target path/project/origin while restoring credential material and server identity.
python3 - "$env_file" "$temporary/secrets/compose.env" <<'PY'
import sys
from pathlib import Path
def load(p): return dict(line.split('=',1) for line in Path(p).read_text().splitlines() if '=' in line and not line.startswith('#'))
target, source = load(sys.argv[1]), load(sys.argv[2])
if target['ZAVLIQ_SERVER_NAME'] != source['ZAVLIQ_SERVER_NAME']:
    raise SystemExit('Matrix server-name is immutable; initialize the recovery environment with the original name.')
target['ZAVLIQ_RELEASE'] = source['ZAVLIQ_RELEASE']
path=Path(sys.argv[1]);path.write_text(''.join(f'{k}={v}\n' for k,v in target.items()));path.chmod(0o600)
PY
for name in postgres_password registration_secret policy_secret control_secret; do
  install -m 600 "$temporary/secrets/$name" "$(dirname "$env_file")/$name"
done
bash "$compose" up -d --wait --no-build postgres
bash "$compose" exec -T postgres pg_restore -U zavliq --clean --if-exists --exit-on-error --dbname=synapse < "$temporary/postgres.dump"
for service in synapse control; do
  if [[ "$service" == synapse ]]; then owner=991; else owner=1000; fi
  # Safe extraction intentionally drops archive ownership. Restore only the known
  # service owner after writing to this new deployment's managed volume.
  tar -C "$temporary/$service" -cf - . | bash "$compose" run --rm --no-deps -T --user 0 --entrypoint sh "$service" -c "mkdir -p /data && tar -xpf - -C /data && chown -R $owner:$owner /data"
done
tar -C "$temporary/bootstrap" -cf - . | bash "$compose" run --rm --no-deps -T --user 0 --entrypoint sh bootstrap -c 'mkdir -p /bootstrap && tar -xpf - -C /bootstrap'
bash "$compose" up -d --wait --no-build
printf 'Restore services healthy. Verify original identities, standard/E2EE history, attachments and queued delivery before DNS cutover.\n'
python3 - "$started" <<'PY'
import json,sys,time
print(json.dumps({'operation':'restore','services_healthy':True,'elapsed_seconds':round(time.monotonic()-float(sys.argv[1]),3)}))
PY
