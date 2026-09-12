import json
from pathlib import Path
import tempfile
import unittest

from zavliq_echo.health import Health


class HealthTests(unittest.TestCase):
    def test_status_file_is_atomic_private_content_free_and_refreshes_without_continuous_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000]
            path = Path(directory)
            health = Health(path, '@echo:local', now=lambda: clock[0])
            health.update('starting')
            health.update('ready')
            expected = {'status': 'ready', 'user_id': '@echo:local', 'updated_at': 1000}
            self.assertEqual(json.loads((path / 'health.json').read_text()), expected)
            self.assertEqual((path / 'health.json').stat().st_mode & 0o777, 0o600)
            clock[0] = 1009
            health.update('ready')
            self.assertEqual(json.loads((path / 'health.json').read_text()), expected)
            clock[0] = 1010
            health.update('ready')
            self.assertEqual(json.loads((path / 'health.json').read_text())['updated_at'], 1010)
            health.update('retrying')
            self.assertEqual(json.loads((path / 'health.json').read_text())['status'], 'retrying')
            health.update('stopped')
            self.assertEqual(json.loads((path / 'health.json').read_text())['status'], 'stopped')
            self.assertEqual([entry.name for entry in path.iterdir()], ['health.json'])
            with self.assertRaises(ValueError):
                health.update('message content')
