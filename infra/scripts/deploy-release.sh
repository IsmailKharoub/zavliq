#!/usr/bin/env bash
# Execute on the provisioned host after uploading a release directory.
set -euo pipefail
[[ $EUID -eq 0 && $# -eq 1 && $1 =~ ^[0-9a-f]{40}$ ]] || { printf 'Usage (root): deploy-release.sh COMMIT_SHA\n' >&2; exit 2; }
revision=$1
release="/opt/zavliq/releases/$revision"
[[ -f /etc/zavliq/compose.env ]] || { printf 'Initialize /etc/zavliq with production secrets first.\n' >&2; exit 2; }
python3 - "$release" "$revision" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); manifest=json.loads((p/'manifest.json').read_text()); h=hashlib.sha256()
with (p/'images.tar.gz').open('rb') as stream:
    for chunk in iter(lambda: stream.read(1048576),b''):h.update(chunk)
if manifest['revision']!=sys.argv[2] or manifest['images_sha256']!=h.hexdigest():raise SystemExit('Release integrity mismatch')
PY
export ZAVLIQ_ENVIRONMENT=production ZAVLIQ_ENV_FILE=/etc/zavliq/compose.env
if [[ -L /opt/zavliq/current ]]; then
  # A failed backup blocks rollout. No user message bodies or credentials are printed.
  set -a
  source /etc/zavliq/operations.env
  set +a
  bash /opt/zavliq/current/infra/scripts/backup.sh
  readlink -f /opt/zavliq/current > /etc/zavliq/previous-release
fi
gzip -dc "$release/images.tar.gz" | docker load
cp /etc/zavliq/compose.env /etc/zavliq/previous-compose.env
python3 - "$revision" <<'PY'
import sys
from pathlib import Path
p=Path('/etc/zavliq/compose.env')
lines=[line for line in p.read_text().splitlines() if not line.startswith('ZAVLIQ_RELEASE=')]
p.write_text('\n'.join(lines+['ZAVLIQ_RELEASE='+sys.argv[1]])+'\n');p.chmod(0o600)
PY
ln -sfn "$release" /opt/zavliq/current.next
mv -Tf /opt/zavliq/current.next /opt/zavliq/current
if ! bash "$release/infra/scripts/compose.sh" up -d --no-build --wait --wait-timeout 180; then
  printf 'Deployment unhealthy. Previous release retained; use the documented schema-aware rollback procedure.\n' >&2
  exit 1
fi
origin=$(sed -n 's/^ZAVLIQ_PUBLIC_URL=//p' /etc/zavliq/compose.env)
python3 "$release/infra/scripts/healthcheck.py" --origin "$origin"
printf 'Release %s healthy.\n' "$revision"
