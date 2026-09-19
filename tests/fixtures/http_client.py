"""A Streamable HTTP client, for driving `/mcp` the way Claude Code drives it.

Hand-rolled on `asyncio.open_connection` for the same reason the server is: what is being
tested is an HTTP conversation, and a library that normalises it would hide exactly the
things worth asserting on -- which status a refusal carries, whether the session header came
back, how the SSE frames are split.

One connection per request, which is what the server does too. The SSE stream is the
exception and is what `stream()` returns.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def session_id(self) -> str | None:
        return self.headers.get("mcp-session-id")


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


class HttpClient:
    """Requests against one gateway, remembering the session id once it has one."""

    def __init__(
        self, port: int, host: str = "127.0.0.1", key: str | None = None, ssl: Any = None
    ) -> None:
        self.host = host
        self.port = port
        self.key = key
        #: A client `SSLContext` to speak https with; `None` for plain http.
        self.ssl = ssl
        self.session_id: str | None = None
        self._id = 0

    def _target(self, path: str = "/mcp") -> str:
        return f"{path}?key={self.key}" if self.key else path

    async def send(
        self,
        method: str,
        body: bytes = b"",
        *,
        path: str = "/mcp",
        headers: dict[str, str] | None = None,
        session: str | None = ...,  # type: ignore[assignment]
        accept: str | None = "application/json, text/event-stream",
    ) -> Response:
        reader, writer = await asyncio.open_connection(self.host, self.port, ssl=self.ssl)
        head = {"Host": f"{self.host}:{self.port}", "Content-Length": str(len(body))}
        if accept is not None:
            head["Accept"] = accept
        if body:
            head["Content-Type"] = "application/json"
        sid = self.session_id if session is ... else session
        if sid:
            head["Mcp-Session-Id"] = sid
        head.update(headers or {})
        raw = f"{method} {self._target(path)} HTTP/1.1\r\n"
        raw += "".join(f"{k}: {v}\r\n" for k, v in head.items())
        writer.write(raw.encode("latin-1") + b"\r\n" + body)
        await writer.drain()
        try:
            status, response_headers = await _read_head(reader)
            payload = await reader.read()
        finally:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 5)
        return Response(status, response_headers, payload)

    async def rpc(self, method: str, params: dict | None = None, **kwargs: Any) -> Response:
        self._id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        return await self.send("POST", json.dumps(message).encode(), **kwargs)

    async def notify(self, method: str, params: dict | None = None, **kwargs: Any) -> Response:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        return await self.send("POST", json.dumps(message).encode(), **kwargs)

    async def initialize(self, **kwargs: Any) -> Response:
        response = await self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "http-test", "version": "1"},
            },
            session=None,
            **kwargs,
        )
        if response.session_id:
            self.session_id = response.session_id
            await self.notify("notifications/initialized")
        return response

    async def result(self, method: str, params: dict | None = None) -> Any:
        response = await self.rpc(method, params)
        assert response.status == 200, (response.status, response.body)
        return response.json()["result"]

    def stream(self) -> "SseClient":
        return SseClient(self)


class SseClient:
    """An open `GET /mcp`, read frame by frame."""

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.status = 0
        self.headers: dict[str, str] = {}
        self.messages: list[dict] = []
        self._pump: asyncio.Task | None = None

    async def __aenter__(self) -> "SseClient":
        reader, writer = await asyncio.open_connection(
            self._client.host, self._client.port, ssl=self._client.ssl
        )
        head = {
            "Host": f"{self._client.host}:{self._client.port}",
            "Accept": "text/event-stream",
        }
        if self._client.session_id:
            head["Mcp-Session-Id"] = self._client.session_id
        raw = f"GET {self._client._target()} HTTP/1.1\r\n"
        raw += "".join(f"{k}: {v}\r\n" for k, v in head.items())
        writer.write(raw.encode("latin-1") + b"\r\n")
        await writer.drain()
        self._reader, self._writer = reader, writer
        self.status, self.headers = await _read_head(reader)
        if self.status == 200:
            self._pump = asyncio.create_task(self._read_frames())
        return self

    async def _read_frames(self) -> None:
        assert self._reader is not None
        buffer = ""
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                return
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                frame, _, buffer = buffer.partition("\n\n")
                data = "\n".join(
                    line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:")
                )
                if data:
                    self.messages.append(json.loads(data))

    def of_kind(self, method: str) -> list[dict]:
        return [m for m in self.messages if m.get("method") == method]

    async def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False

    async def __aexit__(self, *exc: Any) -> None:
        if self._pump is not None:
            self._pump.cancel()
        if self._writer is not None:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), 5)
            except (asyncio.TimeoutError, ConnectionError):
                pass
