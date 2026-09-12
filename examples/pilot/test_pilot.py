"""Offline replay/correlation checks. No installed SDK, native processes or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pilot

FINDING = {'summary': 'Private finding text', 'evidence': ['https://example.invalid/source'],
           'question': 'Does the cited evidence support this finding?'}


def state(role='researcher'):
    value = pilot.initial('pair-one', role)
    value['config'] = {'peer': '@reviewer:zavliq.com' if role == 'researcher' else '@researcher:zavliq.com',
                       'room_id': '!pilot:zavliq.com'}
    return value


def event(value, exchange='day-one', event_id='$received', *, role='researcher', reply=None):
    payload = {'schema': pilot.SCHEMA, 'pilot_id': 'pair-one', 'exchange_id': exchange,
               'kind': 'verdict' if role == 'researcher' else 'finding', **value}
    return {'event_id': event_id, 'room_id': '!pilot:zavliq.com',
        'sender': '@reviewer:zavliq.com' if role == 'researcher' else '@researcher:zavliq.com',
        'content': {'com.zavliq.data_json': json.dumps(payload),
                    **({'m.relates_to': {'m.in_reply_to': {'event_id': reply}}} if reply else {})}}


class Client:
    def __init__(self):
        self.calls, self.transactions, self.pages = [], {}, {}
        self.lose_response = False
        self.lose_ack = False

    async def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'send':
            key = params['idempotency_key']
            if key not in self.transactions:
                self.transactions[key] = {'event_id': '$sent' + str(len(self.transactions)), 'params': copy.deepcopy(params)}
            assert self.transactions[key]['params'] == params
            if self.lose_response:
                self.lose_response = False
                raise ValueError('ACCEPTED_RESPONSE_LOST')
            return {'event_id': self.transactions[key]['event_id']}
        if method == 'acknowledge':
            if self.lose_ack:
                self.lose_ack = False
                raise ValueError('ACK_RESPONSE_LOST')
            return {'status': 'read'}
        if method == 'inbox': return self.pages[params['cursor']]
        raise AssertionError('Unexpected or unauthorized method ' + method)


class PilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_send_response_loss_reopens_same_private_intent(self):
        client = Client(); client.lose_response = True
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve() / 'pilot'
            with pilot.journal(folder, create=True) as (_, save):
                value = state()
                key = pilot.intent(value, 'day-one', 'finding', {'finding': FINDING})
                with self.assertRaisesRegex(ValueError, 'ACCEPTED_RESPONSE_LOST'):
                    await pilot.send_intent(client, value, key, save)
            with pilot.journal(folder) as (reopened, save):
                self.assertIsNone(reopened['outgoing'][key]['event_id'])
                result = await pilot.send_intent(client, reopened, key, save)
                self.assertEqual(result['event_id'], '$sent0')
                self.assertEqual(len(client.transactions), 1)
                self.assertEqual(pilot.metrics(reopened)['accepted_finding_count'], 1)
                with self.assertRaisesRegex(ValueError, 'DIFFERENT_CONTENT'):
                    pilot.intent(reopened, 'day-one', 'finding', {'finding': FINDING | {'summary': 'changed'}})
            self.assertEqual((folder / 'state.json').stat().st_mode & 0o777, 0o600)

    async def test_ack_retry_does_not_create_another_review(self):
        client = Client(); client.lose_ack = True; value = state('reviewer')
        key = pilot.intent(value, 'day-one', 'verdict', {'finding_event_id': '$finding', 'decision': 'approve', 'note': 'Explicit operator choice'})
        snapshots = []
        def save(state): snapshots.append(copy.deepcopy(state))
        with self.assertRaisesRegex(ValueError, 'ACK_RESPONSE_LOST'):
            await pilot.send_intent(client, value, key, save)
        reopened = snapshots[-1]
        result = await pilot.send_intent(client, reopened, key, save)
        self.assertTrue(result['read_acknowledged'])
        self.assertEqual(len(client.transactions), 1)
        self.assertEqual(sum(method == 'send' for method, _ in client.calls), 1)
        sent = client.calls[0][1]
        self.assertEqual(sent['room_id'], '!pilot:zavliq.com')
        self.assertEqual(sent['reply_to'], '$finding')
        self.assertEqual(pilot.metrics(reopened)['accepted_review_count'], 1)

    async def test_sender_room_relation_and_original_event_must_all_match(self):
        value = state(); key = pilot.intent(value, 'day-one', 'finding', {'finding': FINDING})
        value['outgoing'][key]['event_id'] = '$finding'
        valid = event({'finding_event_id': '$finding', 'decision': 'approve', 'note': ''}, reply='$finding')
        bad = [valid | {'sender': '@other:zavliq.com'}, valid | {'room_id': '!other:zavliq.com'},
               event({'finding_event_id': '$other', 'decision': 'approve', 'note': ''}, reply='$finding'),
               event({'finding_event_id': '$finding', 'decision': 'approve', 'note': ''}, reply='$other'),
               event({'finding_event_id': '$finding', 'decision': 'approve', 'note': ''}, exchange='unrequested', reply='$finding')]
        for row in bad: pilot.ingest(value, row)
        self.assertEqual(value['incoming'], {})
        pilot.ingest(value, valid); pilot.ingest(value, valid)
        self.assertEqual(pilot.metrics(value)['correlated_round_trip_count'], 1)
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_OR_CHANGED'):
            pilot.ingest(value, valid | {'event_id': '$duplicate'})

    async def test_pages_commit_correlations_and_cursor_together_and_resume(self):
        value = state('reviewer'); client = Client()
        one = event({'finding': FINDING}, role='reviewer', event_id='$one')
        two = event({'finding': FINDING}, role='reviewer', event_id='$two', exchange='day-two')
        client.pages = {0: {'items': [one], 'next_cursor': 1, 'has_more': True, 'history_gap_rooms': []},
                        1: {'items': [two], 'next_cursor': 2, 'has_more': False, 'history_gap_rooms': []}}
        saved = []
        def interrupt_second(state):
            if state['cursor'] == 2: raise ValueError('DISK_WRITE_INTERRUPTED')
            saved.append(copy.deepcopy(state))
        with self.assertRaisesRegex(ValueError, 'DISK_WRITE_INTERRUPTED'):
            await pilot.poll(client, value, interrupt_second)
        self.assertEqual(value['cursor'], 1)
        self.assertEqual(set(saved[-1]['incoming']), {'day-one'})
        result = await pilot.poll(client, saved[-1], lambda value: None)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['next_cursor'], 2)
        self.assertTrue(all(method == 'inbox' for method, _ in client.calls))

    async def test_history_gaps_and_pending_sends_do_not_advance_cursor(self):
        value = state('reviewer'); client = Client()
        client.pages[0] = {'items': [], 'next_cursor': 7, 'has_more': False, 'history_gap_rooms': ['!pilot:zavliq.com']}
        with self.assertRaisesRegex(ValueError, 'HISTORY_INCOMPLETE'):
            await pilot.poll(client, value, lambda _: self.fail('must not save a cursor across a gap'))
        self.assertEqual(value['cursor'], 0)
        pilot.intent(value, 'day-one', 'verdict', {'finding_event_id': '$one', 'decision': 'approve', 'note': ''})
        with self.assertRaisesRegex(ValueError, 'PENDING_SEND'):
            await pilot.poll(client, value, lambda _: None)

    async def test_metrics_separate_retention_daily_cadence_and_operator_usefulness(self):
        value = state()
        for exchange, at in [('day-one', '2026-01-01T10:00:00+00:00'), ('day-eight', '2026-01-08T10:00:00+00:00')]:
            with patch.object(pilot, 'now', return_value=at):
                pilot.milestone(value, 'finding_accepted', exchange, '$finding-' + exchange)
                pilot.milestone(value, 'review_received', exchange, '$review-' + exchange)
        value['incoming']['day-one'] = {'payload': {'finding': FINDING, 'credential-shaped': 'never-export-this'}}
        report = pilot.metrics(value)
        self.assertTrue(report['day7_repeat_use'])
        self.assertEqual(len(report['active_utc_dates']), 2)
        self.assertFalse(report['daily_cadence_target_met'])
        self.assertEqual(report['useful_completed_runs'], 0)
        self.assertNotIn('Private finding text', json.dumps(report))
        self.assertNotIn('never-export-this', json.dumps(report))
        for number in range(2, 7):
            exchange = 'day-' + str(number)
            with patch.object(pilot, 'now', return_value=f'2026-01-{number:02}T10:00:00+00:00'):
                pilot.milestone(value, 'review_received', exchange, '$review-' + exchange)
        self.assertFalse(pilot.metrics(value)['daily_cadence_target_met'])
        value['assessments'] = {item['exchange_id']: True for item in value['evidence'] if item['event'] == 'review_received'}
        report = pilot.metrics(value)
        self.assertFalse(report['daily_cadence_target_met'])  # Seven useful dates, but day seven is missing.
        self.assertEqual(report['longest_useful_daily_streak'], 6)
        with patch.object(pilot, 'now', return_value='2026-01-07T10:00:00+00:00'):
            pilot.milestone(value, 'review_received', 'day-seven', '$review-day-seven')
        value['assessments']['day-seven'] = True
        report = pilot.metrics(value)
        self.assertTrue(report['daily_cadence_target_met'])
        self.assertEqual(report['longest_useful_daily_streak'], 8)
        self.assertEqual(report['reviewer_verdict_returned_count'], 0)
        self.assertEqual(report['researcher_verdict_received_count'], 8)
        self.assertFalse(report['pilot_completion_claimed'])
        self.assertFalse(report['outside_operator_ownership_verified'])

    async def test_seven_weekly_runs_do_not_satisfy_daily_cadence(self):
        value = state()
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for number in range(7):
            exchange = 'week-' + str(number)
            with patch.object(pilot, 'now', return_value=(start + timedelta(days=7 * number)).isoformat()):
                pilot.milestone(value, 'review_received', exchange, '$review-' + exchange)
            value['assessments'][exchange] = True
        report = pilot.metrics(value)
        self.assertTrue(report['day7_repeat_use'])
        self.assertEqual(report['useful_completed_runs'], 7)
        self.assertEqual(len(report['active_utc_dates']), 7)
        self.assertEqual(report['longest_useful_daily_streak'], 1)
        self.assertFalse(report['daily_cadence_target_met'])


if __name__ == '__main__': unittest.main()
