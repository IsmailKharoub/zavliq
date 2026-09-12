#!/usr/bin/env python3
"""Get one ephemeral Lightsail SSH identity; write credentials only to private files."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

p=argparse.ArgumentParser();p.add_argument('--instance',required=True);p.add_argument('--directory',required=True);a=p.parse_args()
os.umask(0o077)
directory=Path(a.directory).resolve();directory.mkdir(parents=True,exist_ok=True,mode=0o700)
result=subprocess.run(['aws','lightsail','get-instance-access-details','--region','us-east-1','--instance-name',a.instance,'--protocol','ssh'],check=True,capture_output=True,text=True)
details=json.loads(result.stdout)['accessDetails']
ip=details['ipAddress'];user=details['username']
if not re.fullmatch(r'[0-9.]+',ip) or not re.fullmatch(r'[a-z_][a-z0-9_-]*',user):raise SystemExit('Unexpected SSH destination shape')
(directory/'identity').write_text(details['privateKey'])
(directory/'identity-cert.pub').write_text(details['certKey'])
known=[]
for key in details['hostKeys']:
    public=key['publicKey'].strip()
    if public.startswith(('ssh-','ecdsa-')):known.append(f'{ip} {public}')
    else:known.append(f"{ip} {key['algorithm']} {public}")
if not known:raise SystemExit('AWS did not provide host keys; refusing unverified SSH')
(directory/'known_hosts').write_text('\n'.join(known)+'\n')
(directory/'config').write_text(f'Host zavliq\n  HostName {ip}\n  User {user}\n  IdentityFile {directory}/identity\n  CertificateFile {directory}/identity-cert.pub\n  UserKnownHostsFile {directory}/known_hosts\n  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n  LogLevel ERROR\n')
print('Ephemeral SSH identity saved privately; credentials withheld.')
