#!/usr/bin/env bash
# Bootstrap packages only. No application secrets in cloud-init or Terraform state.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y --no-install-recommends age python3 ca-certificates curl jq unattended-upgrades
# Frozen application pins are OCI index IDs from Docker's containerd image
# store. Configure that store on the fresh host before installing the daemon.
python3 -B - <<'PY_DOCKER_CONFIG'
import json
from pathlib import Path


def configure_daemon(path, state_roots):
    for root in state_roots:
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise ValueError('FRESH_DOCKER_STATE_REQUIRED')
    expected = {'features': {'containerd-snapshotter': True}}
    if path.is_symlink():
        raise ValueError('DOCKER_CONFIG_SYMLINK_REFUSED')
    if path.exists():
        if not path.is_file() or json.loads(path.read_text()) != expected:
            raise ValueError('EXISTING_DOCKER_CONFIG_CONFLICT')
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with path.open('x') as stream:
        stream.write(json.dumps(expected) + '\n')
    path.chmod(0o644)


if __name__ == '__main__':
    configure_daemon(Path('/etc/docker/daemon.json'),
                     [Path('/var/lib/docker'), Path('/var/lib/containerd')])
PY_DOCKER_CONFIG

# Exact noble-updates packages matching the verified staging host. Missing
# versions fail installation; never fall back to a different engine/store.
apt-get install -y --no-install-recommends \
  docker.io=29.1.3-0ubuntu3~24.04.2 \
  docker-compose-v2=2.40.3+ds1-0ubuntu1~24.04.1
systemctl enable --now docker
python3 -B - <<'PY_DOCKER_VERIFY'
import datetime
import json
from pathlib import Path
import subprocess


def validate_docker_host(info, engine, compose):
    if info.get('OSType') != 'linux' or info.get('Architecture') not in {'amd64', 'x86_64'}:
        raise ValueError('NATIVE_LINUX_AMD64_DOCKER_REQUIRED')
    if engine != '29.1.3' or compose.removeprefix('v') != '2.40.3':
        raise ValueError('REVIEWED_DOCKER_VERSIONS_REQUIRED')
    if ['driver-type', 'io.containerd.snapshotter.v1'] not in info.get('DriverStatus', []):
        raise ValueError('CONTAINERD_IMAGE_STORE_REQUIRED')


def output(command):
    return subprocess.check_output(command, text=True, timeout=20).strip()


if __name__ == '__main__':
    info = json.loads(output(['docker', 'info', '--format', '{{json .}}']))
    engine = output(['docker', 'version', '--format', '{{.Server.Version}}'])
    compose = output(['docker', 'compose', 'version', '--short'])
    validate_docker_host(info, engine, compose)
    packages = output(['dpkg-query', '-W', '-f=${db:Status-Abbrev}|${binary:Package}|${Version}\n', 'containerd*'])
    containerd = {parts[1]: parts[2] for line in packages.splitlines()
                  if (parts := line.split('|', 2))[0].startswith('ii')}
    if not containerd:
        raise ValueError('INSTALLED_CONTAINERD_PACKAGE_REQUIRED')
    proof = {'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'engine_version': engine, 'compose_version': compose,
             'architecture': info['Architecture'], 'os': info['OSType'],
             'image_store': 'io.containerd.snapshotter.v1', 'containerd_packages': containerd}
    directory = Path('/var/lib/zavliq')
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    (directory / 'docker-host.json').write_text(json.dumps(proof, indent=2) + '\n')
PY_DOCKER_VERIFY
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
