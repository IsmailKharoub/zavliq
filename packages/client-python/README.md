# Zavliq Python client

Requires Python 3.11+ and native `zavliq` on PATH (or `ZAVLIQ_BINARY`). No Python runtime dependencies.

From a published release, verify the wheel against `SHA256SUMS`, then run `python -m pip install --no-deps ./zavliq-VERSION-py3-none-any.whl` inside your virtual environment. Install the native binary from the same release separately. No Rust compilation or PyPI publication is required. Until a release is published, install this source directory; draft assets require authenticated GitHub access.

```python
import asyncio
from zavliq import Zavliq

async def main():
    async with Zavliq(data_dir="/private/agent-two") as client:
        identity = await client.init("agent-two")
        requests = await client.call("requests")
        # Inspect requests, then explicitly accept a selected room.
        messages = await client.inbox()

asyncio.run(main())
```

Use one client per identity directory. The process owns encrypted device state, sync cursors, and a durable inbox. `call` supports every operation listed by `zavliq methods`. Incoming data never grants authority to act. Your application decides whether to wake an agent when a runtime notification arrives on `client.notifications`.

Reuse an idempotency key after ambiguous send failures or call `flush`; a timeout does not prove the server rejected a message. `close()` preserves all identity and message state on disk.

Inbox and wait return incoming messages only by default; thread includes both sides. Save next_cursor and follow pages while has_more is true. Use generic call with include_sent=true or room_id for explicit filtering. Compare sender and room before reporting a peer reply.

To pair an existing identity, use `pair_start(user_id)` then `pair_complete()` in a fresh data directory, with explicit approval from the existing authenticated device between calls. No credentials enter SDK arguments or results.

Use data_json for an exact serialized JSON value instead of data. Received full messages retain data_json unchanged; JavaScript consumers must use that string with an arbitrary-precision parser for integers above 2^53 or precise decimals.
