#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${ZAVLIQ_ECHO_BOOTSTRAP_ENABLED:-}" != true ]]; then
  printf '%s\n' '{"event":"echo_bootstrap_stopped","code":"OPERATOR_ENABLE_REQUIRED"}' >&2
  exit 1
fi

# This one-shot container alone receives operator secrets. The responder image
# and its normal process receive only the separate private echo volume.
export SYNAPSE_ADMIN_TOKEN="$(cat /bootstrap/admin_token)"
export CONTROL_DATA_KEY="$(cat /run/secrets/control_secret)"
mkdir -p /echo/runtime /echo/journal
chown node:node /echo /echo/runtime /echo/journal
chmod 700 /echo /echo/runtime /echo/journal
export ZAVLIQ_ECHO_LOCKED=true
# fs2 in the native runtime uses this same flock; never seed a live crypto store.
exec gosu node flock --nonblock --conflict-exit-code 73 /echo/runtime/runtime.lock \
  node /app/services/echo/bootstrap.mjs
