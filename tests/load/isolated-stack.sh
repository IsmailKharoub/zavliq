#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
state="$root/tests/load/.local/stack"
case "${1:-}" in
  init)
    python3 "$root/infra/scripts/init-environment.py" --directory "$state" --server-name localhost --public-url http://localhost:19080
    python3 - "$state" <<'PY'
import json, os, sys
from pathlib import Path
os.umask(0o077)
state = Path(sys.argv[1])
path = state / 'compose.env'
text = path.read_text().replace('COMPOSE_PROJECT_NAME=zavliq-local', 'COMPOSE_PROJECT_NAME=zavliq-load').replace('ZAVLIQ_RELEASE=development', 'ZAVLIQ_RELEASE=load-local')
text += 'ZAVLIQ_LOCAL_WEB_PORT=19080\nZAVLIQ_LOCAL_MATRIX_PORT=19008\nZAVLIQ_LOCAL_CONTROL_PORT=19001\n'
path.write_text(text)
(state / 'target.json').write_text(json.dumps({'project': 'zavliq-load', 'origin': 'http://localhost:19080', 'environment': 'local', 'hardware': 'Local Docker; capture host CPU, memory, Docker resource limits and competing workload before interpreting performance.'}, indent=2) + '\n')
PY
    ;;
  up)
    ZAVLIQ_ENV_FILE="$state/compose.env" bash "$root/infra/scripts/compose.sh" -f "$root/tests/load/compose.override.yaml" build
    ZAVLIQ_ENV_FILE="$state/compose.env" bash "$root/infra/scripts/compose.sh" -f "$root/tests/load/compose.override.yaml" up -d --no-build --pull never
    ;;
  stop|status)
    action=stop
    if [[ "$1" == status ]]; then action=ps; fi
    ZAVLIQ_ENV_FILE="$state/compose.env" bash "$root/infra/scripts/compose.sh" -f "$root/tests/load/compose.override.yaml" "$action"
    ;;
  *) printf 'Usage: isolated-stack.sh init|up|stop|status\n' >&2; exit 2 ;;
esac
