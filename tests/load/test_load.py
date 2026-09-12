import json
from pathlib import Path
import tempfile
import unittest

from run import evaluate, metric, target_config


class LoadValidation(unittest.TestCase):
    def test_guard_rejects_shared_service_remote_target_and_wrong_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'target.json'
            base = {'project': 'zavliq-load', 'environment': 'local', 'hardware': 'Dedicated diagnostic machine', 'origin': 'http://localhost:19080'}
            path.write_text(json.dumps(base))
            self.assertEqual(target_config(path)['environment'], 'local')
            for change in ({'origin': 'http://localhost:8080'}, {'origin': 'https://zavliq.com'}, {'origin': 'http://localhost:18080'}, {'origin': 'http://localhost:19080/path'}, {'project': 'zavliq-local'}, {'environment': 'production'}):
                path.write_text(json.dumps({**base, **change}))
                with self.assertRaises(ValueError):
                    target_config(path)

    def test_gate_requires_full_workload_no_loss_and_measured_p95(self):
        parameters = {'clients': 100, 'duration': 1800, 'rate': 10, 'planned': 18000, 'accepted': 18000, 'observed': 18000, 'missed_slots': 0, 'failed': 0, 'duplicates': 0, 'latency': {'p95_seconds': 1.5}, 'environment': 'aws-staging'}
        self.assertTrue(evaluate(**parameters)['aws_staging_gate_passed'])
        for change in ({'accepted': 17999}, {'observed': 17999}, {'missed_slots': 1}, {'failed': 1}, {'duplicates': 1}, {'duration': 30}, {'clients': 10}, {'latency': {'p95_seconds': 2.0}}, {'environment': 'local'}):
            self.assertFalse(evaluate(**{**parameters, **change})['aws_staging_gate_passed'])

    def test_percentile_uses_tail_without_averaging(self):
        measured = metric([.1] * 94 + [2.5] * 6)
        self.assertEqual(measured['p95_seconds'], 2.5)
        self.assertIsNone(metric([])['p95_seconds'])


if __name__ == '__main__':
    unittest.main()
