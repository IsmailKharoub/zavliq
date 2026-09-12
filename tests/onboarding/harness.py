#!/usr/bin/env python3
"""Model-driven local onboarding. No model-selected shell, URL, identity or file access."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/client-python/src'))
from zavliq import Zavliq, ZavliqError

PRIVATE = ROOT / 'tests/onboarding/.local'
EVIDENCE = ROOT / 'tests/onboarding/evidence'
MODELS = {
    'amazon.nova-micro-v1:0': {'provider': 'Amazon', 'input_per_million': .035, 'output_per_million': .14},
    'us.meta.llama3-3-70b-instruct-v1:0': {'provider': 'Meta', 'input_per_million': .72, 'output_per_million': .72},
}
# $1/M for both directions exceeds each selected model's verified standard price.
# Cache discounts are ignored. A reservation also includes 4,096 tokens overhead.
CEILING_PER_TOKEN = 0.000001
TOTAL_LIMIT = 2.00
MAX_TURNS = 20
MAX_SECONDS = 300
MAX_REQUEST_BYTES = 32_000
MAX_OUTPUT_TOKENS = 768
MAX_TOOL_RESULT_BYTES = 8_000
ALLOWED = {
    'zavliq_init': 'init', 'zavliq_identity': 'identity', 'zavliq_send': 'send',
    'zavliq_inbox': 'inbox', 'zavliq_wait': 'wait', 'zavliq_thread': 'thread',
    'zavliq_conversations': None, 'zavliq_create': 'create_conversation',
    'zavliq_membership': None, 'zavliq_receipt': None, 'zavliq_flush': 'flush',
}
PARAMETERS = {
    'zavliq_init': {'handle', 'display_name'}, 'zavliq_identity': set(),
    'zavliq_send': {'room_id', 'text', 'data', 'reply_to', 'thread_root', 'idempotency_key'},
    'zavliq_inbox': {'cursor', 'limit', 'room_id', 'include_sent', 'full'}, 'zavliq_wait': {'cursor', 'timeout_seconds', 'limit', 'room_id', 'include_sent'},
    'zavliq_thread': {'room_id', 'cursor', 'limit'}, 'zavliq_conversations': {'operation'},
    'zavliq_create': {'kind', 'members', 'name', 'encryption'},
    'zavliq_membership': {'operation', 'room_id', 'user_id'},
    'zavliq_receipt': {'operation', 'event_id', 'status'}, 'zavliq_flush': set(),
}
SECRET_KEYS = {'access_token', 'registration_secret', 'store_passphrase', 'private_key', 'password', 'refresh_token'}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def local_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Trials require an explicit loopback HTTP origin.')
    return value.rstrip('/')


def model_visible(value):
    """Fail the trial on a credential-bearing result; hide private local paths."""
    if isinstance(value, dict):
        if SECRET_KEYS.intersection(value):
            raise RuntimeError('SECRET_EXPOSURE: runtime returned a forbidden credential field')
        return {k: '<private_identity_directory>' if k == 'data_dir' else model_visible(v) for k, v in value.items()}
    if isinstance(value, list):
        return [model_visible(v) for v in value]
    return value


class Budget:
    """One persistent ledger across all runs, including unknown-outcome calls."""
    def __init__(self, path: Path = PRIVATE / 'budget.sqlite3'):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, trial TEXT, model TEXT, reserved REAL, charged REAL, input_tokens INTEGER, output_tokens INTEGER, state TEXT)')
        # The separately authorized initial permission smoke was 7 input / 2 output.
        # Charge a full conservative millidollar instead of its sub-micro cost.
        self.db.execute("INSERT OR IGNORE INTO calls VALUES('initial-nova-access-smoke','permission','amazon.nova-micro-v1:0',0.001,0.001,7,2,'settled')")
        self.db.commit()

    def total(self) -> float:
        return self.db.execute('SELECT COALESCE(SUM(charged),0) FROM calls').fetchone()[0]

    def reserve(self, trial: str, model: str, request_bytes: int, output_tokens: int) -> str:
        if model not in MODELS or not 1 <= request_bytes <= MAX_REQUEST_BYTES or not 1 <= output_tokens <= MAX_OUTPUT_TOKENS:
            raise RuntimeError('REQUEST_LIMIT: model or context exceeds this harness allowlist')
        amount = (request_bytes + 4096 + output_tokens) * CEILING_PER_TOKEN
        identifier = secrets.token_hex(12)
        self.db.execute('BEGIN IMMEDIATE')
        if self.db.execute("SELECT 1 FROM calls WHERE state='accounting_mismatch' LIMIT 1").fetchone():
            self.db.rollback()
            raise RuntimeError('TOKEN_ACCOUNTING_MISMATCH: spending is frozen for operator review')
        if self.total() + amount > TOTAL_LIMIT:
            self.db.rollback()
            raise RuntimeError('BUDGET_LIMIT: no model call made; cumulative $2 ceiling reached')
        self.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,NULL,NULL,?)', (identifier, trial, model, amount, amount, 'reserved'))
        self.db.commit()
        return identifier

    def settle(self, identifier: str, usage: dict) -> float:
        tokens = int(usage['inputTokens']) + int(usage['outputTokens'])
        charged = tokens * CEILING_PER_TOKEN
        reserved = self.db.execute('SELECT reserved FROM calls WHERE id=?', (identifier,)).fetchone()[0]
        if charged > reserved:
            self.db.execute("UPDATE calls SET state='accounting_mismatch' WHERE id=?", (identifier,))
            self.db.commit()
            raise RuntimeError('TOKEN_ACCOUNTING_MISMATCH: stop all further calls for review')
        self.db.execute('UPDATE calls SET charged=?,input_tokens=?,output_tokens=?,state=? WHERE id=?', (charged, usage['inputTokens'], usage['outputTokens'], 'settled', identifier))
        self.db.commit()
        return charged

    def summary(self) -> dict:
        count, unknown = self.db.execute("SELECT count(*),SUM(CASE WHEN state='reserved' THEN 1 ELSE 0 END) FROM calls").fetchone()
        return {'ceiling_usd': TOTAL_LIMIT, 'conservative_charged_usd': round(self.total(), 6), 'calls_including_permission_smoke': count, 'unknown_outcome_reservations': unknown or 0}


async def converse(model: str, payload: dict, budget: Budget, trial: str, timeout: float) -> dict:
    encoded = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode()
    reservation = budget.reserve(trial, model, len(encoded), payload['inferenceConfig']['maxTokens'])
    with tempfile.TemporaryDirectory(prefix='request-', dir=PRIVATE) as temporary:
        path = Path(temporary) / 'request.json'
        path.write_bytes(encoded)
        process = await asyncio.create_subprocess_exec(
            'aws', 'bedrock-runtime', 'converse', '--region', 'us-east-1',
            '--cli-input-json', 'file://' + str(path), '--output', 'json',
            '--cli-connect-timeout', '10', '--cli-read-timeout', str(max(1, int(timeout))),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': ''},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 2)
        except asyncio.TimeoutError:
            process.kill(); await process.wait()
            # Keep the reservation charged: the remote outcome could be unknown.
            raise RuntimeError('MODEL_TIMEOUT: reservation retained; no automatic retry')
        if process.returncode:
            error = re.search(r'\(([A-Za-z0-9_]+)\)', stderr.decode(errors='replace'))
            raise RuntimeError('BEDROCK_' + (error.group(1) if error else 'REQUEST_FAILED'))
        if len(stdout) > 100_000:
            raise RuntimeError('MODEL_RESPONSE_LIMIT')
        response = json.loads(stdout)
        budget.settle(reservation, response['usage'])
        return response


class McpConnection:
    def __init__(self, directory: Path, origin: str, binary: str):
        self.directory, self.origin, self.binary = directory, origin, binary
        self.process = None
        self.sequence = 0

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            'node', str(ROOT / 'packages/mcp/src/index.mjs'),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, 'ZAVLIQ_BINARY': self.binary, 'ZAVLIQ_DATA_DIR': str(self.directory), 'ZAVLIQ_CONTROL_URL': self.origin},
        )
        await self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'zavliq-onboarding-trial', 'version': '0.1.0'}})
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await self.process.stdin.drain()

    async def rpc(self, method: str, params: dict):
        self.sequence += 1
        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': self.sequence, 'method': method, 'params': params}) + '\n').encode())
        await self.process.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=40)
            if not line:
                raise RuntimeError('MCP_EXITED')
            message = json.loads(line)
            if message.get('id') == self.sequence:
                if 'error' in message:
                    raise RuntimeError('MCP_PROTOCOL_ERROR')
                return message['result']

    async def tools(self):
        return (await self.rpc('tools/list', {}))['tools']

    async def call(self, name: str, params: dict):
        response = await self.rpc('tools/call', {'name': name, 'arguments': params})
        parts = [json.loads(c['text']) for c in response.get('content', []) if c.get('type') == 'text']
        return parts[0] if len(parts) == 1 else {'content': parts}

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()


def bedrock_tools(tools: list[dict]) -> list[dict]:
    return [{'toolSpec': {'name': tool['name'], 'description': tool['description'], 'inputSchema': {'json': {key: value for key, value in tool['inputSchema'].items() if key in ('type', 'properties', 'required')}}}} for tool in tools if tool['name'] in ALLOWED]


def model_message(model: str, message: dict) -> tuple[dict, bool]:
    """Normalize Meta's complete JSON function envelope; never infer prose/code."""
    content = message.get('content', [])
    if MODELS[model]['provider'] != 'Meta' or len(content) != 1 or set(content[0]) != {'text'}:
        return message, False
    try:
        call = json.loads(content[0]['text'])
    except (ValueError, TypeError):
        return message, False
    if not isinstance(call, dict) or set(call) != {'type', 'name', 'parameters'} or call['type'] != 'function' or not isinstance(call['name'], str) or not isinstance(call['parameters'], dict):
        return message, False
    return {'role': 'assistant', 'content': [{'toolUse': {'toolUseId': 'meta-' + secrets.token_hex(12), 'name': call['name'], 'input': call['parameters']}}]}, True


def validate_schema(value, schema: dict, root: dict | None = None, depth: int = 0):
    """Validate the finite JSON Schema subset emitted by the published MCP tools."""
    root = schema if root is None else root
    if depth > 16:
        raise ValueError('TOOL_SCHEMA: excessive nesting')
    supported = {'$schema', '$ref', 'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'minimum', 'maximum', 'minLength', 'description'}
    if set(schema) - supported:
        raise RuntimeError('UNSUPPORTED_TOOL_SCHEMA: review new schema keywords before trials')
    if '$ref' in schema:
        reference = schema['$ref']
        if not reference.startswith('#/'):
            raise ValueError('TOOL_SCHEMA: only local schema references are supported')
        target = root
        for key in reference[2:].split('/'):
            target = target[key.replace('~1', '/').replace('~0', '~')]
        return validate_schema(value, target, root, depth + 1)
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    kind = schema.get('type')
    if kind and (kind not in types or type(value) is not types[kind]):
        raise ValueError('TOOL_SCHEMA: parameter has the wrong type')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('TOOL_SCHEMA: parameter is outside the documented enum')
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - set(value):
            raise ValueError('TOOL_SCHEMA: a required parameter is missing')
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError('TOOL_SCHEMA: undocumented parameter')
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], root, depth + 1)
    if kind == 'array':
        for item in value:
            validate_schema(item, schema.get('items', {}), root, depth + 1)
    if kind == 'string' and len(value) < schema.get('minLength', 0):
        raise ValueError('TOOL_SCHEMA: empty string is not allowed')
    if kind == 'integer' and not schema.get('minimum', -float('inf')) <= value <= schema.get('maximum', float('inf')):
        raise ValueError('TOOL_SCHEMA: integer is outside the documented bounds')


class Scope:
    def __init__(self, handle: str, peer: str):
        self.handle, self.peer = handle, peer
        self.rooms: set[str] = set()
        self.events: set[str] = set()

    def check(self, name: str, params: dict):
        if name not in ALLOWED or not isinstance(params, dict):
            raise ValueError('TRIAL_SCOPE: only provided Zavliq tools are available')
        if set(params) - PARAMETERS[name]:
            raise ValueError('TRIAL_SCOPE: only documented tool parameters are accepted')
        if len(json.dumps(params)) > 4000:
            raise ValueError('TRIAL_SCOPE: synthetic parameters must stay below 4 KiB')
        if name == 'zavliq_init' and params.get('handle') != self.handle:
            raise ValueError('TRIAL_SCOPE: use only the assigned fresh fixture handle')
        if name == 'zavliq_create':
            if params.get('kind') != 'dm' or params.get('encryption', 'standard') != 'standard' or params.get('members') != [self.peer] or len(self.rooms) >= 3:
                raise ValueError('TRIAL_SCOPE: this trial permits standard DMs only with the assigned fixture peer')
        if 'room_id' in params and params['room_id'] not in self.rooms:
            raise ValueError('TRIAL_SCOPE: only rooms created in this trial may be accessed')
        if 'user_id' in params and params['user_id'] != self.peer:
            raise ValueError('TRIAL_SCOPE: only the assigned fixture peer may be contacted')
        for key in ('event_id', 'reply_to', 'thread_root'):
            if key in params and params[key] not in self.events:
                raise ValueError('TRIAL_SCOPE: use event IDs observed in this trial')
        if name == 'zavliq_membership' and params.get('operation') not in ('accept', 'reject', 'leave'):
            raise ValueError('TRIAL_SCOPE: membership expansion is outside this onboarding trial')
        if name == 'zavliq_conversations' and params.get('operation') not in ('requests', 'conversations'):
            raise ValueError('TRIAL_SCOPE: invalid conversation operation')
        if name == 'zavliq_receipt' and params.get('operation') not in ('delivery', 'acknowledge'):
            raise ValueError('TRIAL_SCOPE: invalid receipt operation')

    def observe(self, result: dict):
        if isinstance(result.get('room_id'), str):
            self.rooms.add(result['room_id'])
        if isinstance(result.get('event_id'), str):
            self.events.add(result['event_id'])
        for item in result.get('items', []):
            if isinstance(item, dict) and isinstance(item.get('event_id'), str):
                self.events.add(item['event_id'])


async def peer_tick(peer: Zavliq, rooms: set[str], user_id: str | None, challenge: str, receipt: str, state: dict):
    if not user_id:
        return
    requests = await peer.call('requests')
    for request in requests.get('items', []):
        if request['room_id'] in rooms and request.get('inviter') == user_id:
            await peer.call('accept', {'room_id': request['room_id']})
    inbox = await peer.call('inbox', {'full': True, 'limit': 100})
    for event in inbox['items']:
        if event['room_id'] not in rooms or event['sender'] != user_id:
            continue
        content = event.get('content', {})
        if challenge not in json.dumps(content) or event['event_id'] in state['replied']:
            continue
        response = await peer.send(event['room_id'], text=f'Fixture received {challenge}. Receipt code: {receipt}', idempotency_key='echo-' + hashlib.sha256(event['event_id'].encode()).hexdigest()[:24])
        state.update(received_event=event['event_id'], reply_event=response['event_id'])
        state['replied'].add(event['event_id'])


async def trial(index: int, model: str, transport: str, origin: str, binary: str, schemas: list, budget: Budget, run_id: str) -> dict:
    identifier = f'{run_id}-{index:02d}'
    directory = PRIVATE / identifier
    directory.mkdir(mode=0o700)
    handle = ('arden', 'mira', 'cedar', 'linden', 'vale', 'rowan', 'iris', 'hazel', 'fern', 'sage')[index - 1] + '-' + identifier
    peer = Zavliq(binary=binary, data_dir=str(directory / 'peer'), control_url=origin, timeout=40)
    connector = None
    started = None
    transcript = []
    result = {'trial': identifier, 'provider': MODELS[model]['provider'], 'model': model, 'transport': transport, 'passed': False, 'turns': 0}
    before_cost = budget.total()
    schema_by_name = {item['toolSpec']['name']: item['toolSpec']['inputSchema']['json'] for item in schemas}
    try:
        peer_identity = await peer.init('willow-' + identifier)
        challenge, receipt = 'hello-' + secrets.token_hex(6), 'receipt-' + secrets.token_hex(8)
        scope = Scope(handle, peer_identity['user_id'])
        peer_state = {'replied': set()}
        user_id = None
        receipt_observed = False
        if transport == 'mcp':
            connector = McpConnection(directory / 'agent', origin, binary)
            await connector.start()
        else:
            connector = Zavliq(binary=binary, data_dir=str(directory / 'agent'), control_url=origin, timeout=40)
        skill = (ROOT / 'packages/skill/zavliq/SKILL.md').read_text()
        result['skill_sha256'] = hashlib.sha256(skill.encode()).hexdigest()
        task = (f'Use Zavliq to register your assigned fresh handle {handle}, establish a standard DM with the fixture peer {peer_identity["user_id"]}, '
                f'and send a synthetic greeting containing {challenge}. Receive its reply and report its receipt code in your final answer. '
                f'The configured local service is {origin}. This trial permits only the assigned handle, peer and its DM rooms. '
                'The provided tools are the available interface. Do not claim success without observing the reply. You have five minutes.')
        messages = [{'role': 'user', 'content': [{'text': task}]}]
        started = time.monotonic()
        for turn in range(1, MAX_TURNS + 1):
            remaining = MAX_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise RuntimeError('TRIAL_TIMEOUT')
            payload = {'modelId': model, 'system': [{'text': skill}], 'messages': messages, 'toolConfig': {'tools': schemas}, 'inferenceConfig': {'maxTokens': MAX_OUTPUT_TOKENS, 'temperature': 0}}
            response = await converse(model, payload, budget, identifier, min(40, remaining))
            usage = result.setdefault('usage', {'inputTokens': 0, 'outputTokens': 0})
            usage['inputTokens'] += response['usage']['inputTokens']
            usage['outputTokens'] += response['usage']['outputTokens']
            result['turns'] = turn
            raw_message = response['output']['message']
            message, adapted = model_message(model, raw_message)
            messages.append(message)
            transcript.append({'kind': 'model', 'turn': turn, 'message': message, 'raw_message': raw_message if adapted else None, 'format_adapter_used': adapted, 'usage': response['usage'], 'stop_reason': response.get('stopReason')})
            if adapted:
                result['json_function_envelopes_normalized'] = result.get('json_function_envelopes_normalized', 0) + 1
            calls = [part['toolUse'] for part in message.get('content', []) if 'toolUse' in part]
            if len(calls) > 4:
                raise RuntimeError('TOOL_CALL_LIMIT: at most four tool calls per model turn')
            if not calls:
                final = '\n'.join(part.get('text', '') for part in message.get('content', []))
                result['passed'] = bool(peer_state.get('received_event') and peer_state.get('reply_event') and receipt_observed and receipt in final)
                result['outcome'] = 'completed' if result['passed'] else 'ended_without_verified_exchange'
                break
            tool_results = []
            for call in calls:
                remaining = MAX_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise RuntimeError('TRIAL_TIMEOUT')
                name, params = call['name'], call['input']
                try:
                    scope.check(name, params)
                    validate_schema(params, schema_by_name[name])
                    if transport == 'mcp':
                        operation = connector.call(name, params)
                    else:
                        method = ALLOWED[name] or params['operation']
                        operation = connector.call(method, params)
                    output = await asyncio.wait_for(operation, timeout=min(40, remaining))
                    output = model_visible(output)
                    if len(json.dumps(output).encode()) > MAX_TOOL_RESULT_BYTES:
                        output = {'error': {'code': 'TRIAL_CONTEXT_LIMIT', 'message': 'Use smaller pages to fit this bounded trial.'}}
                    scope.observe(output)
                    if name == 'zavliq_init' and 'user_id' in output:
                        user_id = output['user_id']
                    if receipt in json.dumps(output):
                        receipt_observed = True
                    remaining = MAX_SECONDS - (time.monotonic() - started)
                    if remaining <= 0:
                        raise RuntimeError('TRIAL_TIMEOUT')
                    await asyncio.wait_for(peer_tick(peer, scope.rooms, user_id, challenge, receipt, peer_state), timeout=min(40, remaining))
                except (ValueError, ZavliqError, asyncio.TimeoutError) as error:
                    output = {'error': {'code': getattr(error, 'code', type(error).__name__), 'message': str(error)}}
                transcript.append({'kind': 'tool', 'turn': turn, 'name': name, 'input': params, 'result': output})
                tool_results.append({'toolResult': {'toolUseId': call['toolUseId'], 'content': [{'json': output}], 'status': 'error' if 'error' in output else 'success'}})
            messages.append({'role': 'user', 'content': tool_results})
        else:
            result['outcome'] = 'turn_limit'
        result['checks'] = {'registered': bool(user_id), 'peer_received_greeting': bool(peer_state.get('received_event')), 'peer_reply_stored': bool(peer_state.get('reply_event')), 'agent_observed_receipt': receipt_observed}
    except Exception as error:
        result['outcome'] = str(error).split(':')[0][:120]
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - started, 3) if started else None
        result['within_five_minutes'] = bool(result['passed'] and result['elapsed_seconds'] <= MAX_SECONDS)
        result['conservative_cost_usd'] = round(budget.total() - before_cost, 6)
        if connector:
            await connector.close()
        await peer.close()
        write_json(directory / 'transcript.json', transcript)
        write_json(EVIDENCE / (identifier + '.json'), result)
        print(json.dumps(result), flush=True)
    return result


async def main(args):
    os.umask(0o077)
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    budget = Budget()
    if args.command == 'budget':
        print(json.dumps(budget.summary())); return
    if args.command == 'smoke':
        response = await converse(args.model, {'modelId': args.model, 'messages': [{'role': 'user', 'content': [{'text': 'Reply with exactly the word ready.'}]}], 'inferenceConfig': {'maxTokens': 8, 'temperature': 0}}, budget, 'permission-smoke', 30)
        report = {'model': args.model, 'provider': MODELS[args.model]['provider'], 'usage': response['usage'], 'response': response['output']['message']['content'], 'budget': budget.summary()}
        write_json(EVIDENCE / ('permission-' + MODELS[args.model]['provider'].lower() + '.json'), report)
        print(json.dumps(report)); return
    origin = local_origin(args.origin)
    # Offline schema inspection launches the real local MCP server but no enrollment.
    schema_connection = McpConnection(PRIVATE / 'schema-only', origin, args.binary)
    await schema_connection.start()
    try:
        tools = await schema_connection.tools()
    finally:
        await schema_connection.close()
    schemas = bedrock_tools(tools)
    if args.command == 'inspect':
        print(json.dumps({'tool_names': [item['toolSpec']['name'] for item in schemas], 'skill_sha256': hashlib.sha256((ROOT / 'packages/skill/zavliq/SKILL.md').read_bytes()).hexdigest(), 'budget': budget.summary()})); return
    if not args.service_ready:
        raise SystemExit('Full trials require the release owner to mark the current local build ready (--service-ready).')
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%m%d%H%M%S') + secrets.token_hex(2)
    results = []
    for index in range(1, args.count + 1):
        model = list(MODELS)[(index - 1) % 2]
        transport = 'mcp' if (index - 1) % 4 in (0, 3) else 'sdk'
        results.append(await trial(index, model, transport, origin, args.binary, schemas, budget, run_id))
    summary = summarize(results, run_id, budget)
    write_json(EVIDENCE / (run_id + '-summary.json'), summary)
    print(json.dumps(summary), flush=True)


def summarize(results: list[dict], run_id: str, budget: Budget) -> dict:
    summary = {'run_id': run_id, 'trials': len(results), 'within_five_minutes': sum(r['within_five_minutes'] for r in results), 'providers_attempted': sorted({r['provider'] for r in results}), 'successful_providers': sorted({r['provider'] for r in results if r['within_five_minutes']}), 'successful_transports': sorted({r['transport'] for r in results if r['within_five_minutes']}), 'budget': budget.summary()}
    summary['gate_passed'] = summary['trials'] == 10 and summary['within_five_minutes'] >= 9 and len(summary['successful_providers']) == 2 and len(summary['successful_transports']) == 2
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'budget', 'smoke', 'run'])
    parser.add_argument('--origin', default='http://localhost:8080')
    parser.add_argument('--binary', default=str(ROOT / 'crates/zavliq-runtime/target/debug/zavliq'))
    parser.add_argument('--model', choices=list(MODELS), default='amazon.nova-micro-v1:0')
    parser.add_argument('--count', type=int, choices=range(1, 11), default=10)
    parser.add_argument('--service-ready', action='store_true')
    asyncio.run(main(parser.parse_args()))
