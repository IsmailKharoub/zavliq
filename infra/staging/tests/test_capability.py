import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, BASE / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


issuer = module('issue_capability')
uploader = module('upload')
monitor = module('cloud_monitor')


class FakeS3:
    def __init__(self): self.calls = []
    def generate_presigned_post(self, **kwargs):
        self.calls.append(kwargs)
        return {'url': 'https://' + kwargs['Bucket'] + '.s3.us-east-1.amazonaws.com/', 'fields': {'key': kwargs['Key'], **kwargs['Fields']}}


class CapabilityTests(unittest.TestCase):
    def test_post_policies_have_fixed_keys_sizes_and_48_hour_expiry(self):
        client = FakeS3()
        capability = issuer.issue(client, 'zavliq-staging-backups-123456789012', 100001)
        self.assertEqual(capability['expires_at'] - capability['issued_at'], 172800)
        self.assertEqual(len(capability['backups']), 5)
        self.assertTrue(all(call['ExpiresIn'] == 172800 for call in client.calls))
        self.assertTrue(all('${filename}' not in call['Key'] for call in client.calls))
        health = next(call for call in client.calls if call['Key'] == 'status/health.json')
        backups = [call for call in client.calls if call['Key'].startswith('daily/')]
        self.assertIn(['content-length-range', 1, 65536], health['Conditions'])
        self.assertTrue(all(['content-length-range', 1, 268435456] in call['Conditions'] for call in backups))

    def test_capability_expiry_and_private_file_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capability.json'
            path.write_text(json.dumps({'version': 1, 'issued_at': 1000, 'expires_at': 2000, 'bucket': 'zavliq-staging-backups-123456789012'}))
            path.chmod(0o600)
            self.assertEqual(uploader.load_capability(path, 1500)['expires_at'], 2000)
            with self.assertRaises(ValueError): uploader.load_capability(path, 2000)
            path.chmod(0o644)
            with self.assertRaises(ValueError): uploader.load_capability(path, 1500)

    def test_arbitrary_endpoint_or_oversized_upload_is_refused(self):
        capability = {'bucket': 'zavliq-staging-backups-123456789012'}
        form = {'maximum_bytes': 64, 'url': 'https://example.com/'}
        with self.assertRaises(ValueError): uploader.upload(capability, form, io.BytesIO(b'x'), 1)
        with self.assertRaises(ValueError): uploader.upload(capability, form, io.BytesIO(b'x'), 65)

    def test_upload_errors_preserve_only_known_safe_codes(self):
        known = uploader.safe_error(ValueError('CAPABILITY_EXPIRED_OR_INVALID'))
        self.assertEqual(known['code'], 'CAPABILITY_EXPIRED_OR_INVALID')
        self.assertIn('newly issued', known['action'])
        unsafe = uploader.safe_error(ValueError('https://bucket.invalid/?signature=private-value'))
        self.assertEqual(unsafe['code'], 'INVALID_LOCAL_STATE')
        self.assertNotIn('private-value', json.dumps(unsafe))

    def test_stale_unhealthy_and_expiring_telemetry_fail(self):
        report = {'ok': True, 'checked_at': 10000, 'capability_issued_at': 9000, 'capability_expires_at': 20000}
        self.assertEqual(monitor.health_metrics(report, 10000), {'HostHealthy': 1, 'CapabilityValid': 1})
        self.assertEqual(monitor.health_metrics(report, 10200)['HostHealthy'], 0)
        self.assertEqual(monitor.health_metrics({**report, 'ok': False}, 10000)['HostHealthy'], 0)
        self.assertEqual(monitor.health_metrics(report, 17000)['CapabilityValid'], 0)

    def test_old_snapshot_reupload_does_not_reset_backup_age(self):
        metadata = {'created-at': '1000', 'sha256': 'a' * 64}
        self.assertTrue(monitor.backup_fresh(metadata, 2000))
        self.assertFalse(monitor.backup_fresh(metadata, 100000))
        self.assertFalse(monitor.backup_fresh({'created-at': '100000'}, 100000))

    def test_monitor_bounds_clients_and_checks_newest_four_despite_one_head_failure(self):
        now = 10000
        calls, heads, metric_calls = [], [], []
        body = io.BytesIO(json.dumps({'ok': True, 'checked_at': now, 'capability_issued_at': 9000, 'capability_expires_at': 20000}).encode())
        class S3:
            def get_object(self, **kwargs): return {'ContentLength': len(body.getvalue()), 'Body': body}
            def list_objects_v2(self, **kwargs): return {'Contents': [{'Key': f'daily/zavliq-2026091{day}T000000Z.tar.age', 'Size': 100} for day in range(1, 7)]}
            def head_object(self, **kwargs):
                heads.append(kwargs['Key'])
                if len(heads) == 1: raise RuntimeError('Object disappeared')
                return {'Metadata': {'created-at': str(now), 'sha256': 'a' * 64}}
        class CloudWatch:
            def put_metric_data(self, **kwargs): metric_calls.append(kwargs)
        class Context:
            def get_remaining_time_in_millis(self): return 20000
        def client(name, **kwargs):
            calls.append(kwargs['config'])
            return S3() if name == 's3' else CloudWatch()
        with patch.object(monitor.boto3, 'client', side_effect=client), patch.object(monitor.time, 'time', return_value=now), patch.dict('os.environ', {'ZAVLIQ_BACKUP_BUCKET': 'bucket'}):
            result = monitor.handler({}, Context())
        self.assertEqual(result['BackupFresh'], 1)
        self.assertEqual(heads, ['daily/zavliq-20260916T000000Z.tar.age', 'daily/zavliq-20260915T000000Z.tar.age'])
        self.assertTrue(body.closed)
        self.assertTrue(all(config.connect_timeout == 1 and config.read_timeout == 2 and config.retries['total_max_attempts'] == 1 for config in calls))
        self.assertEqual(len(metric_calls), 1)

    def test_monitor_reserves_time_for_emitting_metrics(self):
        metric_calls, heads = [], []
        class S3:
            def get_object(self, **kwargs): raise RuntimeError('Unavailable')
            def list_objects_v2(self, **kwargs): return {'Contents': [{'Key': 'daily/zavliq-20260912T000000Z.tar.age', 'Size': 100}]}
            def head_object(self, **kwargs): heads.append(kwargs); return {}
        class CloudWatch:
            def put_metric_data(self, **kwargs): metric_calls.append(kwargs)
        class Context:
            def get_remaining_time_in_millis(self): return 4999
        with patch.object(monitor.boto3, 'client', side_effect=lambda name, **kwargs: S3() if name == 's3' else CloudWatch()), patch.dict('os.environ', {'ZAVLIQ_BACKUP_BUCKET': 'bucket'}):
            result = monitor.handler({}, Context())
        self.assertFalse(heads)
        self.assertEqual(len(metric_calls), 1)
        self.assertEqual(result['BackupFresh'], 0)


if __name__ == '__main__':
    unittest.main()
