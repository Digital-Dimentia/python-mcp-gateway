"""A minimal MCP client over WebSocket, for driving a real daemon in tests.

Module-level async helpers rather than pytest fixtures, matching the template's idiom: a
harness that is a plain function can be called twice in one test, which is exactly what the
multi-client cases need.

Everything here speaks the wire. Nothing imports the daemon's session or handler code, so a
test that passes proves a real client would work rather than proving two halves of our own
code agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import websockets

from mcp_gateway.config import load as load_config
from mcp_gateway.gateway import Gateway
from mcp_gateway.logging_redaction import install_redaction
from mcp_gateway.protocol import MCP_PROTOCOL_VERSION
from mcp_gateway.secret_providers import build_store


class Client:
    """One connection. Correlates responses, and records notifications for assertions."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        #: Every notification the daemon sent, in order. Tests assert on this directly --
        #: "exactly one tools/list_changed reached the client" is the shape that matters.
        self.notifications: list[dict[str, Any]] = []
        #: Requests the daemon sent *us* (reverse passthrough), with an answer callback.
        self.server_requests: list[dict[str, Any]] = []
        self.answer_with: Any = None
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        with contextlib.suppress(Exception):
            async for raw in self._websocket:
                message = json.loads(raw)
                if "method" in message and "id" not in message:
                    self.notifications.append(message)
                elif "method" in message:
                    self.server_requests.append(message)
                    if self.answer_with is not None:
                        await self._send(
                            {"jsonrpc": "2.0", "id": message["id"], "result": self.answer_with}
                        )
                else:
                    future = self._pending.pop(message.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(message)

    async def _send(self, message: dict) -> None:
        await self._websocket.send(json.dumps(message))

    async def request(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        """Send a request and return the whole response envelope, error included.

        The envelope rather than the result, because half these tests are about which error
        code came back and what `data.source` said.
        """
        self._id += 1
        request_id = self._id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return await asyncio.wait_for(future, timeout)

    async def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        """Like `request`, but fail the test on an error response."""
        response = await self.request(method, params, timeout)
        assert "error" not in response, response["error"]
        return response["result"]

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def initialize(self, capabilities: dict | None = None, version: str | None = None) -> dict:
        result = await self.call(
            "initialize",
            {
                "protocolVersion": version or MCP_PROTOCOL_VERSION,
                "capabilities": capabilities or {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        )
        await self.notify("notifications/initialized")
        # A round trip after the notification, so the handshake is demonstrably complete
        # before the test looks at anything. A notification has no reply of its own, and the
        # transport dispatches each message in its own task, so without this a test can read
        # session state the daemon has not applied yet.
        await self.call("ping")
        return result

    async def tool_names(self) -> list[str]:
        return [tool["name"] for tool in (await self.call("tools/list"))["tools"]]

    async def tool_text(self, name: str, arguments: dict | None = None) -> str:
        result = await self.call("tools/call", {"name": name, "arguments": arguments or {}})
        return "\n".join(
            block.get("text", "") for block in result.get("content", []) if isinstance(block, dict)
        )

    async def close(self) -> None:
        self._reader.cancel()
        # CancelledError is a BaseException, so `suppress(Exception)` does not catch it.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        with contextlib.suppress(Exception):
            await self._websocket.close()


class Harness:
    """A running daemon plus a factory for connections to it."""

    def __init__(self, gateway: Gateway, access_key: str | None) -> None:
        self.gateway = gateway
        self.access_key = access_key
        self._clients: list[Client] = []

    @property
    def port(self) -> int:
        assert self.gateway.server is not None
        return self.gateway.server.port

    def url(self, path: str = "/mcp", key: str | None = ...) -> str:  # type: ignore[assignment]
        chosen = self.access_key if key is ... else key
        suffix = f"?key={chosen}" if chosen else ""
        return f"ws://127.0.0.1:{self.port}{path}{suffix}"

    async def connect(
        self,
        path: str = "/mcp",
        *,
        key: str | None = ...,  # type: ignore[assignment]
        capabilities: dict | None = None,
        initialize: bool = True,
    ) -> Client:
        websocket = await websockets.connect(self.url(path, key))
        client = Client(websocket)
        self._clients.append(client)
        if initialize:
            await client.initialize(capabilities)
        return client

    async def close(self) -> None:
        for client in self._clients:
            await client.close()
        await self.gateway.stop()


async def daemon(
    tmp_path: Path,
    *,
    servers: str = "servers: {}\n",
    env: str = "",
    host: str = "127.0.0.1",
    port: int = 0,
    access_key: str | None = None,
    allow_unauthenticated: bool = False,
    tls: Any = None,
) -> Harness:
    """Write both config files into `tmp_path` and start a daemon.

    Port 0 by default so tests run in parallel without colliding; `Harness.port` reports what
    the OS gave us. Pass an explicit `port` to rebind one a previous harness released, which
    is how the reconnect test puts the daemon back where the bridge is looking for it.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "servers.yaml"
    config_path.write_text(servers)
    env_path = tmp_path / "gateway.env"
    env_path.write_text(env)

    # `build_store`, not `secrets.load`, so a harness builds its store exactly the way the
    # daemon does -- including any `secrets:` block the test put in `servers`.
    config = load_config(config_path)
    store = build_store(config, env_path)
    install_redaction(store, extra=[access_key] if access_key else [])
    gateway = Gateway(
        config,
        store,
        config_path=config_path,
        env_path=env_path,
        host=host,
        port=port,
    )
    await gateway.start(
        access_key=access_key, allow_unauthenticated=allow_unauthenticated, tls=tls
    )
    return Harness(gateway, access_key)
