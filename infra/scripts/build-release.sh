#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
revision=$(git rev-parse HEAD)
[[ -z $(git status --porcelain) ]] || { printf 'Commit or isolate all changes before creating an immutable release.\n' >&2; exit 2; }
destination=${1:-"$root/infra/.artifacts/$revision"}
[[ ! -e "$destination" ]] || { printf 'Release destination already exists.\n' >&2; exit 2; }
mkdir -p "$destination"
for service in synapse control web; do
  docker buildx build --platform linux/amd64 --load -f "infra/docker/$service.Dockerfile" -t "zavliq-$service:$revision" .
done
docker pull --platform linux/amd64 postgres:17.11-alpine
docker save "zavliq-synapse:$revision" "zavliq-control:$revision" "zavliq-web:$revision" postgres:17.11-alpine | gzip > "$destination/images.tar.gz"
git archive HEAD infra | tar -x -C "$destination"
printf '%s\n' "$revision" > "$destination/revision"
python3 - "$destination" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); h=hashlib.sha256()
with (p/'images.tar.gz').open('rb') as stream:
    for chunk in iter(lambda: stream.read(1048576), b''): h.update(chunk)
(p/'manifest.json').write_text(json.dumps({'revision':(p/'revision').read_text().strip(),'images_sha256':h.hexdigest(),'architecture':'linux/amd64'},indent=2)+'\n')
PY
printf 'Immutable release built at %s\n' "$destination"
