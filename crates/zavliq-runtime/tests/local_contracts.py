"""Local process regressions. Uses synthetic credentials and an isolated loopback server."""
import json
import os
import select
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BINARY = Path(__file__).resolve().parents[1] / 'target/debug/zavliq'
PROOF = 'synthetic-pairing-proof-private'
TOKEN = 'synthetic-session-token-private'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def result(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.server.request_started.set()
        self.server.release.wait(5)
        self.result(200, {'versions': ['v1.11']})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        if self.path == '/v1/pairings':
            self.server.starts += 1
            self.result(201, {'pairing_id': 'public-request', 'pairing_secret': PROOF,
                             'confirmation_code': '123456', 'expires_at': int(time.time() * 1000) + (-1000 if self.server.expired else 300000)})
        elif self.path.endswith('/poll'):
            assert body['pairing_secret'] == PROOF
            self.server.polls += 1
            if not self.server.approved:
                self.result(200, {'status': 'pending', 'retry_after_ms': 2000})
            else:
                self.result(200, {'status': 'approved', 'credentials': {
                    'user_id': '@different:localhost' if self.server.wrong_user else '@sage:localhost',
                    'device_id': 'NEW-DEVICE', 'access_token': TOKEN, 'homeserver': self.server.url}})
        elif self.path.endswith('/ack'):
            assert body['pairing_secret'] == PROOF
            self.server.acks += 1
            # Model a server that consumed the acknowledgement but lost its response.
            if self.server.acks == 1:
                self.result(503, {'error': {'code': 'SERVICE_UNAVAILABLE', 'message': 'Retry acknowledgement'}})
            else:
                self.result(200, {'completed': True})
        else:
            self.server.request_started.set()
            self.server.release.wait(5)
            self.result(200, {'one_time_key_counts': {}})


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zavliq-local-contract-')
        self.directory = Path(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.url = f'http://127.0.0.1:{self.server.server_port}'
        self.server.starts = self.server.polls = self.server.acks = 0
        self.server.approved = self.server.wrong_user = self.server.expired = False
        self.server.request_started = threading.Event()
        self.server.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def call(self, method, params=None, error=False):
        result = subprocess.run([str(BINARY), '--data-dir', str(self.directory), '--control-url',
                                 self.server.url, 'call', method, '--params', json.dumps(params or {})],
                                capture_output=True, text=True, timeout=15)
        self.assertNotIn(PROOF, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertEqual(result.returncode != 0, error, result.stdout)
        return json.loads(result.stdout)

    def test_pairing_resumes_after_lost_ack_without_activating_pending_credentials(self):
        first = self.call('pairing_start', {'user_id': '@sage:localhost'})
        resumed = self.call('pairing_start', {'user_id': '@sage:localhost'})
        self.assertEqual(first['pairing_id'], resumed['pairing_id'])
        self.assertEqual(self.server.starts, 1)
        self.assertEqual(self.call('pairing_complete')['status'], 'pending')
        self.assertFalse((self.directory / 'identity.json').exists())
        self.assertEqual(self.call('init', {'handle': 'sage'}, error=True)['error']['code'], 'PAIRING_IN_PROGRESS')
        self.server.approved = True
        self.call('pairing_complete', error=True)
        self.assertFalse((self.directory / 'identity.json').exists())
        self.assertEqual(self.call('identity', error=True)['error']['code'], 'NOT_INITIALIZED')
        completed = self.call('pairing_complete')
        self.assertEqual(completed['device_id'], 'NEW-DEVICE')
        self.assertEqual(self.server.polls, 2)  # retry reuses privately persisted credentials, not poll
        self.assertFalse((self.directory / 'pairing.json').exists())
        self.assertEqual(self.call('identity')['user_id'], '@sage:localhost')
        self.assertEqual(self.call('pairing_complete')['status'], 'completed')
        self.assertEqual(self.server.acks, 2)
        if os.name == 'posix':
            self.assertEqual((self.directory / 'identity.json').stat().st_mode & 0o777, 0o600)

    def test_expired_unused_pairing_can_restart_in_the_same_directory(self):
        self.server.expired = True
        self.call('pairing_start', {'user_id': '@sage:localhost'})
        self.server.expired = False
        renewed = self.call('pairing_start', {'user_id': '@sage:localhost'})
        self.assertEqual(self.server.starts, 2)
        self.assertGreater(renewed['expires_at'], time.time() * 1000)
        self.assertFalse((self.directory / 'identity.json').exists())

    def test_pairing_rejects_unexpected_account(self):
        self.call('pairing_start', {'user_id': '@sage:localhost'})
        self.server.approved = self.server.wrong_user = True
        result = self.call('pairing_complete', error=True)
        self.assertEqual(result['error']['code'], 'PAIRING_IDENTITY_MISMATCH')
        self.assertEqual(self.server.acks, 0)
        self.assertFalse((self.directory / 'identity.json').exists())

    def test_local_rpc_remains_responsive_during_stalled_background_network(self):
        identity = {'handle': 'sage', 'control_url': self.server.url, 'registration_secret': PROOF,
                    'store_passphrase': 'synthetic-passphrase', 'session': {'user_id': '@sage:localhost',
                    'device_id': 'DEVICE', 'access_token': TOKEN, 'homeserver': self.server.url}}
        (self.directory / 'identity.json').write_text(json.dumps(identity))
        process = subprocess.Popen([str(BINARY), '--data-dir', str(self.directory), '--control-url',
                                    self.server.url, 'rpc'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, bufsize=1)
        try:
            self.assertTrue(self.server.request_started.wait(20), 'background network request never started')
            started = time.monotonic()
            process.stdin.write('{"jsonrpc":"2.0","id":7,"method":"identity","params":{}}\n')
            process.stdin.flush()
            self.assertTrue(select.select([process.stdout], [], [], 1)[0], 'local identity blocked on network')
            result = json.loads(process.stdout.readline())
            self.assertEqual(result['id'], 7)
            self.assertEqual(result['result']['user_id'], '@sage:localhost')
            self.assertLess(time.monotonic() - started, 1)
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()


if __name__ == '__main__':
    unittest.main()
