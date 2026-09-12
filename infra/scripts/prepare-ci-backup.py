#!/usr/bin/env python3
"""Prepare pinned dependencies and private inputs before an existing-backup check."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import runpy
import tarfile
import subprocess

HERE = Path(__file__).resolve().parent
PINS = HERE.parent / 'dependencies/age-v1.3.2.json'
DOWNLOAD_SECONDS = 75


class PreparationError(RuntimeError):
    """Fixed public code; never include private inputs or remote response text."""


def private_write(path, content, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def inputs(environment):
    access = runpy.run_path(str(HERE / 'ci-host-access.py'))
    identity = access['validate_identity'](environment['INSTANCE'], environment['INSTANCE_ARN'],
                                           environment['EXPECTED_ACCOUNT'])
    account = identity['account_id']
    if (environment['DEPLOY_ROLE'] != 'arn:aws:iam::' + account + ':role/zavliq-production-github' or
            environment['BUCKET'] != 'zavliq-production-backups-' + account):
        raise PreparationError('BACKUP_CHECK_DESTINATION_MISMATCH')
    manifest = environment['EXPECTED_BACKUP_MANIFEST'].encode('utf-8')
    if not 1 <= len(manifest) <= 4096:
        raise PreparationError('BACKUP_CHECK_MANIFEST_REQUIRED')
    fetch = runpy.run_path(str(HERE / 'fetch-backup.py'))
    fetch['validate_manifest'](json.loads(manifest))
    recipient = environment['JOURNAL_RECIPIENT'].encode('ascii')
    digest = environment['JOURNAL_RECIPIENT_SHA256']
    if (not 1 <= len(recipient) <= 128 or
            re.fullmatch(rb'age1[0-9a-z]{58}', recipient.strip()) is None or
            re.fullmatch(r'[0-9a-f]{64}', digest) is None or
            hashlib.sha256(recipient).hexdigest() != digest):
        raise PreparationError('BACKUP_CHECK_RECIPIENT_PIN_MISMATCH')
    return manifest, recipient, digest


def download_archive(destination, pin):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        try:
            # Ubuntu 24.04 supplies curl with streamed --max-filesize support.
            # A separate process deadline also bounds DNS, redirects and a slow body.
            # --disable must be first so a local curlrc cannot alter the request.
            subprocess.run(['curl', '--disable', '--proto', '=https', '--proto-redir', '=https',
                            '--tlsv1.2', '--location', '--max-redirs', '5', '--fail', '--silent', '--show-error',
                            '--connect-timeout', '10', '--max-time', str(DOWNLOAD_SECONDS),
                            '--max-filesize', str(pin['archive_bytes']), '--output', '-',
                            pin['download_url']], stdin=subprocess.DEVNULL, stdout=stream,
                           stderr=subprocess.PIPE, check=True, timeout=DOWNLOAD_SECONDS + 5,
                           env={'PATH': os.defpath, 'LANG': 'C'})
        except subprocess.TimeoutExpired:
            raise PreparationError('BACKUP_CHECK_DEPENDENCY_DOWNLOAD_TIMEOUT') from None
        except subprocess.CalledProcessError:
            raise PreparationError('BACKUP_CHECK_DEPENDENCY_DOWNLOAD_FAILED') from None
        stream.flush()
        os.fsync(stream.fileno())
    seal = runpy.run_path(str(HERE / 'seal-ci-journal.py'))
    content, _ = seal['private_bytes'](destination, pin['archive_bytes'], 'BACKUP_CHECK_DEPENDENCY_ARCHIVE_MISMATCH')
    if len(content) != pin['archive_bytes'] or hashlib.sha256(content).hexdigest() != pin['archive_sha256']:
        raise PreparationError('BACKUP_CHECK_DEPENDENCY_ARCHIVE_MISMATCH')


def install_age(archive, destination, pin):
    # Recheck local bytes before interpreting the tar; do not trust a download flag.
    seal = runpy.run_path(str(HERE / 'seal-ci-journal.py'))
    content, _ = seal['private_bytes'](archive, pin['archive_bytes'], 'BACKUP_CHECK_DEPENDENCY_ARCHIVE_MISMATCH')
    if len(content) != pin['archive_bytes'] or hashlib.sha256(content).hexdigest() != pin['archive_sha256']:
        raise PreparationError('BACKUP_CHECK_DEPENDENCY_ARCHIVE_MISMATCH')
    expected = pin['executables']['age/age']
    executable = None
    with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as bundle:
        for index, member in enumerate(bundle):
            if index >= 128:
                raise PreparationError('BACKUP_CHECK_DEPENDENCY_MEMBER_MISMATCH')
            if member.name != 'age/age':
                continue
            if executable is not None or not member.isfile() or member.size != expected['bytes']:
                raise PreparationError('BACKUP_CHECK_DEPENDENCY_MEMBER_MISMATCH')
            with bundle.extractfile(member) as stream:
                executable = stream.read(expected['bytes'] + 1)
    if (executable is None or len(executable) != expected['bytes'] or
            hashlib.sha256(executable).hexdigest() != expected['sha256']):
        raise PreparationError('BACKUP_CHECK_DEPENDENCY_MEMBER_MISMATCH')
    private_write(destination, executable, 0o700)


def main(args, environment=None):
    os.umask(0o077)
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise PreparationError('BACKUP_CHECK_LINUX_AMD64_REQUIRED')
    manifest, recipient, recipient_digest = inputs(os.environ if environment is None else environment)
    pins = json.loads(PINS.read_text())
    pin = pins['platforms']['linux-amd64']
    directory = args.directory.absolute()
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    dependency = directory / 'dependency'
    dependency.mkdir(mode=0o700)
    ciphertext = directory / 'ciphertext'
    ciphertext.mkdir(mode=0o700)
    private_write(directory / 'expected-manifest.json', manifest)
    private_write(directory / 'recipient.txt', recipient)
    archive = dependency / 'age.tar.gz'
    download_archive(archive, pin)
    binary = dependency / 'age'
    install_age(archive, binary, pin)
    seal = runpy.run_path(str(HERE / 'seal-ci-journal.py'))
    check = argparse.Namespace(action='check', directory=ciphertext, age_binary=binary,
                               age_sha256=pin['executables']['age/age']['sha256'], age_version=pins['version'],
                               recipient_file=directory / 'recipient.txt', recipient_sha256=recipient_digest)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        seal['main'](check)
    if json.loads(output.getvalue()) != {'ok': True, 'encryption_ready': True, 'off_host_decryptability_verified': False}:
        raise PreparationError('BACKUP_CHECK_ENCRYPTION_PREFLIGHT_UNCONFIRMED')
    print(json.dumps({'ok': True, 'private_inputs_ready': True, 'encryption_ready': True,
                      'off_host_decryptability_verified': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    try:
        main(parser.parse_args())
    except (Exception, KeyboardInterrupt) as error:
        code = str(error) if isinstance(error, PreparationError) else 'BACKUP_CHECK_PREPARATION_FAILED'
        print(json.dumps({'ok': False, 'code': code,
                          'action': 'Keep access unopened and review private preflight inputs.'}))
        raise SystemExit(1)
