import tempfile
import unittest
from pathlib import Path

from harness import Budget, MAX_REQUEST_BYTES, Scope, local_origin, model_visible, model_message, summarize, validate_schema


class HarnessBounds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.budget = Budget(Path(self.temp.name) / 'ledger.sqlite3')
        self.scope = Scope('arden', '@willow:localhost')

    def tearDown(self):
        self.budget.db.close()
        self.temp.cleanup()

    def test_budget_reserves_unknown_outcome_and_refuses_over_cap(self):
        for _ in range(54):
            self.budget.reserve('synthetic', 'amazon.nova-micro-v1:0', MAX_REQUEST_BYTES, 768)
        with self.assertRaisesRegex(RuntimeError, 'BUDGET_LIMIT'):
            self.budget.reserve('synthetic', 'amazon.nova-micro-v1:0', MAX_REQUEST_BYTES, 768)
        self.assertLessEqual(self.budget.total(), 2)
        self.assertEqual(self.budget.summary()['unknown_outcome_reservations'], 54)

    def test_budget_survives_reopen_and_settles_known_usage(self):
        token = self.budget.reserve('synthetic', 'amazon.nova-micro-v1:0', 100, 8)
        self.budget.settle(token, {'inputTokens': 7, 'outputTokens': 2})
        reopened = Budget(Path(self.temp.name) / 'ledger.sqlite3')
        self.assertAlmostEqual(reopened.total(), .001009)
        reopened.db.close()

    def test_budget_rejects_unbounded_request_and_freezes_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, 'REQUEST_LIMIT'):
            self.budget.reserve('x', 'amazon.nova-micro-v1:0', MAX_REQUEST_BYTES + 1, 8)
        token = self.budget.reserve('x', 'amazon.nova-micro-v1:0', 1, 8)
        with self.assertRaisesRegex(RuntimeError, 'TOKEN_ACCOUNTING_MISMATCH'):
            self.budget.settle(token, {'inputTokens': 9000, 'outputTokens': 8})
        with self.assertRaisesRegex(RuntimeError, 'TOKEN_ACCOUNTING_MISMATCH'):
            self.budget.reserve('x', 'amazon.nova-micro-v1:0', 1, 8)

    def test_only_loopback_origin_allowed(self):
        self.assertEqual(local_origin('http://localhost:8080/'), 'http://localhost:8080')
        for origin in ('https://example.com', 'http://localhost.example.com', 'http://localhost@evil.com', 'http://localhost:8080/path'):
            with self.assertRaises(ValueError):
                local_origin(origin)

    def test_scope_blocks_foreign_identity_room_peer_and_undocumented_params(self):
        for name, params in [
            ('zavliq_init', {'handle': 'someone-else'}),
            ('zavliq_create', {'kind': 'dm', 'members': ['@stranger:localhost']}),
            ('zavliq_thread', {'room_id': '!foreign:localhost'}),
            ('zavliq_send', {'room_id': '!known:localhost', 'idempotency_key': 'x', 'url': 'https://example.com'}),
            ('shell', {'command': 'anything'}),
            ('zavliq_transfer', {'operation': 'download', 'path': '/tmp/file'}),
        ]:
            with self.assertRaises(ValueError):
                self.scope.check(name, params)
        self.scope.observe({'room_id': '!known:localhost', 'items': [{'event_id': '$seen'}]})
        self.scope.check('zavliq_send', {'room_id': '!known:localhost', 'reply_to': '$seen', 'text': 'hello', 'idempotency_key': 'x'})

    def test_credentials_fail_closed_and_paths_hidden(self):
        with self.assertRaisesRegex(RuntimeError, 'SECRET_EXPOSURE'):
            model_visible({'result': [{'access_token': 'fixture-only'}]})
        self.assertEqual(model_visible({'data_dir': '/private/path'}), {'data_dir': '<private_identity_directory>'})

    def test_meta_adapter_only_accepts_complete_function_json(self):
        model = 'us.meta.llama3-3-70b-instruct-v1:0'
        message = {'role': 'assistant', 'content': [{'text': '{"type":"function","name":"zavliq_init","parameters":{"handle":"arden"}}'}]}
        adapted, changed = model_message(model, message)
        self.assertTrue(changed)
        self.assertEqual(adapted['content'][0]['toolUse']['input'], {'handle': 'arden'})
        for text in ('Run zavliq_init now.', '```json\n{"type":"function","name":"zavliq_init","parameters":{}}\n```', '{"type":"function","name":"zavliq_init","parameters":{},"extra":true}'):
            self.assertFalse(model_message(model, {'content': [{'text': text}]})[1])
        self.assertFalse(model_message('amazon.nova-micro-v1:0', message)[1])

    def test_published_schema_required_types_refs_and_limits(self):
        schema = {'type': 'object', 'required': ['room'], 'additionalProperties': False, 'properties': {'room': {'type': 'string', 'minLength': 1}, 'reply': {'$ref': '#/properties/room'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}}
        validate_schema({'room': '!known', 'reply': '$seen', 'limit': 10}, schema)
        for value in ({}, {'room': ''}, {'room': 5}, {'room': '!known', 'reply': False}, {'room': '!known', 'limit': True}, {'room': '!known', 'limit': 101}, {'room': '!known', 'extra': 1}):
            with self.assertRaises(ValueError):
                validate_schema(value, schema)

    def test_gate_requires_ten_trials_two_successful_providers_and_interfaces(self):
        results = [{'within_five_minutes': True, 'provider': 'Amazon', 'transport': 'sdk'} for _ in range(10)]
        self.assertFalse(summarize(results, 'x', self.budget)['gate_passed'])
        results[-1] = {'within_five_minutes': True, 'provider': 'Meta', 'transport': 'mcp'}
        self.assertTrue(summarize(results, 'x', self.budget)['gate_passed'])
        results[-1]['within_five_minutes'] = False
        self.assertFalse(summarize(results, 'x', self.budget)['gate_passed'])
        self.assertFalse(summarize(results[:9], 'x', self.budget)['gate_passed'])


if __name__ == '__main__':
    unittest.main()
