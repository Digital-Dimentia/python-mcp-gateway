"""JSON-RPC 2.0 message classification and framing. No MCP knowledge.

Used by all three transports: the daemon's WebSocket server, the backend stdio client, and
the bridge. Kept protocol-agnostic so the one thing every direction has to agree on -- what
counts as a request, a notification, or a response -- is decided once.

## Classification is by shape, not by a `type` field

JSON-RPC has no discriminator. A message is a request if it has a `method` and an `id`, a
notification if it has a `method` and no `id`, and a response if it has an `id` and one of
`result`/`error`. Getting that wrong in a proxy is how a notification ends up answered --
which a conforming peer treats as a protocol violation, because it never asked.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

VERSION = "2.0"


class Kind(Enum):
    REQUEST = "request"
    NOTIFICATION = "notification"
    RESPONSE = "response"
    INVALID = "invalid"


def classify(message: Any) -> Kind:
    """What kind of message this is, by shape.

    `id: null` is deliberately **not** a request. The spec reserves it for a response to a
    request whose id could not be determined, and treating it as an id would let a caller
    open an unanswerable correlation.
    """
    if not isinstance(message, dict):
        return Kind.INVALID
    has_id = "id" in message and message["id"] is not None
    method = message.get("method")
    if isinstance(method, str) and method:
        return Kind.REQUEST if has_id else Kind.NOTIFICATION
    if has_id and ("result" in message or "error" in message):
        return Kind.RESPONSE
    return Kind.INVALID


def request(request_id: Any, method: str, params: dict | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": VERSION, "id": request_id, "method": method}
    body["params"] = params if params is not None else {}
    return body


def notification(method: str, params: dict | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": VERSION, "method": method}
    if params is not None:
        body["params"] = params
    return body


def success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": VERSION, "id": request_id, "result": result}


def failure(request_id: Any, error: dict[str, Any]) -> dict[str, Any]:
    """An error response.

    `request_id` may be `None`, which is the spec's answer for a message so malformed that
    no id could be read from it -- a parse error, or a payload that is not an object.
    """
    return {"jsonrpc": VERSION, "id": request_id, "error": error}


def encode(message: dict[str, Any]) -> str:
    """Serialise one message.

    `ensure_ascii=False` because the wire is UTF-8 in both directions and escaping every
    non-ASCII character would inflate a tool result full of ordinary prose for no gain.
    No trailing newline: the stdio framing adds one, the WebSocket framing must not.
    """
    return json.dumps(message, ensure_ascii=False)


def decode(raw: str | bytes) -> Any:
    """Parse one frame. Raises `json.JSONDecodeError`, which the caller answers as -32700."""
    return json.loads(raw)


def request_id_of(message: Any) -> Any:
    """The id to answer, or `None` when there is nothing to correlate against."""
    if isinstance(message, dict):
        value = message.get("id")
        if value is not None:
            return value
    return None
