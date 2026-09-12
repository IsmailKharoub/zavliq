#!/usr/bin/env python3
"""Own only this CI run's temporary production SSH rule; never print credentials."""
import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

REGION = 'us-east-1'
OPERATION_SECONDS = 120
SUCCESS = {'Completed', 'Succeeded'}


class AccessError(RuntimeError):
    """A fixed, credential-free action code, safe for CI output."""


class Deadline:
    def __init__(self, seconds=OPERATION_SECONDS):
        self.end = time.monotonic() + seconds

    def remaining(self, maximum=None):
        value = self.end - time.monotonic()
        if value <= 0:
            raise AccessError('SSH_ACCESS_TIMEOUT_RETAIN_JOURNAL')
        return min(value, maximum) if maximum is not None else value

    def pause(self):
        time.sleep(self.remaining(1))


def aws(*arguments, deadline):
    result = subprocess.run(
        ['aws', 'lightsail', *arguments, '--region', REGION, '--output', 'json',
         '--cli-connect-timeout', '5', '--cli-read-timeout', '15', '--no-cli-pager'],
        check=True, capture_output=True, text=True, timeout=deadline.remaining(25),
        env={**os.environ, 'AWS_MAX_ATTEMPTS': '1'})
    return json.loads(result.stdout)


def validate_identity(instance, instance_arn, account_id):
    """Validate protected deployment inputs without reading environment or AWS."""
    if (not isinstance(instance, str) or len(instance) > 63 or
            re.fullmatch(r'zavliq-production(?:-recovery-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)?', instance) is None or
            not isinstance(account_id, str) or re.fullmatch(r'[0-9]{12}', account_id) is None or
            not isinstance(instance_arn, str) or
            re.fullmatch(r'arn:aws:lightsail:' + REGION + ':' + account_id + r':Instance/[A-Za-z0-9_-]{1,128}', instance_arn) is None):
        raise AccessError('EXACT_PRODUCTION_INSTANCE_IDENTITY_REQUIRED')
    return {'instance': instance, 'instance_arn': instance_arn, 'account_id': account_id}


def ports(identity, deadline):
    validate_identity(identity.get('instance'), identity.get('instance_arn'), identity.get('account_id'))
    instance = aws('get-instance', '--instance-name', identity['instance'], deadline=deadline)['instance']
    if instance.get('name') != identity['instance'] or instance.get('arn') != identity['instance_arn']:
        raise AccessError('PRODUCTION_INSTANCE_MISMATCH')
    return instance['networking']['ports']


def runner_cidr(deadline):
    connection = http.client.HTTPSConnection('checkip.amazonaws.com', timeout=deadline.remaining(10))
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        body = response.read(65)
        if response.status != 200 or len(body) > 64:
            raise AccessError('RUNNER_IP_RESPONSE_REFUSED')
        address = ipaddress.IPv4Address(body.decode('ascii').strip())
        if not address.is_global:
            raise AccessError('PUBLIC_RUNNER_IPV4_REQUIRED')
        return str(address) + '/32'
    finally:
        connection.close()


def already_allowed(rules, cidr):
    address = ipaddress.IPv4Network(cidr).network_address
    return any(rule.get('protocol') in ('tcp', 'all') and
               (rule.get('protocol') == 'all' or rule.get('fromPort', 65536) <= 22 <= rule.get('toPort', -1)) and
               any(address in ipaddress.IPv4Network(value) for value in rule.get('cidrs', []))
               for rule in rules)


def exact_rule_exists(rules, cidr):
    return any(rule.get('protocol') == 'tcp' and rule.get('fromPort') == 22 and rule.get('toPort') == 22
               and cidr in rule.get('cidrs', []) for rule in rules)


def write_state(directory, state):
    # Unique temp files allow a subsequent cleanup after an interrupted write.
    fd, name = tempfile.mkstemp(prefix='state-', suffix='.next', dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / 'state.json')
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def record_operation(directory, state, kind, value, expected_id=None):
    operation_id = value.get('id')
    if (not isinstance(operation_id, str) or
            not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', operation_id) or
            (expected_id is not None and operation_id != expected_id) or
            value.get('resourceName') != state['instance'] or value.get('resourceType') != 'Instance' or
            value.get('operationType') != kind.title() + 'InstancePublicPorts' or
            value.get('location', {}).get('regionName') != REGION or
            type(value.get('isTerminal')) is not bool or
            value.get('status') not in {'NotStarted', 'Started', 'Failed', *SUCCESS}):
        raise AccessError('SSH_OPERATION_IDENTITY_UNCONFIRMED_RETAIN_JOURNAL')
    # Never retain AWS errorDetails or other arbitrary service output.
    record = {'id': operation_id, 'status': value['status'], 'is_terminal': value['isTerminal'],
              'has_error': bool(value.get('errorCode'))}
    state[kind + '_operation'] = record
    write_state(directory, state)
    return record


def wait_operation(directory, state, kind, deadline):
    record = state[kind + '_operation']
    operation_id = record['id']
    while True:
        value = aws('get-operation', '--operation-id', operation_id, deadline=deadline)['operation']
        record = record_operation(directory, state, kind, value, operation_id)
        if record['is_terminal']:
            if record['status'] in SUCCESS and not record['has_error']:
                return True
            if record['status'] == 'Failed':
                return False
            raise AccessError('SSH_OPERATION_STATUS_UNCONFIRMED_RETAIN_JOURNAL')
        deadline.pause()


def change_rule(kind, directory, state, deadline):
    # A name can be reused after an instance is deleted. Never change its
    # replacement's firewall using an old name-bound operation journal.
    ports(state, deadline)
    # A persisted unknown request must survive a lost response/CLI timeout.
    state[kind + '_operation'] = {'id': None, 'status': 'Unknown', 'is_terminal': False}
    write_state(directory, state)
    info = {'fromPort': 22, 'toPort': 22, 'protocol': 'tcp', 'cidrs': [state['cidr']]}
    value = aws(kind + '-instance-public-ports', '--instance-name', state['instance'],
                '--port-info', json.dumps(info), deadline=deadline)['operation']
    record_operation(directory, state, kind, value)
    if not wait_operation(directory, state, kind, deadline):
        raise AccessError('SSH_OPERATION_FAILED_RETAIN_JOURNAL')
    confirm_ports(kind, state, deadline)


def confirm_ports(kind, state, deadline):
    while exact_rule_exists(ports(state, deadline), state['cidr']) != (kind == 'open'):
        deadline.pause()


def open_access(directory, *, instance, instance_arn, account_id, deadline=None):
    deadline = deadline or Deadline()
    identity = validate_identity(instance, instance_arn, account_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Verify the exact protected resource before requesting SSH credentials or
    # changing access. This response also provides the current borrowed rules.
    rules = ports(identity, deadline)
    cidr = runner_cidr(deadline)
    borrowed = already_allowed(rules, cidr)
    state = {'schema': 'zavliq-ci-host-access-v2', **identity, 'cidr': cidr, 'close_required': False}
    write_state(directory, state)
    subprocess.run(['python3', str(Path(__file__).with_name('prepare-ssh.py')), '--instance', identity['instance'],
                    '--directory', str(directory / 'ssh')], check=True, capture_output=True,
                   text=True, timeout=deadline.remaining(60))
    if not borrowed:
        state['close_required'] = True
        write_state(directory, state)
        change_rule('open', directory, state, deadline)
    return {'access_ready': True, 'temporary_rule_owned': not borrowed}


def close_access(directory, deadline=None):
    deadline = deadline or Deadline()
    path = directory / 'state.json'
    if not path.exists():
        return {'cleanup': 'no_rule_journal'}
    if directory.is_symlink() or path.is_symlink() or path.stat().st_size > 4096:
        raise AccessError('PRIVATE_RULE_JOURNAL_REQUIRED')
    state = json.loads(path.read_text())
    if state.get('schema') != 'zavliq-ci-host-access-v2' or type(state.get('close_required')) is not bool:
        raise AccessError('INVALID_RULE_JOURNAL')
    try:
        validate_identity(state.get('instance'), state.get('instance_arn'), state.get('account_id'))
    except AccessError as error:
        raise AccessError('INVALID_RULE_JOURNAL') from error
    network = ipaddress.IPv4Network(state['cidr'])
    if network.prefixlen != 32 or not network.network_address.is_global:
        raise AccessError('RUNNER_RULE_REQUIRED')
    if not state['close_required']:
        return {'cleanup': 'no_owned_rule'}

    # Cleanup uses the original journal, never the current workflow environment.
    # Refuse even a matching old operation ID if this name now has another ARN.
    ports(state, deadline)

    opening = state.get('open_operation', {})
    unknown_open = not opening.get('id')
    if not unknown_open:
        # A delayed open must settle before a close can prove final ownership release.
        # A terminal failed open can still have partial effects: close it explicitly.
        wait_operation(directory, state, 'open', deadline)
    closing = state.get('close_operation', {})
    if closing.get('id') and wait_operation(directory, state, 'close', deadline):
        confirm_ports('close', state, deadline)
    else:
        # Retrying an unknown/failed close is safe: a delayed close cannot reopen SSH.
        change_rule('close', directory, state, deadline)
    if unknown_open:
        # Best effort removes current exposure, but an untracked open may still arrive.
        raise AccessError('SSH_OPEN_OUTCOME_UNKNOWN_OPERATOR_RECONCILIATION_REQUIRED')
    state['close_required'] = False
    write_state(directory, state)
    return {'cleanup': 'temporary_rule_closed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    opening = operations.add_parser('open')
    opening.add_argument('--directory', type=Path, required=True)
    opening.add_argument('--instance', required=True)
    opening.add_argument('--instance-arn', required=True)
    opening.add_argument('--account-id', required=True)
    closing = operations.add_parser('close')
    closing.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = (open_access(args.directory.absolute(), instance=args.instance,
                              instance_arn=args.instance_arn, account_id=args.account_id)
                  if args.operation == 'open' else close_access(args.directory.absolute()))
        print(json.dumps(result))
    except Exception as error:
        code = str(error) if isinstance(error, AccessError) else 'SSH_ACCESS_FAILED_RETAIN_JOURNAL'
        print(json.dumps({'ok': False, 'code': code, 'error_class': type(error).__name__,
                          'action': 'Preserve the rule journal; retry cleanup for known operations or reconcile an unknown open with the operator.'}))
        raise SystemExit(1)
