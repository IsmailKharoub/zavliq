import argparse
import asyncio
import json
from itertools import islice
import os
from pathlib import Path
import signal
import sys
from dataclasses import dataclass
from typing import Any

from .state import Capacity, State, transaction_id
from .health import Health


@dataclass(frozen=True)
class Limits:
    per_minute: int = 20
    per_day: int = 500
    peer_per_minute: int = 5
    peer_per_day: int = 50
    accepts_per_minute: int = 10
    accepts_per_day: int = 100
    max_rooms: int = 1000
    body_bytes: int = 24 * 1024


def standard_dm(room: dict[str, Any], *, invited=False):
    count = room.get("joined_member_count")
    # Stripped invite state has no member summary. The immutable DM metadata
    # is checked first; after joining require an authoritative count of two.
    count_ok = ((count is None or type(count) is int and count in (0, 1)) if invited
                else type(count) is int and count == 2)
    inviter = room.get("inviter")
    return (room.get("kind") == "dm" and room.get("encrypted") is False
            and room.get("membership") == ("invited" if invited else "joined")
            and count_ok and (not invited or isinstance(inviter, str)
                              and inviter.startswith("@") and ":" in inviter))


def payload(item: dict[str, Any], owner: str, body_limit: int):
    """Accept only literal original text/JSON; replies and edits cannot start loops."""
    content = item.get("content")
    if item.get("sender") == owner or not isinstance(content, dict):
        return None
    if content.get("msgtype") != "m.text" or "m.relates_to" in content or "algorithm" in content:
        return None
    # Attachments, HTML, executable requests and URLs are never interpreted.
    text = content.get("body")
    encoded = content.get("com.zavliq.data_json")
    if not isinstance(text, str) or (encoded is not None and not isinstance(encoded, str)):
        return None
    result: dict[str, Any] = {}
    if text:
        result["text"] = text
    if encoded is not None:
        try:
            json.loads(encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            return None
        result["data_json"] = encoded
    elif "com.zavliq.data" in content:
        # Older valid JSON transport; native runtime revalidates this value.
        result["data"] = content["com.zavliq.data"]
    if not result or len(json.dumps(result, ensure_ascii=False).encode()) > body_limit:
        return None
    return result


def permanent_conversation_error(error):
    return getattr(error, "code", None) in {
        "M_FORBIDDEN", "FORBIDDEN", "M_NOT_FOUND", "NOT_FOUND", "ROOM_NOT_FOUND",
    }


class Echo:
    def __init__(self, client, state: State, owner: str, limits=Limits()):
        self.client, self.state, self.owner, self.limits = client, state, owner, limits

    async def verify_identity(self):
        identity = await self.client.call("identity")
        if identity.get("user_id") != self.owner:
            raise RuntimeError("The runtime identity does not match the configured echo account.")

    async def once(self):
        conversations = (await self.client.call("conversations"))["items"]
        joined = sum(room.get("membership") == "joined" for room in conversations)
        pending = (await self.client.call("requests"))["items"]
        changed = False
        candidates = (r for r in pending if standard_dm(r, invited=True) and r.get("inviter") != self.owner)
        for room in islice(candidates, 100):
            if joined >= self.limits.max_rooms:
                break
            action = "accept:" + room["room_id"]
            try:
                status = self.state.reserve(action, [
                    ("accept-minute", 60, self.limits.accepts_per_minute),
                    ("accept-day", 86400, self.limits.accepts_per_day),
                ])
            except Capacity:
                break
            if status != "done":
                try:
                    await self.client.call("accept", {"room_id": room["room_id"]})
                except Exception as error:
                    if not permanent_conversation_error(error):
                        raise
                    self.state.finish(action)
                    continue
                self.state.finish(action)
                changed = True
                joined += 1
        if changed:
            conversations = (await self.client.call("conversations"))["items"]
        allowed = {r["room_id"] for r in conversations if standard_dm(r)}
        unknown_counts = {r["room_id"] for r in conversations
            if r.get("kind") == "dm" and r.get("encrypted") is False
            and r.get("membership") == "joined"
            and (type(r.get("joined_member_count")) is not int or r["joined_member_count"] == 0)}
        page = await self.client.call("inbox", {"cursor": self.state.cursor, "limit": 100, "full": True})
        for item in page["items"]:
            cursor = item["cursor"]
            if item["room_id"] in unknown_counts:
                return 2
            data = payload(item, self.owner, self.limits.body_bytes) if item["room_id"] in allowed else None
            if data is None:
                self.state.advance(cursor)
                continue
            action = transaction_id(item["event_id"])
            # Per-peer exhaustion drops that peer's excess demo input. A full
            # service budget pauses before advancing this global arrival cursor.
            peer = transaction_id(item["sender"])
            peer_limits = [(peer + ":minute", 60, self.limits.peer_per_minute),
                           (peer + ":day", 86400, self.limits.peer_per_day)]
            all_limits = [("send-minute", 60, self.limits.per_minute),
                          ("send-day", 86400, self.limits.per_day), *peer_limits]
            try:
                status = self.state.reserve(action, all_limits)
            except Capacity as error:
                # Distinguish peer capacity without making a second reservation.
                now = int(self.state.now())
                peer_full = any((self.state.db.execute(
                    "SELECT used FROM counters WHERE scope=? AND window=? AND duration=?",
                    (scope, now // duration * duration, duration)).fetchone() or (0,))[0] >= cap
                    for scope, duration, cap in peer_limits)
                if peer_full:
                    self.state.finish(action, cursor)
                    continue
                return min(error.retry_after, 30)
            if status != "done":
                try:
                    await self.client.call("send", {"room_id": item["room_id"], **data,
                        "reply_to": item["event_id"], "idempotency_key": action})
                    # Acknowledgement is retried with the same event ID. The send is
                    # already deduplicated by its native persisted transaction ID.
                    await self.client.call("acknowledge", {"event_id": item["event_id"], "status": "delivered"})
                except Exception as error:
                    if not permanent_conversation_error(error):
                        raise
                    # A withdrawn conversation cannot indefinitely block other DMs.
                    # This only cancels unsent local retry state, never recalls data.
                    await self.client.call("cancel_send", {"transaction_id": action})
                self.state.finish(action, cursor)
            else:
                self.state.advance(cursor)
        next_cursor = page.get("next_cursor")
        if isinstance(next_cursor, int):
            self.state.advance(next_cursor)
        self.state.prune()
        return 0.05 if page.get("has_more") else 2


async def run(args):
    from zavliq import Zavliq
    os.umask(0o077)
    state = State(Path(args.state_dir), args.owner)
    health = Health(Path(args.state_dir), args.owner)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    try:
        health.update('starting')
        async with Zavliq(binary=args.binary, data_dir=args.data_dir, control_url=args.control_url) as client:
            service = Echo(client, state, args.owner)
            await service.verify_identity()
            print(json.dumps({"event": "echo_ready", "user_id": args.owner}), flush=True)
            failures = 0
            while not stopping.is_set():
                try:
                    delay = await service.once()
                    failures = 0
                    health.update('ready')
                except Exception:
                    failures += 1
                    delay = min(2 ** min(failures, 6), 60)
                    health.update('retrying')
                    # Never print exception strings, received messages or RPC data.
                    print(json.dumps({"event": "echo_retry", "retry_seconds": delay}), flush=True)
                try:
                    await asyncio.wait_for(stopping.wait(), delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        try:
            health.update('stopped')
        finally:
            state.close()


def main():
    parser = argparse.ArgumentParser(description="Opt-in literal echo for an operator-owned Zavliq account")
    parser.add_argument("--enabled", action="store_true", default=os.environ.get("ZAVLIQ_ECHO_ENABLED") == "true")
    parser.add_argument("--owner", default=os.environ.get("ZAVLIQ_ECHO_USER_ID"))
    parser.add_argument("--data-dir", default=os.environ.get("ZAVLIQ_DATA_DIR"))
    parser.add_argument("--state-dir", default=os.environ.get("ZAVLIQ_ECHO_STATE_DIR"))
    parser.add_argument("--binary", default=os.environ.get("ZAVLIQ_BINARY", "zavliq"))
    parser.add_argument("--control-url", default=os.environ.get("ZAVLIQ_CONTROL_URL", "https://zavliq.com"))
    args = parser.parse_args()
    if not args.enabled:
        parser.error("Echo is disabled; pass --enabled only after operator provisioning.")
    if not args.owner or not args.owner.startswith("@") or ":" not in args.owner:
        parser.error("Set --owner to the exact operator-owned Matrix address.")
    if not args.data_dir or not args.state_dir:
        parser.error("Set separate persistent private --data-dir and --state-dir paths.")
    if Path(args.data_dir).resolve() == Path(args.state_dir).resolve():
        parser.error("Runtime and echo journal paths must be separate.")
    try:
        asyncio.run(run(args))
    except (Exception, KeyboardInterrupt):
        print('{"event":"echo_stopped","action":"Check private runtime provisioning, identity ownership and directory access."}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
