#!/usr/bin/env python3
"""Seal an exact private CI cleanup journal; never publish plaintext or credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time

JOURNAL_LIMIT = 4096
BINARY_LIMIT = 32 * 1024 ** 2
OPERATION_SECONDS = 60
CANARY = b'Zavliq CI journal encryption preflight; contains no operator data.\n'


class SealError(RuntimeError):
    """Fixed public failure code, with no input paths or subprocess output."""


def signature(details):
    return (details.st_dev, details.st_ino, details.st_size, details.st_mode,
            details.st_uid, details.st_mtime_ns, details.st_ctime_ns)


def private_bytes(path, limit, code):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                    before.st_mode & 0o077 or not 1 <= before.st_size <= limit):
                raise SealError(code)
            content = stream.read(limit + 1)
            if len(content) != before.st_size or signature(os.fstat(stream.fileno())) != signature(before):
                raise SealError(code)
            return content, signature(before)
    except OSError:
        raise SealError(code) from None


def unchanged(path, expected, code):
    try:
        if signature(os.stat(path, follow_symlinks=False)) != expected:
            raise SealError(code)
    except OSError:
        raise SealError(code) from None


def private_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    details = os.fstat(descriptor)
    if details.st_uid != os.geteuid() or details.st_mode & 0o077:
        os.close(descriptor)
        raise SealError('CI_JOURNAL_PRIVATE_OUTPUT_REQUIRED')
    return descriptor


def directory_unchanged(path, descriptor):
    try:
        current, opened = os.stat(path, follow_symlinks=False), os.fstat(descriptor)
        fields = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
        if fields(current) != fields(opened):
            raise SealError('CI_JOURNAL_OUTPUT_DIRECTORY_CHANGED')
    except OSError:
        raise SealError('CI_JOURNAL_OUTPUT_DIRECTORY_CHANGED') from None


def ciphertext_unchanged(name, directory_fd, stream, expected):
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if signature(named) != signature(os.fstat(stream.fileno())):
            raise SealError('CI_JOURNAL_CIPHERTEXT_CHANGED')
        stream.seek(0)
        if stream.read(JOURNAL_LIMIT + 4097) != expected:
            raise SealError('CI_JOURNAL_CIPHERTEXT_CHANGED')
    except OSError:
        raise SealError('CI_JOURNAL_CIPHERTEXT_CHANGED') from None


def run_age(argv, *, end, data=None, output=subprocess.PIPE):
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise SealError('CI_JOURNAL_ENCRYPTION_TIMEOUT')
    # No cloud credentials, interactive stdin, plugins or inherited process group.
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output,
                               stderr=subprocess.PIPE, start_new_session=True,
                               env={'PATH': os.defpath, 'LANG': 'C'})
    try:
        stdout, stderr = process.communicate(input=data, timeout=remaining)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            raise SealError('CI_JOURNAL_CHILD_CLEANUP_UNCONFIRMED') from None
        raise SealError('CI_JOURNAL_ENCRYPTION_INTERRUPTED') from None
    if process.returncode or stderr:
        raise SealError('CI_JOURNAL_ENCRYPTION_FAILED')
    return stdout


def configuration(args):
    binary, binary_signature = private_bytes(args.age_binary, BINARY_LIMIT, 'CI_JOURNAL_PINNED_AGE_REQUIRED')
    recipient_bytes, recipient_signature = private_bytes(args.recipient_file, 128, 'CI_JOURNAL_RECIPIENT_REQUIRED')
    if (re.fullmatch(r'[0-9a-f]{64}', args.age_sha256) is None or
            hashlib.sha256(binary).hexdigest() != args.age_sha256 or
            not binary_signature[3] & stat.S_IXUSR or
            re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', args.age_version) is None):
        raise SealError('CI_JOURNAL_PINNED_AGE_REQUIRED')
    if (re.fullmatch(r'[0-9a-f]{64}', args.recipient_sha256) is None or
            hashlib.sha256(recipient_bytes).hexdigest() != args.recipient_sha256):
        raise SealError('CI_JOURNAL_RECIPIENT_PIN_MISMATCH')
    try:
        recipient = recipient_bytes.decode('ascii').strip()
    except UnicodeError:
        raise SealError('CI_JOURNAL_RECIPIENT_REQUIRED') from None
    # Only native X25519 recipients. No plugins, identities, passphrases or SSH keys.
    if re.fullmatch(r'age1[0-9a-z]{58}', recipient) is None:
        raise SealError('CI_JOURNAL_RECIPIENT_REQUIRED')
    return recipient, binary_signature, recipient_signature


def main(args, seconds=OPERATION_SECONDS):
    os.umask(0o077)
    end = time.monotonic() + seconds
    source = private_bytes(args.journal, JOURNAL_LIMIT, 'CI_JOURNAL_PRIVATE_SOURCE_REQUIRED') if args.action == 'seal' else None
    recipient, binary_signature, recipient_signature = configuration(args)
    directory = args.output.parent if args.action == 'seal' else args.directory
    if args.action == 'seal' and (args.output.name in {'', '.', '..'} or args.output.suffix != '.age'):
        raise SealError('CI_JOURNAL_CIPHERTEXT_PATH_REQUIRED')
    directory_fd = private_directory(directory)
    temporary = '.journal-' + secrets.token_hex(16) + '.partial'
    owned_inode = None
    try:
        if args.action == 'seal':
            try:
                os.stat(args.output.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SealError('CI_JOURNAL_OUTPUT_EXISTS')
        unchanged(args.age_binary, binary_signature, 'CI_JOURNAL_AGE_CHANGED')
        version = run_age([str(args.age_binary.absolute()), '--version'], end=min(end, time.monotonic() + 5))
        if version != (args.age_version + '\n').encode('ascii'):
            raise SealError('CI_JOURNAL_AGE_VERSION_MISMATCH')
        directory_unchanged(directory, directory_fd)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        created = os.fstat(descriptor)
        owned_inode = (created.st_dev, created.st_ino)
        with os.fdopen(descriptor, 'w+b') as stream:
            unchanged(args.age_binary, binary_signature, 'CI_JOURNAL_AGE_CHANGED')
            unchanged(args.recipient_file, recipient_signature, 'CI_JOURNAL_RECIPIENT_CHANGED')
            run_age([str(args.age_binary.absolute()), '--encrypt', '--recipient', recipient],
                    end=end, data=source[0] if source else CANARY, output=stream)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            ciphertext = stream.read(JOURNAL_LIMIT + 4097)
            if (not ciphertext.startswith(b'age-encryption.org/v1\n') or
                    not 128 <= len(ciphertext) <= JOURNAL_LIMIT + 4096):
                raise SealError('CI_JOURNAL_CIPHERTEXT_UNCONFIRMED')
            unchanged(args.age_binary, binary_signature, 'CI_JOURNAL_AGE_CHANGED')
            unchanged(args.recipient_file, recipient_signature, 'CI_JOURNAL_RECIPIENT_CHANGED')
            directory_unchanged(directory, directory_fd)
            ciphertext_unchanged(temporary, directory_fd, stream, ciphertext)
            if source:
                unchanged(args.journal, source[1], 'CI_JOURNAL_SOURCE_CHANGED')
                # Keep the verified inode open through atomic no-overwrite publication.
                os.link(temporary, args.output.name, src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False)
                os.fsync(directory_fd)
                directory_unchanged(directory, directory_fd)
                ciphertext_unchanged(args.output.name, directory_fd, stream, ciphertext)
                ciphertext_unchanged(temporary, directory_fd, stream, ciphertext)
                unchanged(args.journal, source[1], 'CI_JOURNAL_SOURCE_CHANGED')
                result = {'ok': True, 'ciphertext_sha256': hashlib.sha256(ciphertext).hexdigest(),
                          'ciphertext_bytes': len(ciphertext), 'off_host_preservation_verified': False}
            else:
                result = {'ok': True, 'encryption_ready': True, 'off_host_decryptability_verified': False}
    finally:
        # Remove only our unpublished temporary name; keep source and any final file.
        try:
            if owned_inode is not None:
                named = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != owned_inode:
                    raise SealError('CI_JOURNAL_TEMPORARY_CHANGED')
                os.unlink(temporary, dir_fd=directory_fd)
                os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)
    print(json.dumps(result))


if __name__ == '__main__':
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--age-binary', type=Path, required=True)
    common.add_argument('--age-sha256', required=True)
    common.add_argument('--age-version', required=True)
    common.add_argument('--recipient-file', type=Path, required=True)
    common.add_argument('--recipient-sha256', required=True)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('check', parents=[common], help='Validate encryption before opening access; uses public canary bytes only')
    check.add_argument('--directory', type=Path, required=True)
    seal = commands.add_parser('seal', parents=[common], help='Encrypt the required exact journal after cleanup finishes')
    seal.add_argument('--journal', type=Path, required=True)
    seal.add_argument('--output', type=Path, required=True)
    try:
        main(parser.parse_args())
    except (Exception, KeyboardInterrupt) as error:
        code = str(error) if isinstance(error, SealError) else 'CI_JOURNAL_PRESERVATION_UNCONFIRMED'
        print(json.dumps({'ok': False, 'code': code, 'preservation': 'unconfirmed',
                          'action': 'Retain the private journal and reconcile access cleanup before retrying.'}))
        raise SystemExit(1)
