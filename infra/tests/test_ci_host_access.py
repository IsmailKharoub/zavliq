import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('ci_host_access', Path(__file__).parents[1] / 'scripts/ci-host-access.py')
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)
CIDR = '8.8.8.8/32'
INSTANCE = 'zavliq-production'
ACCOUNT = '123456789012'
ARN = 'arn:aws:lightsail:us-east-1:' + ACCOUNT + ':Instance/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
IDENTITY = {'instance': INSTANCE, 'instance_arn': ARN, 'account_id': ACCOUNT}
OPEN_ID = '11111111-1111-1111-1111-111111111111'
CLOSE_ID = '22222222-2222-2222-2222-222222222222'


def rule(cidr=CIDR):
    return {'protocol': 'tcp', 'fromPort': 22, 'toPort': 22, 'cidrs': [cidr]}


def operation(kind, status='Succeeded', terminal=True, **changes):
    return {'id': OPEN_ID if kind == 'open' else CLOSE_ID,
            'resourceName': INSTANCE, 'resourceType': 'Instance',
            'operationType': kind.title() + 'InstancePublicPorts',
            'location': {'regionName': access.REGION},
            'status': status, 'isTerminal': terminal, **changes}


def state_for(directory, open_known=True, identity=None):
    identity = identity or IDENTITY
    state = {'schema': 'zavliq-ci-host-access-v2', **identity,
             'cidr': CIDR, 'close_required': True}
    access.write_state(directory, state)
    if open_known:
        access.record_operation(directory, state, 'open', operation('open', resourceName=identity['instance']))
    return state


def state_at(directory):
    return json.loads((directory / 'state.json').read_text())


class Clock:
    value = 0

    def monotonic(self):
        return self.value

    def sleep(self, value):
        self.value += value


class HostAccessTests(unittest.TestCase):
    def test_protected_identity_validation_has_no_defaults_or_wildcards(self):
        replacement = 'zavliq-production-recovery-20260912-a1b2'
        self.assertEqual(access.validate_identity(**IDENTITY), IDENTITY)
        self.assertEqual(access.validate_identity(replacement, ARN, ACCOUNT)['instance'], replacement)
        invalid = [
            {'instance': None}, {'instance': ''}, {'instance': 'zavliq-staging'},
            {'instance': 'zavliq-production-recovery-'}, {'instance': 'zavliq-production-recovery--a'},
            {'instance': 'zavliq-production-recovery-a-'}, {'instance': 'zavliq-production-recovery-A'},
            {'instance': 'zavliq-production-recovery-' + 'a' * 37},
            {'instance': 'zavliq-production;echo'}, {'account_id': '123'}, {'account_id': None},
            {'account_id': '999999999999'}, {'instance_arn': None}, {'instance_arn': '*'},
            {'instance_arn': ARN.replace('us-east-1', 'eu-west-1')},
            {'instance_arn': ARN.replace(':Instance/', ':Disk/')},
            {'instance_arn': ARN + '/*'}, {'instance_arn': ARN + ':other'},
        ]
        with patch.object(access, 'aws') as aws:
            for changes in invalid:
                with self.subTest(changes=changes), self.assertRaisesRegex(access.AccessError, 'EXACT_PRODUCTION'):
                    access.validate_identity(**(IDENTITY | changes))
        aws.assert_not_called()

    def test_open_validates_actual_name_and_arn_before_credentials_or_rules(self):
        for changes in [{'name': 'zavliq-production-recovery-other'},
                        {'arn': ARN.replace('aaaaaaaa', 'bbbbbbbb')},
                        {'arn': ARN.replace(ACCOUNT, '999999999999')}]:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / 'access'
                instance = {'name': INSTANCE, 'arn': ARN, 'networking': {'ports': []}} | changes
                with patch.object(access, 'aws', return_value={'instance': instance}) as aws, \
                        patch.object(access, 'runner_cidr') as ip, \
                        patch.object(access.subprocess, 'run') as credentials:
                    with self.assertRaisesRegex(access.AccessError, 'PRODUCTION_INSTANCE_MISMATCH'):
                        access.open_access(directory, **IDENTITY)
                self.assertEqual([call.args[:3] for call in aws.call_args_list],
                                 [('get-instance', '--instance-name', INSTANCE)])
                credentials.assert_not_called()
                ip.assert_not_called()
                self.assertFalse((directory / 'state.json').exists())

    def test_replacement_open_and_cleanup_use_exact_journal_despite_changed_environment(self):
        replacement = IDENTITY | {'instance': 'zavliq-production-recovery-20260912-a1b2'}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'access'
            rules, stages = [], []
            def service(*args, **kwargs):
                stages.append(args[0])
                if args[0] == 'get-instance':
                    self.assertEqual(args[2], replacement['instance'])
                    return {'instance': {'name': replacement['instance'], 'arn': ARN,
                                         'networking': {'ports': list(rules)}}}
                if args[0] == 'get-operation':
                    return {'operation': operation('open' if args[2] == OPEN_ID else 'close',
                                                   resourceName=replacement['instance'])}
                self.assertEqual(args[2], replacement['instance'])
                self.assertEqual(json.loads(args[4]), rule())
                kind = 'open' if args[0] == 'open-instance-public-ports' else 'close'
                if kind == 'open': rules.append(rule())
                else: rules.clear()
                return {'operation': operation(kind, resourceName=replacement['instance'])}
            def credentials(argv, **kwargs):
                self.assertEqual(stages, ['get-instance'])
                self.assertEqual(argv[2:4], ['--instance', replacement['instance']])
                self.assertEqual(state_at(directory)['schema'], 'zavliq-ci-host-access-v2')
                self.assertFalse(state_at(directory)['close_required'])
            with patch.object(access, 'aws', side_effect=service), \
                    patch.object(access, 'runner_cidr', return_value=CIDR), \
                    patch.object(access.subprocess, 'run', side_effect=credentials):
                self.assertTrue(access.open_access(directory, **replacement)['temporary_rule_owned'])
                self.assertEqual({key: state_at(directory)[key] for key in replacement}, replacement)
                with patch.dict(access.os.environ, {'INSTANCE': 'zavliq-production-recovery-other',
                        'INSTANCE_ARN': ARN.replace('aaaaaaaa', 'bbbbbbbb'), 'EXPECTED_ACCOUNT': '999999999999'}):
                    self.assertEqual(access.close_access(directory)['cleanup'], 'temporary_rule_closed')
            self.assertFalse(rules)
            self.assertEqual(stages, ['get-instance', 'get-instance', 'open-instance-public-ports',
                'get-operation', 'get-instance', 'get-instance', 'get-operation', 'get-instance',
                'close-instance-public-ports', 'get-operation', 'get-instance'])

    def test_reused_instance_name_refuses_cleanup_and_preserves_original_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_for(directory)
            original = (directory / 'state.json').read_bytes()
            foreign = {'name': INSTANCE, 'arn': ARN.replace('aaaaaaaa', 'bbbbbbbb'),
                       'networking': {'ports': [rule()]}}
            with patch.object(access, 'aws', return_value={'instance': foreign}) as aws:
                with self.assertRaisesRegex(access.AccessError, 'PRODUCTION_INSTANCE_MISMATCH'):
                    access.close_access(directory)
            self.assertEqual([call.args[0] for call in aws.call_args_list], ['get-instance'])
            self.assertEqual((directory / 'state.json').read_bytes(), original)

    def test_identity_is_rechecked_immediately_before_rule_mutation(self):
        for kind in ('open', 'close'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                state = state_for(directory)
                original = (directory / 'state.json').read_bytes()
                foreign = {'name': INSTANCE, 'arn': ARN.replace('aaaaaaaa', 'bbbbbbbb'),
                           'networking': {'ports': []}}
                with patch.object(access, 'aws', return_value={'instance': foreign}) as aws:
                    with self.assertRaisesRegex(access.AccessError, 'PRODUCTION_INSTANCE_MISMATCH'):
                        access.change_rule(kind, directory, state, access.Deadline())
                self.assertEqual([call.args[0] for call in aws.call_args_list], ['get-instance'])
                self.assertEqual((directory / 'state.json').read_bytes(), original)

    def test_cli_requires_explicit_open_identity_and_close_cannot_override_journal(self):
        for arguments in [['open', '--directory', '/unused'],
                          ['close', '--directory', '/unused', '--instance', INSTANCE]]:
            result = subprocess.run([sys.executable, '-B', str(SPEC.origin), *arguments],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('error:', result.stderr)

    def test_existing_operator_access_is_not_owned_or_removed(self):
        for allowed in [CIDR, '8.8.8.0/24']:
            with self.subTest(allowed=allowed), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / 'access'
                with patch.object(access, 'runner_cidr', return_value=CIDR), \
                        patch.object(access, 'ports', return_value=[rule(allowed)]), \
                        patch.object(access.subprocess, 'run'), patch.object(access, 'change_rule') as change:
                    self.assertFalse(access.open_access(directory, **IDENTITY)['temporary_rule_owned'])
                    self.assertEqual(access.close_access(directory)['cleanup'], 'no_owned_rule')
                change.assert_not_called()

    def test_unknown_open_cannot_clear_ownership_even_after_successful_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'access'
            def lost_open(*args, **kwargs):
                self.assertEqual(args[0], 'open-instance-public-ports')
                persisted = state_at(directory)
                self.assertTrue(persisted['close_required'])
                self.assertIsNone(persisted['open_operation']['id'])
                raise subprocess.TimeoutExpired('aws', 25)
            with patch.object(access, 'runner_cidr', return_value=CIDR), patch.object(access, 'ports', return_value=[]), \
                    patch.object(access.subprocess, 'run'), patch.object(access, 'aws', side_effect=lost_open):
                with self.assertRaises(subprocess.TimeoutExpired):
                    access.open_access(directory, **IDENTITY)
            with patch.object(access, 'aws', return_value={'operation': operation('close')}), \
                    patch.object(access, 'ports', return_value=[]):
                with self.assertRaisesRegex(access.AccessError, 'OPEN_OUTCOME_UNKNOWN'):
                    access.close_access(directory)
            self.assertTrue(state_at(directory)['close_required'])
            self.assertEqual(state_at(directory)['close_operation']['status'], 'Succeeded')

    def test_credentials_failure_never_opens_ssh(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'access'
            with patch.object(access, 'runner_cidr', return_value=CIDR), patch.object(access, 'ports', return_value=[]), \
                    patch.object(access.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'aws')), \
                    patch.object(access, 'change_rule') as change:
                with self.assertRaises(subprocess.CalledProcessError):
                    access.open_access(directory, **IDENTITY)
                self.assertEqual(access.close_access(directory)['cleanup'], 'no_owned_rule')
                change.assert_not_called()

    def test_pending_close_does_not_pass_on_absent_port_and_resumes_same_operation(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_for(directory)
            def aws(*args, **kwargs):
                if args[0] == 'get-operation' and args[2] == OPEN_ID:
                    return {'operation': operation('open')}
                return {'operation': operation('close', 'Started', False)}
            with patch.object(access, 'aws', side_effect=aws), patch.object(access, 'ports', return_value=[]) as ports, \
                    patch.object(access.time, 'monotonic', clock.monotonic), patch.object(access.time, 'sleep', clock.sleep):
                with self.assertRaisesRegex(access.AccessError, 'TIMEOUT'):
                    access.close_access(directory, access.Deadline(3))
                self.assertEqual(ports.call_count, 2)  # Identity preflights cannot replace terminal operation success.
            self.assertTrue(state_at(directory)['close_required'])
            self.assertEqual(state_at(directory)['close_operation']['id'], CLOSE_ID)
            calls = []
            def completed(*args, **kwargs):
                calls.append(args)
                return {'operation': operation('open' if args[2] == OPEN_ID else 'close')}
            with patch.object(access, 'aws', side_effect=completed), patch.object(access, 'ports', return_value=[]):
                self.assertEqual(access.close_access(directory)['cleanup'], 'temporary_rule_closed')
                self.assertEqual(access.close_access(directory)['cleanup'], 'no_owned_rule')
            self.assertEqual([c[0] for c in calls], ['get-operation', 'get-operation'])
            self.assertFalse(state_at(directory)['close_required'])

    def test_pending_open_must_finish_before_close_is_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = state_for(directory)
            access.record_operation(directory, state, 'open', operation('open', 'Started', False))
            stages = []
            def aws(*args, **kwargs):
                stages.append(args[0])
                if args[0] == 'get-operation' and args[2] == OPEN_ID:
                    self.assertNotIn('close-instance-public-ports', stages)
                    return {'operation': operation('open', 'Started', False) if len(stages) == 1 else operation('open')}
                return {'operation': operation('close')}
            with patch.object(access, 'aws', side_effect=aws), patch.object(access, 'ports', return_value=[]), \
                    patch.object(access.time, 'sleep'):
                access.close_access(directory)
            self.assertEqual(stages, ['get-operation', 'get-operation', 'close-instance-public-ports', 'get-operation'])

    def test_unfinished_open_never_claims_cleanup_and_retains_its_id(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_for(directory)
            with patch.object(access, 'aws', return_value={'operation': operation('open', 'Started', False)}) as aws, \
                    patch.object(access, 'ports', return_value=[]), patch.object(access.time, 'monotonic', clock.monotonic), patch.object(access.time, 'sleep', clock.sleep):
                with self.assertRaisesRegex(access.AccessError, 'TIMEOUT'):
                    access.close_access(directory, access.Deadline(2))
            self.assertTrue(state_at(directory)['close_required'])
            self.assertTrue(all(c.args[0] == 'get-operation' for c in aws.call_args_list))

    def test_terminal_close_with_stale_present_port_cannot_clear_ownership(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_for(directory)
            def aws(*args, **kwargs):
                return {'operation': operation('open' if args[0] == 'get-operation' and args[2] == OPEN_ID else 'close')}
            with patch.object(access, 'aws', side_effect=aws), patch.object(access, 'ports', return_value=[rule()]), \
                    patch.object(access.time, 'monotonic', clock.monotonic), patch.object(access.time, 'sleep', clock.sleep):
                with self.assertRaisesRegex(access.AccessError, 'TIMEOUT'):
                    access.close_access(directory, access.Deadline(2))
            self.assertTrue(state_at(directory)['close_required'])

    def test_unknown_close_retains_ownership_and_retry_can_confirm_a_new_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_for(directory)
            def lost_close(*args, **kwargs):
                if args[0] == 'get-operation':
                    return {'operation': operation('open')}
                self.assertIsNone(state_at(directory)['close_operation']['id'])
                raise subprocess.TimeoutExpired('aws', 25)
            with patch.object(access, 'aws', side_effect=lost_close), patch.object(access, 'ports', return_value=[]):
                with self.assertRaises(subprocess.TimeoutExpired):
                    access.close_access(directory)
            self.assertTrue(state_at(directory)['close_required'])
            responses = [operation('open'), operation('close'), operation('close')]
            with patch.object(access, 'aws', side_effect=[{'operation': value} for value in responses]) as aws, \
                    patch.object(access, 'ports', return_value=[]):
                self.assertEqual(access.close_access(directory)['cleanup'], 'temporary_rule_closed')
            self.assertEqual(aws.call_args_list[1].args[0], 'close-instance-public-ports')
            self.assertFalse(state_at(directory)['close_required'])

    def test_failed_close_retains_ownership_but_can_be_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_for(directory)
            def aws(*args, **kwargs):
                return {'operation': operation('open') if args[0] == 'get-operation' and args[2] == OPEN_ID
                        else operation('close', 'Failed', True, errorDetails='must not persist private output')}
            with patch.object(access, 'aws', side_effect=aws), patch.object(access, 'ports') as ports:
                with self.assertRaisesRegex(access.AccessError, 'OPERATION_FAILED'):
                    access.close_access(directory)
                self.assertEqual(ports.call_count, 2)
            self.assertTrue(state_at(directory)['close_required'])
            self.assertNotIn('private output', (directory / 'state.json').read_text())
            responses = [operation('open'), operation('close', 'Failed'), operation('close'), operation('close')]
            with patch.object(access, 'aws', side_effect=[{'operation': value} for value in responses]) as aws, \
                    patch.object(access, 'ports', return_value=[]):
                access.close_access(directory)
            self.assertEqual(aws.call_args_list[2].args[0], 'close-instance-public-ports')

    def test_operation_identity_mismatch_cannot_authorize_a_close(self):
        for changes in [{'id': CLOSE_ID}, {'resourceName': 'another-instance'}, {'resourceType': 'Disk'},
                        {'operationType': 'CloseInstancePublicPorts'}, {'location': {'regionName': 'eu-west-1'}}]:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                state_for(directory)
                with patch.object(access, 'aws', return_value={'operation': operation('open', **changes)}), patch.object(access, 'ports', return_value=[]):
                    with self.assertRaisesRegex(access.AccessError, 'IDENTITY_UNCONFIRMED'):
                        access.close_access(directory)
                self.assertTrue(state_at(directory)['close_required'])

    def test_only_fixed_production_tcp_22_and_one_host_are_changed(self):
        for kind in ['open', 'close']:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                state = state_for(directory)
                with patch.object(access, 'aws', return_value={'operation': operation(kind)}) as aws, \
                        patch.object(access, 'ports', return_value=[rule()] if kind == 'open' else []):
                    access.change_rule(kind, directory, state, access.Deadline())
                args = aws.call_args_list[0].args
                self.assertEqual(args[:3], (kind + '-instance-public-ports', '--instance-name', 'zavliq-production'))
                self.assertEqual(json.loads(args[4]), rule())
                self.assertEqual(aws.call_args_list[1].args[0], 'get-operation')

    def test_public_ip_probe_is_bounded_and_refuses_redirects_and_private_ips(self):
        for status, body, valid in [(200, b'8.8.8.8\n', True), (302, b'8.8.8.8', False),
                                    (200, b'127.0.0.1', False), (200, b'x' * 65, False)]:
            connection = Mock()
            response = connection.getresponse.return_value
            response.status, response.read.return_value = status, body
            with self.subTest(status=status, body=body), \
                    patch.object(access.http.client, 'HTTPSConnection', return_value=connection) as factory:
                if valid:
                    self.assertEqual(access.runner_cidr(access.Deadline()), CIDR)
                else:
                    with self.assertRaises((ValueError, access.AccessError)):
                        access.runner_cidr(access.Deadline())
                factory.assert_called_once_with('checkip.amazonaws.com', timeout=10)
                response.read.assert_called_once_with(65)
                connection.close.assert_called_once()

    def test_missing_journal_and_wrong_instance_cannot_remove_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(access, 'change_rule') as change:
                self.assertEqual(access.close_access(directory)['cleanup'], 'no_rule_journal')
                access.write_state(directory, {'schema': 'zavliq-ci-host-access-v1', 'instance': 'zavliq-staging',
                                               'cidr': CIDR, 'close_required': True})
                with self.assertRaisesRegex(access.AccessError, 'INVALID_RULE_JOURNAL'):
                    access.close_access(directory)
                change.assert_not_called()

    def test_cli_subprocess_is_killed_at_remaining_whole_operation_deadline(self):
        run = subprocess.run
        def stalled(_argv, **kwargs):
            self.assertEqual(kwargs['env']['AWS_MAX_ATTEMPTS'], '1')
            self.assertLessEqual(kwargs['timeout'], .08)
            return run([sys.executable, '-c', 'import time; time.sleep(5)'], **kwargs)
        started = time.monotonic()
        with patch.object(access.subprocess, 'run', side_effect=stalled):
            with self.assertRaises(subprocess.TimeoutExpired):
                access.aws('get-operation', '--operation-id', OPEN_ID, deadline=access.Deadline(.08))
        self.assertLess(time.monotonic() - started, 2)


if __name__ == '__main__':
    unittest.main()
