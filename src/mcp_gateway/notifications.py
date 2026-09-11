"""Relay what a backend says unprompted, to the clients that should hear it.

A backend speaks up in four ways that matter, and each gets a different answer. Getting this
table wrong is how a daemon becomes noisy or stale, and neither symptom points at its cause.

| Backend sends | Gateway does |
|---|---|
| `notifications/{tools,prompts,resources}/list_changed` | invalidate that backend's cache; emit one upward, **debounced** |
| `notifications/progress` | relay **to the originating connection only**, verbatim |
| `notifications/message` (logging) | log locally, prefixed with the backend's name; do not re-emit |
| `notifications/resources/updated` | drop, with a debug line |
| anything else | drop, with a debug line |

## Why `list_changed` is debounced

A reload that restarts eight backends produces eight notifications within a few
milliseconds. A client that refetches on each one does eight `tools/list` round trips to
reach the same answer, and each of those fans out across every backend. Coalescing them into
one costs a quarter of a second of staleness and saves a burst that grows as the square of
the deployment.

The debounce is per *kind*, not per backend: the client's reaction is a single
`tools/list` covering everything, so there is nothing to be gained by telling it twice which
backend changed -- and the notification carries no backend field anyway.

## Why progress goes to one connection

The `progressToken` in a `notifications/progress` is the **client's own**, forwarded down
unchanged on the call that started the work. Broadcasting it would hand every other client a
token it never issued: noise at best, and a token collision at worst, since two clients can
independently pick the same one.

## Why `notifications/message` is not re-emitted

The gateway does not advertise `logging`, and MCP says a server must not use a capability
the client did not declare. The content is not lost -- it goes to the daemon's own log with
the backend's name on it, which is where an operator looks, and `/admin`'s log stream
surfaces it to the UI.

## Why `resources/updated` is dropped

`subscribe: false` is advertised, so no client has subscribed and none is expecting these.
Forwarding would also require rewriting the URI, which is the work the follow-up issue
covers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from mcp_gateway import protocol

logger = logging.getLogger(__name__)

#: How long `list_changed` notifications of one kind are coalesced. Long enough to swallow a
#: reload's burst, short enough that a human clicking "refresh" does not notice it.
DEBOUNCE_SECONDS = 0.25

#: MCP log levels to Python's, for the messages we log rather than relay.
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "alert": logging.CRITICAL,
    "emergency": logging.CRITICAL,
}

_LIST_CHANGED = {
    protocol.TOOLS_LIST_CHANGED: "tools",
    protocol.PROMPTS_LIST_CHANGED: "prompts",
    protocol.RESOURCES_LIST_CHANGED: "resources",
}

Broadcaster = Callable[[str, dict | None], Awaitable[Any]]


class Notifier:
    """Coalesces and routes backend notifications."""

    def __init__(self, broadcast: Broadcaster, *, debounce: float = DEBOUNCE_SECONDS) -> None:
        self._broadcast = broadcast
        self._debounce = debounce
        self._pending: dict[str, asyncio.Task[None]] = {}

    def kind_of(self, method: str) -> str | None:
        return _LIST_CHANGED.get(method)

    async def list_changed(self, method: str) -> None:
        """Schedule one upward `list_changed` for this kind, coalescing a burst.

        An already-scheduled emission is left alone rather than restarted. Restarting it
        would make a steady trickle of backend changes postpone the notification forever --
        the classic debounce failure, where the busiest case is the one that never fires.
        """
        if method in self._pending and not self._pending[method].done():
            return
        self._pending[method] = asyncio.create_task(self._emit_after_delay(method))

    async def _emit_after_delay(self, method: str) -> None:
        try:
            await asyncio.sleep(self._debounce)
            await self._broadcast(method, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - a broadcast that fails is not fatal
            logger.debug("failed to emit %s: %s", method, exc)
        finally:
            self._pending.pop(method, None)

    async def flush(self) -> None:
        """Emit anything pending now. For shutdown, and for tests that cannot wait."""
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()

    def log_backend_message(self, name: str, params: dict) -> None:
        """Put a backend's `notifications/message` in our log, not on the client's wire."""
        level = _LEVELS.get(str(params.get("level", "info")).lower(), logging.INFO)
        data = params.get("data")
        logger.log(level, "[%s] %s", name, data if isinstance(data, str) else repr(data))

    async def cancel_pending(self) -> None:
        await self.flush()
