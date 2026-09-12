import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from run import ReceiverStopped, check_notification, check_receivers, evaluate, metric, target_config


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


class ReceiverValidation(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_notification_aborts_but_temporary_disconnect_waits(self):
        check_notification(7, {'method': 'connection_state', 'params': {'connected': False}})
        async def closed():
            check_notification(7, {'method': 'connection_state', 'params': {'connected': False, 'closed': True}})
        task = asyncio.create_task(closed())
        await asyncio.sleep(0)
        with self.assertRaises(ReceiverStopped) as failure:
            check_receivers([task])
        self.assertEqual(failure.exception.receiver_index, 7)
        self.assertEqual(failure.exception.reason, 'RUNTIME_CLOSED')

    async def test_unexpected_receiver_failure_aborts_with_sanitized_metadata(self):
        async def broken():
            raise KeyError('private payload must not reach evidence')
        task = asyncio.create_task(broken())
        await asyncio.sleep(0)
        with self.assertRaises(ReceiverStopped) as failure:
            check_receivers([task])
        self.assertEqual(failure.exception.receiver_index, 0)
        self.assertEqual(failure.exception.reason, 'KeyError')
        self.assertNotIn('private payload', str(failure.exception))

    async def test_running_receiver_is_allowed_and_cancelled_one_is_not(self):
        task = asyncio.create_task(asyncio.sleep(10))
        check_receivers([task])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(ReceiverStopped):
            check_receivers([task])


if __name__ == '__main__':
    unittest.main()
