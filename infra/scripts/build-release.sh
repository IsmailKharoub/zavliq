#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
revision=$(git rev-parse HEAD)
[[ -z $(git status --porcelain) ]] || { printf 'Commit or isolate all changes before creating an immutable release.\n' >&2; exit 2; }
destination=${1:-"$root/infra/.artifacts/$revision"}
[[ ! -e "$destination" ]] || { printf 'Release destination already exists.\n' >&2; exit 2; }
mkdir -p "$destination"
echo_user=${VITE_ECHO_USER_ID:-}
[[ -z "$echo_user" || "$echo_user" =~ ^@echo:[a-zA-Z0-9.-]+$ ]] || { printf 'VITE_ECHO_USER_ID must be the exact verified @echo:HOST address.\n' >&2; exit 2; }
for service in synapse control web echo; do
  build_args=()
  if [[ "$service" == web ]]; then build_args+=(--build-arg "VITE_ECHO_USER_ID=$echo_user"); fi
  docker buildx build --platform linux/amd64 --load "${build_args[@]}" -f "infra/docker/$service.Dockerfile" -t "zavliq-$service:$revision" .
done
docker pull --platform linux/amd64 postgres:17.11-alpine
docker save "zavliq-synapse:$revision" "zavliq-control:$revision" "zavliq-web:$revision" "zavliq-echo:$revision" postgres:17.11-alpine | gzip > "$destination/images.tar.gz"
git archive HEAD infra | tar -x -C "$destination"
printf '%s\n' "$revision" > "$destination/revision"
python3 - "$destination" "$echo_user" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); h=hashlib.sha256()
with (p/'images.tar.gz').open('rb') as stream:
    for chunk in iter(lambda: stream.read(1048576), b''): h.update(chunk)
(p/'manifest.json').write_text(json.dumps({'revision':(p/'revision').read_text().strip(),'images_sha256':h.hexdigest(),'architecture':'linux/amd64','web_echo_user_id':sys.argv[2] or None},indent=2)+'\n')
PY
printf 'Immutable release built at %s\n' "$destination"
