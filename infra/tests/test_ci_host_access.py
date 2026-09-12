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
OPEN_ID = '11111111-1111-1111-1111-111111111111'
CLOSE_ID = '22222222-2222-2222-2222-222222222222'


def rule(cidr=CIDR):
    return {'protocol': 'tcp', 'fromPort': 22, 'toPort': 22, 'cidrs': [cidr]}


def operation(kind, status='Succeeded', terminal=True, **changes):
    return {'id': OPEN_ID if kind == 'open' else CLOSE_ID,
            'resourceName': access.INSTANCE, 'resourceType': 'Instance',
            'operationType': kind.title() + 'InstancePublicPorts',
            'location': {'regionName': access.REGION},
            'status': status, 'isTerminal': terminal, **changes}


def state_for(directory, open_known=True):
    state = {'schema': 'zavliq-ci-host-access-v1', 'instance': access.INSTANCE,
             'cidr': CIDR, 'close_required': True}
    access.write_state(directory, state)
    if open_known:
        access.record_operation(directory, state, 'open', operation('open'))
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
    def test_existing_operator_access_is_not_owned_or_removed(self):
        for allowed in [CIDR, '8.8.8.0/24']:
            with self.subTest(allowed=allowed), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / 'access'
                with patch.object(access, 'runner_cidr', return_value=CIDR), \
                        patch.object(access, 'ports', return_value=[rule(allowed)]), \
                        patch.object(access.subprocess, 'run'), patch.object(access, 'change_rule') as change:
                    self.assertFalse(access.open_access(directory)['temporary_rule_owned'])
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
                    access.open_access(directory)
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
                    access.open_access(directory)
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
                ports.assert_not_called()  # Port absence cannot substitute for terminal operation success.
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
                    patch.object(access.time, 'monotonic', clock.monotonic), patch.object(access.time, 'sleep', clock.sleep):
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
            with patch.object(access, 'aws', side_effect=lost_close):
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
                ports.assert_not_called()
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
                with patch.object(access, 'aws', return_value={'operation': operation('open', **changes)}):
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
