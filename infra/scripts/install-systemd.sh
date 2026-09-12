#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { printf 'Run as root on the deployment host.\n' >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
[[ -f /etc/zavliq/operations.env ]] || { printf 'Create /etc/zavliq/operations.env with origin and public age recipient first.\n' >&2; exit 2; }
install -m 644 "$root"/infra/systemd/zavliq-* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zavliq-health.timer zavliq-backup.timer zavliq-retention.timer
