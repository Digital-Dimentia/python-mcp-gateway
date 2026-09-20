"""A Streamable HTTP MCP server in front of `mock_backend.py`, for `url:` backends.

Every knob `mock_backend.py` has works here too, because this is not a second mock: it runs
the stdio one as a child and puts HTTP in front of it. What this file adds is only the
transport, and the handful of things a test needs to do *to* a transport -- answer in SSE
instead of JSON, refuse the GET stream, forget a session as a restarted server would, and
report which headers it was sent.

In-process, on the test's own event loop, so a test can reach in and call `expire()` or read
`requests` without any IPC. One session at a time, which is what one gateway backend is.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

MOCK = Path(__file__).parent / "mock_backend.py"


class HttpBackend:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        *,
        sse: bool = False,
        get_stream: bool = True,
        sessions: bool = True,
        require_headers: dict[str, str] | None = None,
    ) -> None:
        self.env = env or {}
        #: Answer each request POST with an SSE stream rather than one JSON body. Messages
        #: the server sends while a request is in flight then ride that stream, which is
        #: where a real server puts the `roots/list` a tool call provokes.
        self.sse = sse
        #: Offer the GET stream, or answer it `405`.
        self.get_stream = get_stream
        #: Mint an `Mcp-Session-Id` on initialize and require it afterwards.
        self.sessions = sessions
        #: Headers every request must carry, or it is answered `401`.
        self.require_headers = require_headers or {}
        #: `(method, headers)` for every HTTP request received, headers lower-cased.
        self.requests: list[tuple[str, dict[str, str]]] = []
        #: The JSON-RPC method of every message that reached the child, in order. What the
        #: server was *asked*, as opposed to `requests`, which is the traffic that carried
        #: it: a refused request never appears here, and a renewal's re-sends do.
        self.received: list[str] = []
        self.session_id: str | None = None
        self.initializations = 0
        self._child: asyncio.subprocess.Process | None = None
        self._server: asyncio.base_events.Server | None = None
        self._pending: dict[Any, asyncio.Queue] = {}
        self._streams: list[asyncio.Queue] = []
        self._reader: asyncio.Task | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def expire(self) -> None:
        """Forget the session, as a server that restarted would."""
        self.session_id = None

    def methods(self, verb: str) -> list[dict[str, str]]:
        return [headers for method, headers in self.requests if method == verb]

    async def __aenter__(self) -> "HttpBackend":
        import os

        self._child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(MOCK),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **self.env},
            limit=8 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_child())
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._server is not None:
            self._server.close()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
        if self._child is not None:
            if self._child.stdin is not None:
                self._child.stdin.close()
            try:
                await asyncio.wait_for(self._child.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._child.kill()
                await self._child.wait()

    # --- the child ---------------------------------------------------------------------

    async def _read_child(self) -> None:
        assert self._child is not None and self._child.stdout is not None
        while line := await self._child.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" not in message and message.get("id") in self._pending:
                await self._pending[message["id"]].put(message)
                continue
            # Unprompted: a notification or a request of the server's own. In SSE mode it
            # rides whichever request stream is open; otherwise the GET stream.
            targets = list(self._pending.values()) if self.sse and self._pending else []
            for queue in targets[-1:] or self._streams:
                await queue.put(message)

    async def _to_child(self, message: Any) -> None:
        assert self._child is not None and self._child.stdin is not None
        if isinstance(message, dict) and "method" in message:
            self.received.append(message["method"])
        self._child.stdin.write((json.dumps(message) + "\n").encode())
        await self._child.stdin.drain()

    # --- HTTP --------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, *lines = head.decode("latin-1").split("\r\n")
            verb = request_line.split(" ")[0]
            headers: dict[str, str] = {}
            for line in lines:
                if line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
            body = b""
            if "content-length" in headers:
                body = await reader.readexactly(int(headers["content-length"]))
            self.requests.append((verb, headers))
            await self._route(verb, headers, body, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _route(
        self, verb: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        for name, value in self.require_headers.items():
            if headers.get(name.lower()) != value:
                return self._plain(writer, 401, "unauthorized")
        message = json.loads(body) if body else None
        is_init = isinstance(message, dict) and message.get("method") == "initialize"
        if self.sessions and not is_init:
            if headers.get("mcp-session-id") != self.session_id or self.session_id is None:
                return self._json(writer, 404, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32001, "message": "Session not found"},
                })
        if verb == "DELETE":
            self.session_id = None
            return self._plain(writer, 204, "")
        if verb == "GET":
            if not self.get_stream:
                return self._plain(writer, 405, "no stream")
            return await self._hold_stream(writer)
        if verb != "POST":
            return self._plain(writer, 405, "")

        if "method" not in message or "id" not in message:
            await self._to_child(message)
            return self._plain(writer, 202, "")

        queue: asyncio.Queue = asyncio.Queue()
        self._pending[message["id"]] = queue
        extra = {}
        if is_init:
            self.initializations += 1
            if self.sessions:
                self.session_id = uuid.uuid4().hex
                extra["Mcp-Session-Id"] = self.session_id
        try:
            await self._to_child(message)
            if not self.sse:
                reply = await queue.get()
                return self._json(writer, 200, reply, extra)
            self._head(writer, 200, "text/event-stream", extra, chunked=True)
            while True:
                item = await queue.get()
                self._chunk(writer, f"event: message\ndata: {json.dumps(item)}\n\n")
                await writer.drain()
                if "method" not in item and item.get("id") == message["id"]:
                    break
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            self._pending.pop(message["id"], None)

    async def _hold_stream(self, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self._streams.append(queue)
        self._head(writer, 200, "text/event-stream", {}, chunked=True)
        self._chunk(writer, ": opened\n\n")
        try:
            counter = 0
            while True:
                item = await queue.get()
                counter += 1
                self._chunk(writer, f"id: {counter}\ndata: {json.dumps(item)}\n\n")
                await writer.drain()
        finally:
            self._streams.remove(queue)

    @staticmethod
    def _head(writer, status: int, kind: str, extra: dict, *, chunked=False, length=None):
        lines = [f"HTTP/1.1 {status} X", f"Content-Type: {kind}", "Connection: close"]
        if chunked:
            lines.append("Transfer-Encoding: chunked")
        if length is not None:
            lines.append(f"Content-Length: {length}")
        lines += [f"{k}: {v}" for k, v in extra.items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())

    @staticmethod
    def _chunk(writer, text: str) -> None:
        data = text.encode()
        writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def _json(self, writer, status: int, payload: Any, extra: dict | None = None) -> None:
        data = json.dumps(payload).encode()
        self._head(writer, status, "application/json", extra or {}, length=len(data))
        writer.write(data)

    def _plain(self, writer, status: int, text: str) -> None:
        data = text.encode()
        self._head(writer, status, "text/plain", {}, length=len(data))
        writer.write(data)
