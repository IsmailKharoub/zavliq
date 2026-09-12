#!/usr/bin/env bash
# Install only after the bounded forms are privately present; does not start a soak.
set -euo pipefail
[[ $EUID -eq 0 ]] || { printf 'Run as root on private staging.\n' >&2; exit 2; }
[[ -f /etc/zavliq/staging-capability.json && -f /etc/zavliq/operations.env ]] || { printf 'Install the private capability and operations configuration first.\n' >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
install -d -m 755 /opt/zavliq/staging
install -m 644 "$root"/infra/staging/*.py /opt/zavliq/staging/
install -m 644 "$root"/infra/staging/systemd/* /etc/systemd/system/
for unit in zavliq-backup.service zavliq-backup.timer zavliq-retention.service zavliq-retention.timer; do
  install -m 644 "$root/infra/systemd/$unit" "/etc/systemd/system/$unit"
done
install -d -m 755 /etc/systemd/system/zavliq-backup.service.d
cat > /etc/systemd/system/zavliq-backup.service.d/staging-upload.conf <<'EOF'
[Service]
Environment=ZAVLIQ_ENVIRONMENT=staging
ExecStartPost=/usr/bin/timeout 180 /usr/bin/python3 /opt/zavliq/staging/upload_latest.py
TimeoutStartSec=10min
TimeoutStopSec=15s
KillMode=control-group
EOF
install -d -m 755 /etc/systemd/system/zavliq-retention.service.d
cat > /etc/systemd/system/zavliq-retention.service.d/staging-mode.conf <<'EOF'
[Service]
Environment=ZAVLIQ_ENVIRONMENT=staging
EOF
systemctl daemon-reload
systemctl enable --now zavliq-stage-health.timer zavliq-backup.timer zavliq-retention.timer
printf 'Staging monitoring/backup timers installed. No soak marker was created.\n'
