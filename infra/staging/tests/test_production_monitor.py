"""Isolated production Lambda contracts; no AWS credentials or live requests.

This file lives in the existing boto3-enabled CI test step. It imports only the
separate production monitor; private-stage monitor code/configuration is unchanged.
"""
from contextlib import ExitStack, redirect_stdout
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import signal
import time
import unittest
from unittest.mock import patch
import urllib.response

SPEC = importlib.util.spec_from_file_location('production_monitor',
    Path(__file__).resolve().parents[2] / 'scripts/cloud-monitor.py')
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
NOW = dt.datetime(2026, 9, 12, 12, tzinfo=dt.timezone.utc).timestamp()
ORIGIN = 'https://zavliq.example'
ENVIRONMENT = {'ZAVLIQ_ORIGIN': ORIGIN, 'ZAVLIQ_BACKUP_BUCKET': 'synthetic-production-backups',
               'ZAVLIQ_ENVIRONMENT': 'production'}


def backup(age_seconds, **extra):
    stamp = dt.datetime.fromtimestamp(NOW - age_seconds, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return {'Key': 'daily/zavliq-' + stamp + '.tar.age', 'Size': 1024, **extra}


class Clock:
    def __init__(self, milliseconds=40000):
        self.remaining = milliseconds
        self.elapsed = 0

    def get_remaining_time_in_millis(self):
        return self.remaining

    def consume(self, milliseconds):
        self.remaining -= milliseconds
        self.elapsed += milliseconds

    def now(self):
        return NOW + self.elapsed / 1000


class Response(io.BytesIO):
    def __init__(self, body, clock, *, headers=None, read_ms=0, chunk_limit=None):
        super().__init__(body)
        self.headers = headers or {}
        self.clock, self.read_ms, self.chunk_limit = clock, read_ms, chunk_limit
        self.reads = []

    def read1(self, size):
        self.reads.append((size, self.clock.remaining))
        self.clock.consume(self.read_ms)
        return super().read1(min(size, self.chunk_limit or size))


class Rig:
    def __init__(self, *, milliseconds=40000):
        self.clock = Clock(milliseconds)
        self.requests, self.responses, self.client_calls, self.list_calls, self.metric_calls = [], [], [], [], []
        self.objects = {'Contents': [backup(600)]}
        self.http_errors, self.http_durations = {}, {}
        self.s3_error, self.metric_error = None, None
        self.list_ms, self.metric_ms = 0, 0

    def open(self, url, *, timeout):
        self.requests.append((url, timeout, self.clock.remaining))
        path = url.removeprefix(ORIGIN)
        self.clock.consume(self.http_durations.get(path, 0))
        if path in self.http_errors:
            raise self.http_errors[path]
        payload = {'/health': {'status': 'ok'}, '/_matrix/client/versions': {'versions': ['v1.11']},
                   '/_zavliq/health': {'ok': True, 'checked_at': int(self.clock.now())}}[path]
        response = Response(json.dumps(payload).encode(), self.clock)
        self.responses.append(response)
        return response

    def client(self, name, **kwargs):
        self.client_calls.append((name, kwargs['config']))
        if name not in {'s3', 'cloudwatch'}:
            raise AssertionError('Unexpected client')
        return self

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        self.clock.consume(self.list_ms)
        if self.s3_error:
            raise self.s3_error
        return self.objects

    def put_metric_data(self, **kwargs):
        self.metric_calls.append((kwargs, self.clock.remaining))
        self.clock.consume(self.metric_ms)
        if self.metric_error:
            raise self.metric_error

    def run(self):
        self.logs = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', ENVIRONMENT))
            stack.enter_context(patch.object(monitor.boto3, 'client', side_effect=self.client))
            stack.enter_context(patch.object(monitor.HTTP, 'open', side_effect=self.open))
            stack.enter_context(patch.object(monitor.time, 'time', side_effect=self.clock.now))
            stack.enter_context(redirect_stdout(self.logs))
            return monitor.handler({}, self.clock)


class ProductionMonitorTests(unittest.TestCase):
    def test_healthy_metrics_preserve_public_endpoints_bucket_namespace_and_dimension(self):
        rig = Rig()
        self.assertEqual(rig.run(), {'Available': 1, 'BackupFresh': 1, 'HostHealthy': 1})
        self.assertEqual([request[0] for request in rig.requests],
            [ORIGIN + path for path in ['/health', '/_matrix/client/versions', '/_zavliq/health']])
        self.assertTrue(all(request[1] == 3 for request in rig.requests))
        self.assertTrue(all(response.closed for response in rig.responses))
        self.assertEqual(rig.list_calls, [{'Bucket': ENVIRONMENT['ZAVLIQ_BACKUP_BUCKET'], 'Prefix': 'daily/',
            'MaxKeys': 1000, 'StartAfter': 'daily/zavliq-20260911T120000Z.tar.age'}])
        self.assertEqual([name for name, _ in rig.client_calls], ['s3', 'cloudwatch'])
        for _, config in rig.client_calls:
            self.assertEqual((config.connect_timeout, config.read_timeout), (1, 2))
            self.assertEqual(config.retries, {'mode': 'standard', 'total_max_attempts': 1})
        self.assertEqual(len(rig.metric_calls), 1)
        metrics = rig.metric_calls[0][0]
        self.assertEqual(metrics['Namespace'], 'Zavliq')
        self.assertEqual({item['MetricName'] for item in metrics['MetricData']}, {'Available', 'BackupFresh', 'HostHealthy'})
        self.assertTrue(all(item['Value'] == 1 and item['Unit'] == 'Count' and
            item['Dimensions'] == [{'Name': 'Environment', 'Value': 'production'}] for item in metrics['MetricData']))

    def test_http_and_s3_timeouts_still_emit_all_three_failed_metrics(self):
        rig = Rig()
        rig.http_errors = {'/health': TimeoutError('private URL or response'), '/_zavliq/health': TimeoutError('private URL or response')}
        rig.http_durations = {'/health': 9000, '/_zavliq/health': 9000}
        rig.s3_error, rig.list_ms = TimeoutError('private bucket detail'), 3000
        rig.metric_ms = 3000
        self.assertEqual(rig.run(), {'Available': 0, 'BackupFresh': 0, 'HostHealthy': 0})
        self.assertEqual(len(rig.metric_calls), 1)
        self.assertGreaterEqual(rig.metric_calls[0][1], monitor.METRIC_RESERVE_MS)
        self.assertLess(rig.clock.elapsed, 40000)
        self.assertNotIn('private', rig.logs.getvalue())

    def test_low_remaining_time_skips_every_probe_but_attempts_metrics_once(self):
        rig = Rig(milliseconds=4999)
        rig.metric_ms = 3000
        self.assertEqual(rig.run(), {'Available': 0, 'BackupFresh': 0, 'HostHealthy': 0})
        self.assertEqual(rig.requests, [])
        self.assertEqual(rig.list_calls, [])
        self.assertEqual([name for name, _ in rig.client_calls], ['cloudwatch'])
        self.assertEqual(len(rig.metric_calls), 1)
        self.assertGreater(rig.clock.remaining, 0)

    def test_body_budget_is_rechecked_between_reads_and_response_is_closed(self):
        clock = Clock(15000)
        response = Response(b'{"ok":true}', clock, read_ms=3000, chunk_limit=1)
        with patch.object(monitor.HTTP, 'open', return_value=response):
            with self.assertRaises(TimeoutError): monitor.read_json(ORIGIN, '/health', clock)
        self.assertEqual(len(response.reads), 3)
        self.assertTrue(all(remaining >= monitor.METRIC_RESERVE_MS + 3000 for _, remaining in response.reads))
        self.assertGreaterEqual(clock.remaining, monitor.METRIC_RESERVE_MS)
        self.assertTrue(response.closed)

    def test_budget_exhausted_during_http_read_prevents_later_probes_and_preserves_emission(self):
        rig = Rig(milliseconds=15000)
        response = Response(b'{"ok":true}', rig.clock, read_ms=3000, chunk_limit=1)
        rig.open = lambda url, timeout: response
        self.assertEqual(rig.run(), {'Available': 0, 'BackupFresh': 0, 'HostHealthy': 0})
        self.assertTrue(response.closed)
        self.assertEqual(rig.list_calls, [])
        self.assertEqual(rig.metric_calls[0][1], 6000)

    def test_size_headers_stream_and_non_object_json_are_bounded(self):
        for body, headers in [(b'{}', {'Content-Length': '65537'}), (b'x' * 70000, {}),
                              (b'[]', {}), (b'{}', {'Content-Length': 'invalid'})]:
            with self.subTest(headers=headers, bytes=len(body)):
                response = Response(body, Clock(), headers=headers)
                with patch.object(monitor.HTTP, 'open', return_value=response):
                    with self.assertRaises((ValueError, json.JSONDecodeError)):
                        monitor.read_json(ORIGIN, '/health', response.clock)
                self.assertTrue(response.closed)
                self.assertLessEqual(sum(size for size, _ in response.reads), monitor.MAX_JSON_BYTES + 8192)

    def test_whole_response_deadline_interrupts_a_body_that_keeps_making_progress(self):
        class SlowlyProgressingResponse(Response):
            def read1(self, size):
                time.sleep(0.015)
                return super().read1(size)

        clock = Clock()
        response = SlowlyProgressingResponse(b'{"value":"' + b'x' * 100 + b'"}', clock, chunk_limit=1)
        prior_handler = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with patch.object(monitor, 'HTTP_TOTAL_BUDGET_MS', 90), patch.object(monitor.HTTP, 'open', return_value=response):
            with self.assertRaisesRegex(TimeoutError, 'HTTP probe deadline exceeded'):
                monitor.read_json(ORIGIN, '/health', clock)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertGreaterEqual(len(response.reads), 2)
        self.assertEqual(clock.remaining, 40000)  # Timer, not simulated budget exhaustion.
        self.assertTrue(response.closed)
        self.assertEqual(signal.getsignal(signal.SIGALRM), prior_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_whole_request_deadline_also_bounds_connection_or_headers(self):
        def stalled_open(*args, **kwargs):
            time.sleep(1)
            raise AssertionError('Request was not interrupted')

        with patch.object(monitor, 'HTTP_TOTAL_BUDGET_MS', 60), patch.object(monitor.HTTP, 'open', side_effect=stalled_open):
            with self.assertRaisesRegex(TimeoutError, 'HTTP probe deadline exceeded'):
                monitor.read_json(ORIGIN, '/health', Clock())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_real_opener_rejects_redirects_without_a_second_request(self):
        class SyntheticHTTPS(monitor.urllib.request.HTTPSHandler):
            def __init__(self, status, destination):
                super().__init__()
                self.status, self.destination = status, destination
                self.calls, self.responses = [], []

            def https_open(self, request):
                self.calls.append(request.full_url)
                response = urllib.response.addinfourl(io.BytesIO(b''), {'Location': self.destination},
                                                      request.full_url, self.status)
                response.msg = 'Synthetic redirect'
                self.responses.append(response)
                return response

        for status in [301, 302, 303, 307, 308]:
            for target in [ORIGIN + '/redirected', 'https://other.example/', 'http://other.example/']:
                with self.subTest(status=status, target=target):
                    transport = SyntheticHTTPS(status, target)
                    opener = monitor.urllib.request.build_opener(monitor.NoRedirect(), transport)
                    with patch.object(monitor, 'HTTP', opener):
                        with self.assertRaisesRegex(ValueError, 'must not redirect'):
                            monitor.read_json(ORIGIN, '/health', Clock())
                    self.assertEqual(transport.calls, [ORIGIN + '/health'])
                    self.assertTrue(all(response.closed for response in transport.responses))

    def test_non_https_origins_and_unavailable_deadline_fail_before_http(self):
        with patch.object(monitor.HTTP, 'open') as opener:
            for origin in ['http://zavliq.example', 'https://user:password@zavliq.example',
                           ORIGIN + '/path', ORIGIN + '?query', ORIGIN + '#fragment']:
                with self.subTest(origin=origin):
                    with self.assertRaises(ValueError):
                        monitor.read_json(origin, '/health', Clock())
            with patch.object(monitor.signal, 'getitimer', return_value=(1.0, 0.0)):
                with self.assertRaisesRegex(RuntimeError, 'deadline unavailable'):
                    monitor.read_json(ORIGIN, '/health', Clock())
            with patch.object(monitor.threading, 'current_thread', return_value=object()):
                with self.assertRaisesRegex(RuntimeError, 'deadline unavailable'):
                    monitor.read_json(ORIGIN, '/health', Clock())
            opener.assert_not_called()

    def test_backup_proof_uses_actual_archive_names_and_ignores_old_reuploads_or_future_dates(self):
        self.assertTrue(monitor.backup_fresh({'Contents': [backup(30)]}, NOW))
        for item in [backup(86400, LastModified='recent'), backup(-61), backup(30, Size=100),
                     {'Key': 'daily/arbitrary.tar.age', 'Size': 999},
                     {'Key': 'other/zavliq-20260912T120000Z.tar.age', 'Size': 999},
                     {'Key': 'daily/zavliq-20260912T120000Z.tar.age.sha256', 'Size': 999}]:
            with self.subTest(item=item):
                self.assertFalse(monitor.backup_fresh({'Contents': [item]}, NOW))
        malformed = {'Key': 'daily/zavliq-20269999T120000Z.tar.age', 'Size': 999}
        self.assertTrue(monitor.backup_fresh({'Contents': [malformed, backup(30)]}, NOW))

    def test_one_truncated_page_without_fresh_proof_fails_closed_without_more_aws_calls(self):
        rig = Rig()
        rig.objects = {'Contents': [backup(172800)], 'IsTruncated': True, 'NextContinuationToken': 'unused'}
        self.assertEqual(rig.run()['BackupFresh'], 0)
        self.assertEqual(len(rig.list_calls), 1)
        self.assertEqual(len(rig.metric_calls), 1)
        # One actual fresh archive is sufficient proof; omitted objects cannot
        # invalidate that proof, and no arbitrary object can supply it.
        rig = Rig()
        rig.objects = {'Contents': [backup(60)], 'IsTruncated': True}
        self.assertEqual(rig.run()['BackupFresh'], 1)

    def test_stale_or_future_host_report_does_not_pass_and_expiry_uses_check_time(self):
        for checked in [NOW - 300, NOW + 61, 'timestamp', True]:
            with self.subTest(checked=checked):
                self.assertFalse(monitor.host_healthy({'ok': True, 'checked_at': checked}, NOW))
        rig = Rig()
        rig.objects = {'Contents': [backup(86399)]}
        rig.list_ms = 2000
        self.assertEqual(rig.run()['BackupFresh'], 0)

    def test_cloudwatch_failure_is_not_success_or_retried_and_omits_raw_error_details(self):
        rig = Rig()
        rig.metric_error = RuntimeError('secret endpoint response detail')
        with self.assertRaisesRegex(RuntimeError, '^METRIC_EMISSION_FAILED$'):
            rig.run()
        self.assertEqual(len(rig.metric_calls), 1)
        self.assertNotIn('secret', rig.logs.getvalue())


if __name__ == '__main__':
    unittest.main()
