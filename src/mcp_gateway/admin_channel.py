"""The `/admin` connection: a plain JSON-RPC method table for the UI.

Not MCP. A UI should not have to perform an MCP handshake, negotiate a protocol revision, or
wrap every question in a `tools/call` envelope just to ask which backends are up. And the
inverse matters more: **admin verbs must not appear in the model's tool list.** Two paths on
one port is what buys both.

The implementations are shared with the `gateway__*` meta-tools -- `admin.py` holds them
once -- so the two surfaces cannot drift into disagreeing about what "running" means.

## Read-only in v1

`admin.status`, `admin.backends`, `admin.health`, `admin.config.get`, `admin.secrets.keys`,
`admin.logs.tail`, plus `admin.reload` and `admin.backend.restart`, which ship because they
are the same code the meta-tools already expose to the model -- withholding them from the UI
while the model can call them would be theatre.

The mutating verbs the UI epic needs -- `admin.config.set`, `admin.secrets.set`,
`admin.backend.add`/`remove` -- are **not implemented**. When they land they go here and only
here: a surface the model cannot reach. See `admin.md` for that decision and its reasoning.

## No handshake

There is no `initialize` on this path. A method may be called immediately, because there is
nothing to negotiate: the method table is fixed, and an admin client that knows the URL knows
the protocol. `ping` exists so a UI can check liveness the same way anything else does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp_gateway import errors, jsonrpc
from mcp_gateway.transport_ws import ClientLink

logger = logging.getLogger(__name__)

#: How many recent log records a new subscriber is handed. Enough to explain what just
#: happened -- a failed reload, a backend that died -- without shipping a session's history.
LOG_BACKLOG = 200


class LogStream(logging.Handler):
    """Captures redacted log records for `admin.logs.tail`.

    Installed on the root logger **after** the redaction filter, so every record it sees has
    already been scrubbed. That ordering is the whole safety argument: this class formats
    records and sends them over a socket, and it must never be the thing that reaches a
    credential first.
    """

    def __init__(self, capacity: int = LOG_BACKLOG) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: list[dict[str, Any]] = []
        self._subscribers: set[Any] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        except Exception:  # pragma: no cover - a record that will not format is not fatal
            return
        self._records.append(entry)
        if len(self._records) > self.capacity:
            del self._records[: len(self._records) - self.capacity]
        for queue in list(self._subscribers):
            # `put_nowait` on a bounded queue: a subscriber that stops reading must not be
            # able to make the daemon buffer without limit, and dropping a log line for a
            # stalled UI is the right failure.
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def backlog(self) -> list[dict[str, Any]]:
        return list(self._records)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


class AdminConnection:
    """One `/admin` connection. Same shape as `Session`, different method table."""

    def __init__(self, link: ClientLink, gateway: Any) -> None:
        self.link = link
        self.gateway = gateway
        self._tail: asyncio.Task[None] | None = None
        self._handlers = {
            "ping": self._ping,
            "admin.status": gateway.admin.status,
            "admin.backends": gateway.admin.backends,
            "admin.health": gateway.admin.health,
            "admin.config.get": self._config_get,
            "admin.secrets.keys": gateway.admin.secrets_keys,
            "admin.reload": self._reload,
            "admin.backend.restart": self._restart,
            "admin.logs.tail": self._logs_tail,
            "admin.logs.stop": self._logs_stop,
        }

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        kind = jsonrpc.classify(message)
        if kind is not jsonrpc.Kind.REQUEST:
            logger.debug("ignoring %s on /admin", kind.value)
            return None
        method = message.get("method")
        handler = self._handlers.get(str(method))
        if handler is None:
            raise errors.GatewayErrorObject(errors.MethodNotFound(str(method)).to_error_object())
        return await self._dispatch(handler, message.get("params") or {})

    @errors.as_error_object
    async def _dispatch(self, handler, params: dict) -> dict[str, Any]:
        return await handler(params)

    async def closed(self) -> None:
        await self._stop_tail()

    # --- handlers -----------------------------------------------------------------------

    async def _ping(self, _params: dict) -> dict[str, Any]:
        return {}

    async def _config_get(self, _params: dict) -> dict[str, Any]:
        """The catalogue as parsed, with `${VAR}` references **unresolved**.

        A UI editing the config must see and write back the references, not the values --
        rendering resolved secrets into an editor is how they end up pasted somewhere else.
        """
        config = self.gateway.config
        return {
            "source": str(config.source),
            "servers": {
                name: {
                    "command": spec.command,
                    "args": list(spec.args),
                    "env": dict(spec.env),
                    "env_passthrough": list(spec.env_passthrough),
                    "env_mode": spec.env_mode,
                    "cwd": spec.cwd,
                    "timeout": spec.timeout,
                    "startup_timeout": spec.startup_timeout,
                    "enabled": spec.enabled,
                    "required": spec.required,
                    "description": spec.description,
                }
                for name, spec in config.servers.items()
            },
        }

    async def _reload(self, params: dict) -> dict[str, Any]:
        """The same code `gateway__reload_config` calls, unwrapped from its tool envelope."""
        result = await self.gateway.reload(dry_run=bool(params.get("dry_run")))
        if result.get("isError"):
            raise errors.GatewayError(result["content"][0]["text"])
        import json

        return json.loads(result["content"][0]["text"])

    async def _restart(self, params: dict) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise errors.InvalidParams("admin.backend.restart requires a 'name'")
        if self.gateway.backend(name) is None:
            raise errors.InvalidParams(f"no backend named {name!r}")
        running = await self.gateway.restart_backend(name)
        return {"name": name, "running": running, "error": self.gateway.backend(name).error}

    async def _logs_tail(self, params: dict) -> dict[str, Any]:
        """Start streaming redacted log records as `admin.logs` notifications."""
        if self._tail is not None:
            raise errors.InvalidParams("this connection is already tailing the log")
        stream = self.gateway.log_stream
        if stream is None:
            raise errors.GatewayError("log streaming is not enabled on this daemon")
        backlog = stream.backlog()
        if params.get("backlog") is False:
            backlog = []
        queue = stream.subscribe()
        self._tail = asyncio.create_task(self._pump(stream, queue))
        return {"streaming": True, "backlog": backlog}

    async def _logs_stop(self, _params: dict) -> dict[str, Any]:
        await self._stop_tail()
        return {"streaming": False}

    async def _pump(self, stream: LogStream, queue: asyncio.Queue) -> None:
        try:
            while True:
                entry = await queue.get()
                await self.link.notify("admin.logs", entry)
        except asyncio.CancelledError:
            raise
        finally:
            stream.unsubscribe(queue)

    async def _stop_tail(self) -> None:
        task, self._tail = self._tail, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
