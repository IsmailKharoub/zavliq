#!/usr/bin/env bash
# Bootstrap packages only. No application secrets in cloud-init or Terraform state.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y --no-install-recommends docker.io docker-compose-v2 age python3 ca-certificates curl jq unattended-upgrades
systemctl enable --now docker
install -d -m 700 /etc/zavliq /var/backups/zavliq
install -d -m 755 /opt/zavliq/releases /var/lib/zavliq
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
fi
cat > /etc/sysctl.d/90-zavliq.conf <<'EOF'
vm.swappiness=10
EOF
sysctl -p /etc/sysctl.d/90-zavliq.conf >/dev/null
# Docker owns network forwarding. Only the Lightsail firewall publishes 80/443
# plus operator SSH; app/database containers have no public host ports.
printf 'Host packages ready. Application initialization is a separate audited deploy.\n'
