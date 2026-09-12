"""The standalone identity helper uses only a bounded, non-retrying AWS call."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / 'scripts/prepare-ssh.py'


class PrepareSshTests(unittest.TestCase):
    def setUp(self):
        self.previous_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.previous_umask)

    def test_credential_response_is_private_and_aws_call_has_its_own_deadline(self):
        details = {'ipAddress': '192.0.2.10', 'username': 'ubuntu',
                   'privateKey': 'SYNTHETIC_PRIVATE_KEY', 'certKey': 'SYNTHETIC_CERT',
                   'hostKeys': [{'publicKey': 'ssh-ed25519 SYNTHETIC_HOST_KEY'}]}
        response = subprocess.CompletedProcess(['aws'], 0,
                    json.dumps({'accessDetails': details}), '')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'ssh'
            stdout = io.StringIO()
            with patch.object(sys, 'argv', [str(SCRIPT), '--instance', 'zavliq-production',
                                           '--directory', str(directory)]), \
                    patch('subprocess.run', return_value=response) as aws, \
                    contextlib.redirect_stdout(stdout):
                runpy.run_path(str(SCRIPT), run_name='__main__')
            argv = aws.call_args.args[0]
            self.assertEqual(argv[:3], ['aws', 'lightsail', 'get-instance-access-details'])
            self.assertEqual(argv[argv.index('--region') + 1], 'us-east-1')
            self.assertEqual(argv[argv.index('--instance-name') + 1], 'zavliq-production')
            self.assertEqual(argv[argv.index('--cli-connect-timeout') + 1], '5')
            self.assertEqual(argv[argv.index('--cli-read-timeout') + 1], '15')
            self.assertIn('--no-cli-pager', argv)
            self.assertEqual(aws.call_args.kwargs['timeout'], 25)
            self.assertEqual(aws.call_args.kwargs['env']['AWS_MAX_ATTEMPTS'], '1')
            self.assertEqual(aws.call_args.kwargs['env']['AWS_EC2_METADATA_DISABLED'], 'true')
            self.assertNotIn('SYNTHETIC_', stdout.getvalue())
            self.assertEqual((directory / 'identity').read_text(), details['privateKey'])
            self.assertIn('StrictHostKeyChecking yes', (directory / 'config').read_text())
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir()))

    def test_timeout_never_writes_partial_identity_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'ssh'
            with patch.object(sys, 'argv', [str(SCRIPT), '--instance', 'zavliq-production',
                                           '--directory', str(directory)]), \
                    patch('subprocess.run', side_effect=subprocess.TimeoutExpired('aws', 25)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    runpy.run_path(str(SCRIPT), run_name='__main__')
            self.assertEqual(list(directory.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
