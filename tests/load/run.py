#!/usr/bin/env python3
"""Paced synthetic messaging load against an explicitly isolated loopback stack."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
from zavliq import Zavliq, ZavliqError


def target_config(path: Path) -> dict:
    target = json.loads(path.read_text())
    parsed = urlsplit(target['origin'])
    if target.get('project') != 'zavliq-load' or target.get('environment') not in ('local', 'aws-staging'):
        raise ValueError('Use a dedicated zavliq-load project and explicit environment classification.')
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.port not in (19080, 19180, 28180) or parsed.path not in ('', '/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Only isolated loopback ports 19080/19180/28180 are allowed; shared/public services are excluded.')
    if not isinstance(target.get('hardware'), str) or len(target['hardware']) < 12:
        raise ValueError('Record the actual server hardware in the target manifest.')
    return target


def percentile(values: list[float], quantile: float):
    return round(sorted(values)[max(0, math.ceil(quantile * len(values)) - 1)], 4) if values else None


def metric(values: list[float]) -> dict:
    return {'count': len(values), 'p50_seconds': percentile(values, .5), 'p95_seconds': percentile(values, .95), 'p99_seconds': percentile(values, .99), 'max_seconds': round(max(values), 4) if values else None}


def evaluate(*, clients: int, duration: float, rate: float, planned: int, accepted: int, observed: int, missed_slots: int, failed: int, duplicates: int, latency: dict, environment: str) -> dict:
    complete = accepted == planned and observed == accepted and not (missed_slots or failed or duplicates)
    performance = latency['p95_seconds'] is not None and latency['p95_seconds'] < 2
    full = clients == 100 and duration == 1800 and rate == 10
    return {'zero_acknowledged_events_lost': observed == accepted, 'workload_complete': complete, 'p95_under_two_seconds': performance, 'full_load_parameters': full, 'local_diagnostic_passed': complete and performance, 'aws_staging_gate_passed': full and complete and performance and environment == 'aws-staging'}


class ReceiverStopped(RuntimeError):
    code = 'RECEIVER_STOPPED'

    def __init__(self, index: int, reason: str):
        self.receiver_index, self.reason = index, reason
        super().__init__('A receiver ended unexpectedly; measurement aborted.')


def check_receivers(tasks):
    for index, task in enumerate(tasks):
        if task.done():
            error = None if task.cancelled() else task.exception()
            if isinstance(error, ReceiverStopped):
                raise error from None
            reason = 'CANCELLED' if task.cancelled() else type(error).__name__ if error else 'EARLY_EXIT'
            raise ReceiverStopped(index, reason) from None


def check_notification(index, notification):
    if notification.get('method') == 'connection_state' and notification.get('params', {}).get('closed') is True:
        raise ReceiverStopped(index, 'RUNTIME_CLOSED')


async def run(args) -> dict:
    os.umask(0o077)
    # Bound local generator threads; no server configuration is changed.
    os.environ['TOKIO_WORKER_THREADS'] = '2'
    target = target_config(args.target)
    binary_path = shutil.which(args.binary)
    if not binary_path:
        raise ValueError('The selected native executable is unavailable.')
    with open(binary_path, 'rb') as executable:
        binary_sha256 = hashlib.file_digest(executable, 'sha256').hexdigest()
    if args.clients % 2 or not 2 <= args.clients <= 100 or not 1 <= args.rate <= 10 or not 1 <= args.duration <= 1800:
        raise ValueError('Use2..100 even clients,1..10 messages/second and1..1800 seconds.')
    if args.rate * 60 / args.clients > 24:
        raise ValueError('The chosen per-identity rate leaves insufficient headroom under30/minute.')
    identifier = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    fixture_id = args.resume_run or identifier
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}', fixture_id):
        raise ValueError('Invalid fixture identifier.')
    directory = ROOT / 'tests/load/.local' / fixture_id
    if args.resume_run and not directory.is_dir():
        raise ValueError('Resume requires an existing private fixture directory.')
    directory.mkdir(parents=True, mode=0o700, exist_ok=bool(args.resume_run))
    clients = [Zavliq(binary=args.binary, data_dir=str(directory / f'identity-{index:03}'), control_url=target['origin'], timeout=90) for index in range(args.clients)]
    identities, rooms, cursors = [None] * args.clients, [None] * args.clients, [0] * args.clients
    failures, accepted, observed, duplicate_sequences, ack_latencies, e2e_latencies = [], {}, {}, set(), [], []
    rpc_diagnostics = {name: [] for name in ('send_lock_wait', 'send_rpc', 'inbox_lock_wait', 'inbox_rpc')}
    locks = [asyncio.Lock() for _ in clients]
    observers, pending = [], set()
    state = {'missed_slots': 0, 'scheduled': 0, 'receive_errors': 0}
    enrollment_clock = {'next': 0.0, 'next_start': 0.0, 'completed': 0}
    enrollment_lock = asyncio.Lock()
    startup_lock = asyncio.Lock()
    ledger = (directory / f'events-{identifier}.jsonl').open('a', buffering=1)
    started = None
    started_at_utc = None

    def record(value):
        ledger.write(json.dumps(value) + '\n')

    async def enroll(index):
        async with enrollment_limit:
            async with startup_lock:
                await asyncio.sleep(max(0, enrollment_clock['next_start'] - time.monotonic()))
                enrollment_clock['next_start'] = time.monotonic() + .5
            suffix = hashlib.sha256(fixture_id.encode()).hexdigest()[:12]
            handle = f'{("cedar", "linden", "rowan", "willow")[index % 4]}-{suffix}-{index:03}'
            try:
                identity = await clients[index].identity()
            except ZavliqError as error:
                if error.code != 'NOT_INITIALIZED':
                    raise
                for attempt in range(4):
                    async with enrollment_lock:
                        # Respect the unchanged control limit20 attempts/minute.
                        await asyncio.sleep(max(0, enrollment_clock['next'] - time.monotonic()))
                        enrollment_clock['next'] = time.monotonic() + 3.2
                        try:
                            identity = await clients[index].init(handle)
                            break
                        except ZavliqError as error:
                            if error.code != 'RATE_LIMITED' or attempt == 3:
                                raise
                            found = re.search(r'retry_after_ms=(\d+)', str(error))
                            delay = min(120, max(3.2, int(found[1]) / 1000 + .5)) if found else 60
                            enrollment_clock['next'] = time.monotonic() + delay
                            record({'kind': 'fixture_admission_retry', 'identity_index': index, 'delay_seconds': delay})
            if identity['homeserver'].rstrip('/') != target['origin'].rstrip('/'):
                raise ValueError('Fixture homeserver differs from isolated target; stop before messaging.')
            if not identity['user_id'].startswith('@' + handle + ':'):
                raise ValueError('Existing identity does not match this fixture handle.')
            # Warm encrypted stores and initial sync inside the setup semaphore.
            await clients[index].call('sync')
            identities[index] = identity
            enrollment_clock['completed'] += 1
            if enrollment_clock['completed'] % 10 == 0:
                print(json.dumps({'state': 'fixture_setup', 'identities_ready': enrollment_clock['completed'], 'clients': args.clients}), flush=True)

    async def receive(index):
        while True:
            try:
                queued_at = time.monotonic()
                async with locks[index]:
                    rpc_started = time.monotonic()
                    page = await clients[index].call('inbox', {'cursor': cursors[index], 'limit': 100, 'full': True, 'sync': args.inbox_mode == 'fresh'})
                    rpc_finished = time.monotonic()
                rpc_diagnostics['inbox_lock_wait'].append(rpc_started - queued_at)
                rpc_diagnostics['inbox_rpc'].append(rpc_finished - rpc_started)
                cursors[index] = page['next_cursor']
                if page.get('history_gap_rooms'):
                    failures.append({'stage': 'receive', 'code': 'HISTORY_GAP', 'identity_index': index})
                for event in page['items']:
                    if event['sender'] != identities[index ^ 1]['user_id'] or event['room_id'] != rooms[index]:
                        continue
                    data = event.get('data', {})
                    # Runtime JSON payloads are profile events, not arbitrary text parsing.
                    if not isinstance(data, dict) or data.get('benchmark') != identifier:
                        continue
                    sequence = data.get('sequence')
                    if type(sequence) is not int:
                        continue
                    if sequence in observed and observed[sequence]['event_id'] != event['event_id']:
                        duplicate_sequences.add(sequence)
                    elif sequence not in observed:
                        observed[sequence] = {'event_id': event['event_id'], 'at': time.monotonic()}
                        record({'kind': 'observed', 'sequence': sequence, 'event_id': event['event_id'], 'at': observed[sequence]['at']})
                if page.get('has_more'):
                    continue
                if args.inbox_mode == 'background':
                    # Notifications follow durable ingestion. Never clear this
                    # queue: a wakeup may have arrived during the inbox read.
                    check_notification(index, await clients[index].notifications.get())
                else:
                    try:
                        check_notification(index, await asyncio.wait_for(clients[index].notifications.get(), timeout=2))
                    except asyncio.TimeoutError:
                        pass
            except ZavliqError as error:
                if error.code in ('RUNTIME_CLOSED', 'RUNTIME_UNAVAILABLE'):
                    raise ReceiverStopped(index, error.code) from None
                state['receive_errors'] += 1
                record({'kind': 'receive_error', 'identity_index': index, 'code': error.code})
                await asyncio.sleep(1)

    async def send(sequence, scheduled_at):
        index = sequence % args.clients
        try:
            queued_at = time.monotonic()
            async with locks[index]:
                rpc_started = time.monotonic()
                response = await clients[index].send(rooms[index], text='Scheduled synthetic greeting.', data={'benchmark': identifier, 'sequence': sequence}, idempotency_key=f'{identifier}-{sequence}')
            at = time.monotonic()
            if response.get('status') != 'accepted' or not response.get('event_id'):
                raise ValueError('SEND_NOT_ACCEPTED')
            accepted[sequence] = {'event_id': response['event_id'], 'scheduled_at': scheduled_at, 'at': at}
            ack_latencies.append(at - scheduled_at)
            rpc_diagnostics['send_lock_wait'].append(rpc_started - queued_at)
            rpc_diagnostics['send_rpc'].append(at - rpc_started)
            record({'kind': 'accepted', 'sequence': sequence, **accepted[sequence], 'lock_wait_seconds': rpc_started - queued_at, 'rpc_seconds': at - rpc_started})
        except Exception as error:
            code = getattr(error, 'code', type(error).__name__)
            failures.append({'stage': 'send', 'sequence': sequence, 'code': code})
            record({'kind': 'send_failure', 'sequence': sequence, 'code': code})

    try:
        enrollment_limit = asyncio.Semaphore(2)
        async with asyncio.TaskGroup() as group:
            for index in range(args.clients):
                group.create_task(enroll(index))
        print(json.dumps({'state': 'identities_ready', 'run_id': identifier, 'clients': args.clients}), flush=True)
        fixture_path = directory / 'fixture.json'
        if fixture_path.exists():
            fixture = json.loads(fixture_path.read_text())
            if fixture['clients'] != args.clients or fixture['origin'] != target['origin']:
                raise ValueError('Existing fixture size/origin differs from the requested measurement.')
            rooms = fixture['rooms']
        async def prepare_pair(index):
            async with pair_limit:
                if not rooms[index]:
                    own = await clients[index].call('conversations')
                    other = await clients[index + 1].call('conversations')
                    own_ids = {r['room_id'] for r in own['items'] if r.get('kind') == 'dm' and r.get('membership') == 'joined' and r.get('encryption') == 'standard'}
                    candidates = [r['room_id'] for r in other['items'] if r['room_id'] in own_ids and r.get('membership') in ('joined', 'invited') and identities[index]['user_id'] in r.get('creators', [])]
                    if len(candidates) > 1:
                        raise ValueError('AMBIGUOUS_FIXTURE_DM: reconcile the synthetic pair before resuming')
                    if candidates:
                        rooms[index] = rooms[index + 1] = candidates[0]
                    else:
                        room = await clients[index].create_conversation([identities[index + 1]['user_id']])
                        rooms[index] = rooms[index + 1] = room['room_id']
                # Save the room before join; an ambiguous join can safely be resumed.
                temporary = fixture_path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'clients': args.clients, 'origin': target['origin'], 'rooms': rooms}) + '\n')
                temporary.replace(fixture_path)
                peer_rooms = await clients[index + 1].call('conversations')
                peer_room = next((r for r in peer_rooms['items'] if r['room_id'] == rooms[index]), {})
                if peer_room.get('membership') != 'joined':
                    await clients[index + 1].call('accept', {'room_id': rooms[index]})
                for participant in (index, index + 1):
                    joined = await clients[participant].call('conversations')
                    current = next((r for r in joined['items'] if r['room_id'] == rooms[index]), {})
                    if current.get('membership') != 'joined' or current.get('joined_member_count') != 2 or current.get('encryption') != 'standard' or current.get('kind') != 'dm':
                        raise ValueError('FIXTURE_MEMBERSHIP_INCOMPLETE')
        pair_limit = asyncio.Semaphore(4)
        async with asyncio.TaskGroup() as group:
            for index in range(0, args.clients, 2):
                group.create_task(prepare_pair(index))
        if args.prepare_only:
            result = {'run_id': identifier, 'fixture_id': fixture_id, 'status': 'fixtures_prepared', 'clients': args.clients, 'environment': target['environment'], 'native_binary_sha256': binary_sha256, 'measured': False}
            print(json.dumps(result), flush=True)
            return result
        for client in clients:
            client.timeout = 30
        observers = [asyncio.create_task(receive(index)) for index in range(args.clients)]
        planned = int(args.duration * args.rate)
        started = time.monotonic()
        started_at_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        record({'kind': 'measurement_started', 'at': started, 'utc': started_at_utc})
        print(json.dumps({'state': 'running', 'run_id': identifier, 'environment': target['environment'], 'clients': args.clients, 'planned_messages': planned, 'duration_seconds': args.duration}), flush=True)
        for sequence in range(planned):
            check_receivers(observers)
            scheduled_at = started + sequence / args.rate
            await asyncio.sleep(max(0, scheduled_at - time.monotonic()))
            if time.monotonic() - scheduled_at > .25 or len(pending) >= args.clients * 2:
                state['missed_slots'] += 1
                continue
            task = asyncio.create_task(send(sequence, scheduled_at))
            pending.add(task); task.add_done_callback(pending.discard)
            state['scheduled'] += 1
            if sequence and sequence % int(args.rate * 30) == 0:
                print(json.dumps({'elapsed_seconds': round(time.monotonic() - started, 1), 'accepted': len(accepted), 'observed': len(observed), 'missed_slots': state['missed_slots'], 'send_failures': len(failures)}), flush=True)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=65)
        drain_deadline = time.monotonic() + 60
        while time.monotonic() < drain_deadline and set(accepted) - set(observed):
            check_receivers(observers)
            await asyncio.sleep(.25)
        # Continue observing briefly after the last expected receipt for tail duplicates.
        await asyncio.sleep(2.2)
        check_receivers(observers)
        matched = 0
        for sequence, sent in accepted.items():
            received = observed.get(sequence)
            if received and received['event_id'] == sent['event_id']:
                matched += 1
                e2e_latencies.append(received['at'] - sent['scheduled_at'])
        latency = metric(e2e_latencies)
        result = {'run_id': identifier, 'fixture_id': fixture_id, 'environment': target['environment'], 'hardware': target['hardware'], 'clients': args.clients, 'duration_seconds': args.duration, 'target_messages_per_second': args.rate, 'planned_messages': planned, 'accepted_messages': len(accepted), 'observed_acknowledged_messages': matched, 'missing_acknowledged_messages': len(accepted) - matched, 'duplicate_sequences': len(duplicate_sequences), 'failures': failures, **state, 'send_acknowledgement_latency': metric(ack_latencies), 'end_to_end_latency': latency, 'elapsed_including_drain_seconds': round(time.monotonic() - started, 3)}
        result['checks'] = evaluate(clients=args.clients, duration=args.duration, rate=args.rate, planned=planned, accepted=len(accepted), observed=matched, missed_slots=state['missed_slots'], failed=len(failures), duplicates=len(duplicate_sequences), latency=latency, environment=target['environment'])
        result['generator'] = {'tokio_workers_per_runtime': 2, 'setup_concurrency': 2, 'pair_validation_concurrency': 4, 'process_start_spacing_seconds': .5, 'store_and_sync_warmup_before_timer': True, 'setup_timeout_seconds': 90, 'measurement_rpc_timeout_seconds': 30}
        result['generator']['inbox_mode'] = args.inbox_mode
        result['native_binary_sha256'] = binary_sha256
        result['measurement_started_at_utc'] = started_at_utc
        result['measurement_finished_at_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        result['rpc_diagnostics'] = {name: metric(values) for name, values in rpc_diagnostics.items()}
        evidence = ROOT / 'tests/load/evidence'
        evidence.mkdir(exist_ok=True)
        (evidence / f'{identifier}.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
        return result
    except Exception as error:
        result = {'run_id': identifier, 'environment': target['environment'], 'status': 'failed', 'stage': 'measurement' if started else 'fixture_setup', 'error_code': getattr(error, 'code', type(error).__name__), 'accepted_messages': len(accepted), 'observed_messages': len(observed), 'native_binary_sha256': binary_sha256, 'aws_staging_gate_passed': False}
        result['inbox_mode'] = args.inbox_mode
        result['measurement_started_at_utc'] = started_at_utc
        if isinstance(error, ReceiverStopped):
            result.update(receiver_index=error.receiver_index, receiver_error_class=error.reason)
        evidence = ROOT / 'tests/load/evidence'
        evidence.mkdir(exist_ok=True)
        (evidence / f'{identifier}.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
        raise
    finally:
        for task in observers + list(pending):
            task.cancel()
        await asyncio.gather(*observers, *pending, return_exceptions=True)
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        ledger.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=ROOT / 'tests/load/.local/stack/target.json')
    parser.add_argument('--binary', default=str(ROOT / 'crates/zavliq-runtime/target/debug/zavliq'))
    parser.add_argument('--clients', type=int, default=100)
    parser.add_argument('--rate', type=float, default=10)
    parser.add_argument('--duration', type=float, default=1800)
    parser.add_argument('--resume-run', help='Reuse existing private fixture identities and rooms; creates a new measurement record.')
    parser.add_argument('--prepare-only', action='store_true', help='Prepare identities and rooms, then close clients before a separate measurement.')
    parser.add_argument('--inbox-mode', choices=('fresh', 'background'), default='fresh', help='Fresh sync per read (default), or local durable reads driven by runtime notifications without polling.')
    args = parser.parse_args()
    result = asyncio.run(run(args))
    if result.get('status') != 'fixtures_prepared' and not result.get('checks', {}).get('local_diagnostic_passed', False):
        raise SystemExit(1)
