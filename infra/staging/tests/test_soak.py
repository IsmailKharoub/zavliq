import importlib.util
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from probe import summarize, upload_failure


class SoakEvidence(unittest.TestCase):
    def setUp(self):
        self.marker = {'started_at': 1000, 'expected_revision': 'a' * 40, 'final_service_set': True}
        self.rows = [{'checked_at': 1000 + index * 60, 'ok': True} for index in range(1441)]

    def test_exact_full_day_of_healthy_samples_passes(self):
        result = summarize(self.rows, self.marker, 87400)
        self.assertTrue(result['complete'])
        self.assertTrue(result['final_service_soak_passed'])

    def test_early_run_failure_and_missing_minute_do_not_pass(self):
        self.assertFalse(summarize(self.rows[:-1], self.marker, 87399)['complete'])
        failed = [dict(row) for row in self.rows]
        failed[100]['ok'] = False
        self.assertFalse(summarize(failed, self.marker, 87400)['complete'])
        self.assertFalse(summarize(self.rows[:100] + self.rows[101:], self.marker, 87400)['continuity_passed'])

    def test_diagnostic_scope_cannot_claim_final_service_soak(self):
        result = summarize(self.rows, {**self.marker, 'final_service_set': False}, 87400)
        self.assertTrue(result['complete'])
        self.assertFalse(result['final_service_soak_passed'])

    def test_old_samples_cannot_fill_a_new_soak(self):
        result = summarize(self.rows, {**self.marker, 'started_at': 87400}, 87400)
        self.assertEqual(result['samples'], 1)
        self.assertFalse(result['complete'])

    def test_health_journal_logs_only_known_upload_error_and_static_action(self):
        result = upload_failure('{"code":"CAPABILITY_EXPIRED_OR_INVALID","action":"private-value"}')
        self.assertEqual(result['code'], 'CAPABILITY_EXPIRED_OR_INVALID')
        self.assertNotIn('private-value', str(result))
        self.assertEqual(upload_failure('https://example.invalid/?signature=private-value')['code'], 'INVALID_LOCAL_STATE')


if __name__ == '__main__':
    unittest.main()
