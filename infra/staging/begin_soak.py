#!/usr/bin/env python3
"""Bind a new 24-hour private-backend soak to exact running images."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time


def snapshot(project, services):
    result = {}
    for name in services:
        value = subprocess.check_output(['docker', 'inspect', '--format', '{{.Image}}|{{.Config.Image}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}', project + '-' + name + '-1'], text=True, timeout=5).strip().split('|')
        result[name] = {'image_id': value[0], 'image_ref': value[1], 'running': value[2] == 'true', 'health': value[3]}
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
        if not image['running'] or image['health'] not in ('', 'healthy'):
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
