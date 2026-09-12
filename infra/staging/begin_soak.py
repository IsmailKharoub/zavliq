#!/usr/bin/env python3
"""Bind a new 24-hour private-backend soak to exact running images."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time


SERVICES = {'synapse', 'control', 'gateway', 'postgres', 'echo'}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def snapshot(project, services, *, record_observation=None, deadline=None):
    """Keep the existing image mapping; optionally record each bounded inspection.

    Brackets describe when the client inspected a container, not an atomic
    multi-container snapshot or the precise time of a Docker state transition.
    The callback runs after each attempt, including a failed inspection.
    """
    names = list(services)
    if (not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', project)
            or not 0 < len(names) <= 5 or len(set(names)) != len(names)
            or not set(names) <= SERVICES):
        raise ValueError('INVALID_SERVICE_SELECTION')
    deadline = min(deadline, time.monotonic() + 25) if deadline is not None else time.monotonic() + 25
    result = {}
    failed = False
    for name in names:
        started = time.monotonic()
        observation = {'service': name, 'started_at': timestamp(), 'ok': False}
        try:
            remaining = deadline - started
            if remaining <= 0:
                raise TimeoutError()
            output = subprocess.check_output(
                ['docker', 'inspect', '--format', '{{.Id}}|{{.Image}}|{{.Config.Image}}|{{.State.Running}}|{{.State.Paused}}|{{with index .State "Health"}}{{.Status}}{{end}}', project + '-' + name + '-1'],
                text=True, stderr=subprocess.DEVNULL, timeout=min(5, remaining))
            if len(output) > 1024:
                raise ValueError()
            value = output.strip().split('|')
            if (len(value) != 6 or not re.fullmatch(r'[0-9a-f]{64}', value[0])
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', value[1])
                    or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/@:+-]{0,511}', value[2])
                    or value[3] not in {'true', 'false'} or value[4] not in {'true', 'false'}
                    or value[5] not in {'', 'healthy', 'unhealthy', 'starting'}):
                raise ValueError()
            result[name] = {'image_id': value[1], 'image_ref': value[2], 'running': value[3] == 'true', 'paused': value[4] == 'true', 'health': value[5]}
            observation.update(ok=True, container_id=value[0], state=result[name])
        except (subprocess.TimeoutExpired, TimeoutError):
            observation['error_code'] = 'INSPECTION_TIMEOUT'
            failed = True
        except Exception:
            # Neither Docker output nor exception text belongs in health evidence.
            observation['error_code'] = 'INSPECTION_FAILED'
            failed = True
        observation.update(finished_at=timestamp(), elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        if record_observation is not None:
            record_observation(observation)
    if failed:
        raise ValueError('SERVICE_INSPECTION_INCOMPLETE')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--directory', default='/var/lib/zavliq/staging-soak', type=Path)
    parser.add_argument('--without-echo', action='store_true', help='Diagnostic scope only; never claim the full final service set.')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.revision):
        parser.error('Supply the exact deployed commit SHA.')
    services = ['synapse', 'control', 'gateway', 'postgres'] + ([] if args.without_echo else ['echo'])
    images = snapshot('zavliq-load', services)
    for name, image in images.items():
        if not image['running'] or image['paused'] or image['health'] not in ({'', 'healthy'} if name == 'gateway' else {'healthy'}):
            parser.error('Every selected service must be running and healthy.')
        if name != 'postgres' and not image['image_ref'].endswith(':' + args.revision):
            parser.error('Selected images do not all match the requested source revision.')
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = args.directory / 'marker.json'
    data = {'scope': 'private-backend', 'started_at': int(time.time()), 'duration_seconds': 86400,
            'expected_revision': args.revision, 'expected_images': images,
            'final_service_set': not args.without_echo, 'project': 'zavliq-load'}
    with marker.open('x') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({'operation': 'begin_private_soak', 'started_at': data['started_at'], 'revision': args.revision, 'final_service_set': data['final_service_set']}))


if __name__ == '__main__':
    main()
