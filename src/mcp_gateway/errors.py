"""Map exceptions onto JSON-RPC error objects, and keep backend errors honest.

A message is a concise sentence; structured detail goes in `data`.

## The `source` discriminator

This is the contract the whole module exists to hold.

A gateway sits between two peers that both speak JSON-RPC, and the codes are not
namespaced. Forwarding a backend's `-32601` unchanged would leave a client unable to tell
"this gateway has no such method" from "the backend behind it has no such method" -- two
problems with completely different fixes.

So: an error **forwarded from a backend always carries `data["source"] == "mcp"`**, along
with the backend's name, its original code, and its original `data` if it sent one. An
error the gateway **originates never sets `source`**. One key, checked one way, and the
ambiguity is gone.

The corollary is the part that is easy to get wrong: a `MCPProtocolError` with **no** code
-- a timeout, a dead transport, a malformed result -- is *ours*, not the backend's. It
becomes `-32603` with no `source`, because tagging it would claim the backend produced a
code it never sent, which is the exact fidelity loss this module prevents.

## Messages are forwarded verbatim

A backend that failed has already written a concise sentence describing what failed, and
it knows things we do not. Replacing it with "backend error" destroys the only useful
description in the exchange. We add context in `data`; we never rewrite the message.

## Cancellation is not an error

`asyncio.CancelledError` is re-raised, never mapped. Swallowing it turns a cancelled task
into a completed one, which breaks cancellation everywhere above -- including the
`_abandon` path that tells a backend to stop working on a reply nobody will read.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

# The JSON-RPC 2.0 codes, plus the two MCP adds.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: MCP's dedicated code for a resource URI that names nothing readable.
RESOURCE_NOT_FOUND = -32002
#: MCP's code for a request abandoned after `notifications/cancelled`.
REQUEST_CANCELLED = -32800

#: The value of `data["source"]` on a forwarded backend error, and only there.
BACKEND_SOURCE = "mcp"


class GatewayError(Exception):
    """An error the gateway itself originates. Never carries `source`."""

    def __init__(self, message: str, *, code: int = INTERNAL_ERROR, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})

    def to_error_object(self) -> dict[str, Any]:
        return error_object(self.code, str(self), self.data)


class MethodNotFound(GatewayError):
    def __init__(self, method: str, data: dict | None = None):
        super().__init__(f"unknown method {method!r}", code=METHOD_NOT_FOUND, data=data)


class InvalidParams(GatewayError):
    def __init__(self, message: str, data: dict | None = None):
        super().__init__(message, code=INVALID_PARAMS, data=data)


class ResourceNotFound(GatewayError):
    def __init__(self, message: str, data: dict | None = None):
        super().__init__(message, code=RESOURCE_NOT_FOUND, data=data)


def error_object(code: int, message: str, data: dict | None = None) -> dict[str, Any]:
    """Build one JSON-RPC error object.

    `data` is **omitted when empty** rather than emitted as `null` or `{}`. Its presence is
    meaningful -- a client checks `data.get("source")` -- so an always-present empty object
    would make "the gateway said nothing extra" indistinguishable from "there is a `data`
    here to inspect".
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return error


def backend_error_object(
    backend: str, code: int | None, message: str, data: Any = None
) -> dict[str, Any]:
    """Forward a backend's error, tagged so a client can tell whose it was.

    When `code` is `None` the backend never produced one -- the failure is a timeout or a
    dead transport, which is ours. See the module docstring: that case comes back as
    `-32603` **without** `source`.
    """
    if code is None:
        return error_object(INTERNAL_ERROR, message, {"backend": backend, "reason": message})
    payload: dict[str, Any] = {
        "source": BACKEND_SOURCE,
        "backend": backend,
        "mcpCode": code,
    }
    if data is not None:
        payload["mcpData"] = data
    return error_object(code, message, payload)


#: Duck-typed marker for "this exception came off a backend's wire". `MCPProtocolError`
#: sets it; nothing else does.
#:
#: A marker rather than `isinstance(exc, MCPProtocolError)` so that this module imports
#: nothing from the transport layer. `errors` is imported by `config` and `naming`, which
#: have no business dragging in a subprocess client, and an import-local `from
#: mcp_gateway.mcp_stdio import ...` inside the mapping function would put that cost on
#: every error instead -- and make the two modules import each other in a cycle the moment
#: `mcp_stdio` wants to build an error object of its own.
BACKEND_EXCEPTION_MARKER = "mcp_error"


def to_error_object(exc: BaseException, *, backend: str | None = None) -> dict[str, Any]:
    """Map any exception to a JSON-RPC error object."""
    if isinstance(exc, asyncio.CancelledError):
        # Deliberately not handled. See the module docstring.
        raise exc
    if isinstance(exc, GatewayError):
        return exc.to_error_object()
    if getattr(exc, BACKEND_EXCEPTION_MARKER, False):
        return backend_error_object(
            backend or getattr(exc, "backend", None) or "?",
            getattr(exc, "code", None),
            str(exc),
            getattr(exc, "data", None),
        )
    if isinstance(exc, ValueError):
        # Covers NamingError and ConfigError, both of which describe a malformed argument.
        return error_object(INVALID_PARAMS, str(exc))
    return error_object(INTERNAL_ERROR, str(exc) or exc.__class__.__name__)


def as_error_object(method):
    """Wrap a handler so any exception becomes an error object instead of escaping.

    `maps_errors` is set on the wrapper so a test can assert that every registered handler
    carries the decorator, rather than trusting that nobody forgot one.
    """

    @functools.wraps(method)
    async def wrapper(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("%s failed: %s", getattr(method, "__name__", method), exc)
            raise GatewayErrorObject(to_error_object(exc)) from exc

    wrapper.maps_errors = True
    return wrapper


class GatewayErrorObject(Exception):
    """Carries an already-built error object up to the transport that will write it.

    A separate type so the transport can tell "this has already been mapped, send it" from
    "this escaped unmapped, and something above forgot the decorator".
    """

    def __init__(self, error: dict[str, Any]):
        super().__init__(error.get("message", ""))
        self.error = error
