import asyncio
import json
import os
import uuid
from typing import Any


class ZavliqError(Exception):
    def __init__(self, code: str, message: str, action: str | None = None):
        super().__init__(message)
        self.code, self.action = code, action


class Zavliq:
    """Owns one runtime process. No agent invocation or automatic message actions."""
    def __init__(self, *, binary: str | None = None, data_dir: str | None = None,
                 control_url: str | None = None, timeout: float = 120):
        self.binary = binary or os.environ.get("ZAVLIQ_BINARY", "zavliq")
        self.args = ["rpc"]
        if data_dir:
            self.args += ["--data-dir", data_dir]
        if control_url:
            self.args += ["--control-url", control_url]
        self.timeout = timeout
        self._process = None
        self._reader_task = None
        self._start_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._sequence = 0
        self._closed = False
        self.notifications = asyncio.Queue(maxsize=100)

    async def __aenter__(self):
        await self._start()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def _start(self):
        async with self._start_lock:
            if self._closed:
                raise ZavliqError("RUNTIME_CLOSED", "Create a new client connection.")
            if self._process is not None:
                return
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.binary, *self.args, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=8 * 1024 * 1024)
            except OSError as exc:
                raise ZavliqError("RUNTIME_UNAVAILABLE", "Install zavliq or set ZAVLIQ_BINARY.") from exc
            self._reader_task = asyncio.create_task(self._read())

    def _fail(self, error):
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _read(self):
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                if "method" in message and "id" not in message:
                    if self.notifications.full():
                        self.notifications.get_nowait()
                    self.notifications.put_nowait(message)
                    continue
                future = self._pending.pop(message.get("id"), None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    detail = message["error"].get("data", message["error"])
                    future.set_exception(ZavliqError(str(detail.get("code", "OPERATION_FAILED")),
                                                    detail.get("message", "Operation failed."), detail.get("action")))
                else:
                    future.set_result(message.get("result"))
        except (ValueError, OSError) as exc:
            self._fail(ZavliqError("INVALID_RESPONSE", "Runtime response could not be read."))
        finally:
            self._closed = True
            self._fail(ZavliqError("RUNTIME_CLOSED", "Runtime exited. Check whether another process owns this identity directory."))

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self._start()
        self._sequence += 1
        request_id = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id,
                                                  "method": method, "params": params or {}}) + "\n").encode())
            await self._process.stdin.drain()
            return await asyncio.wait_for(future, self.timeout)
        except asyncio.TimeoutError as exc:
            raise ZavliqError("OUTCOME_UNKNOWN", "Operation timed out and may still complete. Reuse its idempotency key or call flush.") from exc
        finally:
            self._pending.pop(request_id, None)

    async def init(self, handle: str, display_name: str | None = None):
        return await self.call("init", {"handle": handle, "display_name": display_name})

    async def pair_start(self, user_id: str, device_display_name: str | None = None):
        return await self.call("pairing_start", {"user_id": user_id, "device_display_name": device_display_name})

    async def pair_complete(self):
        return await self.call("pairing_complete")

    async def identity(self):
        return await self.call("identity")

    async def send(self, room_id: str, *, text: str | None = None, data: Any = None,
                   idempotency_key: str | None = None, reply_to: str | None = None, data_json: str | None = None):
        params = {"room_id": room_id, "idempotency_key": idempotency_key or str(uuid.uuid4())}
        if text is not None:
            params["text"] = text
        if data is not None:
            params["data"] = data
        if data_json is not None:
            params["data_json"] = data_json
        if reply_to is not None:
            params["reply_to"] = reply_to
        return await self.call("send", params)

    async def inbox(self, cursor: int = 0, limit: int = 10):
        return await self.call("inbox", {"cursor": cursor, "limit": limit})

    async def wait(self, cursor: int = 0, timeout_seconds: int = 30):
        return await self.call("wait", {"cursor": cursor, "timeout_seconds": timeout_seconds})

    async def create_conversation(self, members: list[str], *, kind: str = "dm",
                                  encryption: str = "standard", name: str | None = None):
        return await self.call("create_conversation", {"members": members, "kind": kind,
                                                       "encryption": encryption, "name": name})

    async def close(self):
        self._closed = True
        if self._process and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()
        if self._reader_task:
            await self._reader_task
        self._fail(ZavliqError("RUNTIME_CLOSED", "Client connection closed."))
