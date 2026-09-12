#!/usr/bin/env bash
set -euo pipefail
export SYNAPSE_ADMIN_TOKEN=$(cat /bootstrap/admin_token)
export CONTROL_DATA_KEY=$(cat /run/secrets/control_secret)
chown node:node /data
exec gosu node node /app/services/control/dist/index.js
