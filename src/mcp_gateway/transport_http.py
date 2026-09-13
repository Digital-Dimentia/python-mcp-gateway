"""MCP's Streamable HTTP transport, on the same port and socket as the WebSocket one.

A client that speaks Streamable HTTP -- Claude Code among them -- can attach to the gateway
directly, with no `mcp-gateway-connect` bridge in between. That is the whole point of this
module: the bridge exists only because there was nothing here.

## What this is not

It is not an ASGI application, and there is no framework under it. `pyproject.toml` says
why in more detail; the short version is that this process serves one protocol on one port
and the obvious stack would put two Rust-compiled per-architecture extensions into a daemon
that proxies raw dicts. What is here instead is an HTTP/1.1 request reader, a response
writer, and an SSE writer -- three small, exactly-scoped things, because that is all the
transport needs.

## How it shares the port

`websockets` owns the listening socket, and it hands every connection to a
`ServerConnection`, which is an `asyncio.Protocol`. `transport_ws.DivertingConnection`
subclasses it and looks at the first bytes: a WebSocket upgrade is passed to `super()`
untouched, so `/mcp` and `/admin` sockets keep every line of the library's handshake,
keepalive and close handling. Anything else is handed to `HttpProtocol` here, via
`transport.set_protocol`, with the buffered bytes replayed into it.

That divert is what the old `process_request` hook could not do. The hook returns one
complete `Response`, so it can neither stream an SSE body nor read a request body -- and a
POST has both, because a WebSocket handshake never does and the library has no reason to
read one.

## The shape of the conversation

One path, `/mcp`, and three methods:

| Method | Carries | Answer |
|---|---|---|
| `POST` | one JSON-RPC request | `200` with the response as `application/json` |
| `POST` | a notification, or a response to something we asked | `202` with no body |
| `GET` | nothing | an SSE stream of everything the server says unprompted |
| `DELETE` | nothing | `204`, and the session is gone |

**Responses come back on the POST, not on an SSE stream.** The spec permits either. Doing it
this way means the only streaming body in the process is the `GET`, which is also the only
place it buys anything: notifications and server-to-client requests have nowhere else to go,
while a response has a request still waiting to carry it.

**A server-to-client request goes out on the GET stream and its answer arrives as a POST.**
That is how `roots/list`, `sampling/createMessage` and `elicitation/create` work here, and
it is why a client that never opens the GET stream can still call tools but cannot be asked
anything -- exactly the same position as a WebSocket client whose `initialize` declared no
capabilities.

## Sessions

`Mcp-Session-Id` is minted on the `initialize` response and required on every request after
it. It has to exist because HTTP has no connection to hang state on and this transport's
state is not optional: the negotiated protocol version, the client's capabilities, its log
level and its resource subscriptions all belong to *a client*, not to a socket.

An unknown id is `404`, which the spec defines as "start again with `initialize`". That is
the honest answer after a daemon restart, and the alternative -- inventing a session for an
id we never issued -- would let a client believe a subscription survived that did not.

Sessions are dropped when the GET stream closes and nothing replaces it within
`SESSION_GRACE_SECONDS`, so a client that goes away without a `DELETE` does not leak one.
The grace is what makes a reconnecting client's subscriptions survive the gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from mcp_gateway import errors, jsonrpc

logger = logging.getLogger(__name__)

#: The one Streamable HTTP endpoint. Same path as the WebSocket one, which is not a clash:
#: a WebSocket client sends `Upgrade: websocket` and never reaches this module.
MCP_PATH = "/mcp"

#: Header carrying the session id, spelled as the spec spells it. Compared case-insensitively
#: like every other header, because HTTP says so and clients differ.
SESSION_HEADER = "mcp-session-id"

#: Header a client sends to say which revision it negotiated. Read and echoed; not enforced,
#: because the version was already agreed in `initialize` and refusing here would strand a
#: client over a header it got wrong rather than a protocol it got wrong.
VERSION_HEADER = "mcp-protocol-version"

#: How long a session outlives the GET stream that was carrying its notifications. Long
#: enough for a reconnect across a laptop sleeping or a network changing, short enough that
#: an abandoned session is not a leak.
SESSION_GRACE_SECONDS = 300.0

#: Request bodies are capped at the same size as a WebSocket message, so the two transports
#: do not disagree about how large a `tools/call` result may be.
MAX_BODY_BYTES = 50 * 1024 * 1024

#: How long an idle SSE stream waits before writing a comment frame. Proxies and NAT tables
#: drop a connection with nothing on it, and an SSE comment is the standard, ignorable way
#: to say the stream is still alive. Matches the WebSocket ping interval for the same reason.
SSE_KEEPALIVE_SECONDS = 20.0

_REASONS = {
    200: "OK",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    413: "Content Too Large",
    500: "Internal Server Error",
}


@dataclass(frozen=True)
class Head:
    """One parsed HTTP request line and its headers. The body is read separately."""

    method: str
    target: str
    headers: dict[str, str]

    @property
    def path(self) -> str:
        return urlsplit(self.target).path or "/"

    def get(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def content_length(self) -> int:
        try:
            return int(self.get("content-length", "0"))
        except ValueError:
            return -1

    def accepts(self, media_type: str) -> bool:
        """Whether `Accept` admits this type. A missing or `*/*` header admits everything.

        Deliberately forgiving: the spec asks clients to send both types on a POST, and a
        client that sends neither is far more likely to be an old one than a hostile one.
        Refusing it would trade a working conversation for a rule nothing depends on.
        """
        accept = self.get("accept")
        if not accept or "*/*" in accept:
            return True
        return media_type in accept


def parse_head(raw: bytes) -> Head | None:
    """Parse a request line and headers. `None` if this is not HTTP we can read.

    Repeated headers are joined with `, `, which is what RFC 9110 says a recipient may do
    for every header this module reads.
    """
    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes every byte
        return None
    lines = text.split("\r\n")
    request_line = lines[0].split(" ")
    if len(request_line) != 3 or not request_line[2].startswith("HTTP/"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        if not _:
            return None
        key = name.strip().lower()
        value = value.strip()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return Head(method=request_line[0].upper(), target=request_line[1], headers=headers)


def response_bytes(
    status: int, body: bytes = b"", content_type: str = "", extra: dict[str, str] | None = None
) -> bytes:
    """One complete HTTP/1.1 response. `Connection: close` throughout.

    Keep-alive is not implemented and saying so is the point: a client that pipelined a
    second request onto a connection we are about to close would lose it silently. One
    request per connection costs a TCP handshake against a loopback socket, which is nothing
    next to the class of bug the alternative invites.
    """
    headers = {
        "Connection": "close",
        "Content-Length": str(len(body)),
        **(extra or {}),
    }
    if content_type:
        headers["Content-Type"] = content_type
    head = f"HTTP/1.1 {status} {_REASONS.get(status, 'Unknown')}\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return head.encode("latin-1") + b"\r\n" + body


def sse_head_bytes(extra: dict[str, str] | None = None) -> bytes:
    """The head of an SSE response, after which the body is written frame by frame."""
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        "Connection": "close",
        **(extra or {}),
    }
    head = "HTTP/1.1 200 OK\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return head.encode("latin-1") + b"\r\n"


def sse_frame(data: str, event_id: int | None = None) -> bytes:
    """One SSE event. Every line of `data` is prefixed, which is what makes it framing.

    A JSON-RPC message has no newlines once encoded, so the multi-line branch is defensive
    rather than exercised -- but an SSE writer that splits on the wrong thing produces a
    stream that parses as valid and means something else, which is the worst failure this
    file could have.
    """
    out = "" if event_id is None else f"id: {event_id}\n"
    out += "".join(f"data: {line}\n" for line in data.split("\n"))
    return (out + "\n").encode("utf-8")


def new_session_id() -> str:
    """An unguessable session id. `token_urlsafe`, because the id is a bearer token.

    Anyone holding it can drive the session -- read its subscriptions, answer requests it
    asked -- so a counter or a UUID1 would be a credential you can predict from the last one.
    """
    return secrets.token_urlsafe(24)


class HttpLink:
    """One Streamable HTTP client, as the layers above it see a connection.

    Deliberately the same shape as `transport_ws.ClientLink` -- `send`, `notify`, `path`,
    `remote` -- because that is the whole interface `Session`, `Gateway` and the notifier
    reach for. Neither of them learns that a client arrived over HTTP, which is what stops
    the transport split from spreading upward.

    What is different is where an outbound message goes. A WebSocket link always has a
    socket; this one has one only while the GET stream is open, so anything sent before or
    between streams is buffered. That buffer is bounded and drops its *oldest* entry: a
    client returning after a gap is better served by the most recent state than by a
    backlog it will discard anyway.
    """

    path = MCP_PATH

    def __init__(self, session_id: str, remote: Any, *, backlog: int = 64) -> None:
        self.session_id = session_id
        self.remote = remote
        self._stream: SseStream | None = None
        self._pending: list[dict[str, Any]] = []
        self._backlog = backlog
        self._lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            if self._stream is not None and self._stream.writable:
                self._stream.write(message)
                return
            self._pending.append(message)
            if len(self._pending) > self._backlog:
                dropped = self._pending.pop(0)
                logger.debug("dropped a queued %s: no GET stream", dropped.get("method", "response"))

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self.send(jsonrpc.notification(method, params))

    async def attach(self, stream: SseStream) -> None:
        """Take over as this session's outbound stream, and flush what was waiting."""
        async with self._lock:
            if self._stream is not None:
                self._stream.close()
            self._stream = stream
            for message in self._pending:
                stream.write(message)
            self._pending.clear()

    async def detach(self, stream: SseStream) -> None:
        """The GET went away. Only clears it if it is still the current one -- a reconnect
        that raced its predecessor's teardown must not unhook the stream that replaced it."""
        async with self._lock:
            if self._stream is stream:
                self._stream = None

    @property
    def streaming(self) -> bool:
        return self._stream is not None and self._stream.writable

    async def close(self) -> None:
        async with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


class SseStream:
    """The writing half of one `GET /mcp`: an event id counter and a transport."""

    def __init__(self, transport: asyncio.Transport) -> None:
        self._transport = transport
        self._next_id = 0
        self._closed = False

    @property
    def writable(self) -> bool:
        return not self._closed and not self._transport.is_closing()

    def write(self, message: dict[str, Any]) -> None:
        if not self.writable:
            return
        self._next_id += 1
        self._transport.write(sse_frame(jsonrpc.encode(message), self._next_id))

    def keepalive(self) -> None:
        """An SSE comment. Ignored by every parser, and enough to hold a proxy open."""
        if self.writable:
            self._transport.write(b": keepalive\n\n")

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            self._transport.close()


@dataclass
class HttpSession:
    """One client's state between requests, which is the thing HTTP does not give us."""

    link: HttpLink
    connection: Any
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_seen = time.time()


class StreamableHttp:
    """The `/mcp` endpoint: sessions, and what each method does to one.

    Holds no socket. It is handed a parsed request and a writer, which is what lets every
    rule here be tested without a network -- and the rules are the part with decisions in
    them, while the socket is the part with none.
    """

    def __init__(
        self,
        connection_factory: Callable[[HttpLink], Any],
        *,
        grace: float = SESSION_GRACE_SECONDS,
    ) -> None:
        self._factory = connection_factory
        self._grace = grace
        self._sessions: dict[str, HttpSession] = {}

    # --- the registry --------------------------------------------------------------------

    @property
    def links(self) -> frozenset[HttpLink]:
        """Every live session's link, for `describe_connections`."""
        return frozenset(entry.link for entry in self._sessions.values())

    def get(self, session_id: str) -> HttpSession | None:
        return self._sessions.get(session_id)

    def reap(self) -> list[str]:
        """Forget sessions whose stream has been gone longer than the grace. Returns ids.

        A client that vanishes without a `DELETE` is the ordinary case -- a laptop closing,
        a process killed -- so this is the path most sessions actually end on, not an
        exceptional one.
        """
        cutoff = time.time() - self._grace
        stale = [
            sid
            for sid, entry in self._sessions.items()
            if not entry.link.streaming and entry.last_seen < cutoff
        ]
        for sid in stale:
            logger.info("http session %s expired after %.0fs idle", sid[:8], self._grace)
        return stale

    async def drop(self, session_id: str) -> HttpSession | None:
        entry = self._sessions.pop(session_id, None)
        if entry is not None:
            await entry.link.close()
        return entry

    # --- the methods ---------------------------------------------------------------------

    async def post(self, head: Head, body: bytes, remote: Any) -> tuple[int, bytes, dict[str, str]]:
        """Answer one `POST /mcp`. Returns `(status, body, extra headers)`.

        Three shapes go in and three come out, and which is which is decided by the message
        rather than by the caller: a request gets its response, a notification and a response
        get `202`, and anything unparseable gets a JSON-RPC error with a `200` around it --
        because a parse error *is* the answer to a well-formed HTTP request, and reporting it
        as `400` would put it somewhere a JSON-RPC client is not looking.
        """
        if not head.accepts("application/json"):
            return 406, b"This endpoint answers application/json\n", {}
        try:
            message = jsonrpc.decode(body.decode("utf-8"))
        except Exception:
            return self._json(
                jsonrpc.failure(None, errors.error_object(errors.PARSE_ERROR, "invalid JSON"))
            )

        kind = jsonrpc.classify(message)
        method = message.get("method")

        # `initialize` is the one request that may arrive with no session, because it is
        # what mints one. Everything else on an unknown id is 404: the spec's way of saying
        # "start again", and the honest answer after a daemon restart.
        if method == "initialize" and not head.get(SESSION_HEADER):
            return await self._initialize(message, remote)

        entry = self._sessions.get(head.get(SESSION_HEADER))
        if entry is None:
            return 404, b"Unknown or expired session; send initialize again\n", {}
        entry.touch()

        if kind is jsonrpc.Kind.RESPONSE:
            # An answer to something *we* asked over the GET stream.
            entry.connection.gateway.resolve_client_response(entry.connection, message)
            return 202, b"", {}
        if kind is jsonrpc.Kind.NOTIFICATION:
            await entry.connection.handle(message)
            return 202, b"", {}
        if kind is jsonrpc.Kind.INVALID:
            return self._json(
                jsonrpc.failure(
                    message.get("id"),
                    errors.error_object(errors.INVALID_REQUEST, "not a JSON-RPC message"),
                )
            )

        answer = await self._answer(entry, message)
        return self._json(answer) if answer is not None else (202, b"", {})

    # `Connection.handle` returns a *result payload*, not an envelope -- the WebSocket read
    # loop wraps it in `jsonrpc.success` and this has to do the same, or a client gets a
    # bare result with no `id` to match it against. Both paths therefore agree on what a
    # connection returns, which is what keeps `Session` ignorant of its transport.

    async def _initialize(self, message: dict, remote: Any) -> tuple[int, bytes, dict[str, str]]:
        session_id = new_session_id()
        link = HttpLink(session_id, remote)
        entry = HttpSession(link=link, connection=self._factory(link))
        self._sessions[session_id] = entry
        logger.info("http session %s opened from %s", session_id[:8], remote)
        answer = await self._answer(entry, message)
        if answer is None:  # pragma: no cover - initialize is always a request
            return 202, b"", {}
        # The id goes on the response the client is already reading, which is the only
        # moment it can learn one. Dropping the session if initialize failed keeps a
        # rejected handshake from leaving a usable id behind.
        if "error" in answer:  # an envelope now, so this is the JSON-RPC error member
            await self.drop(session_id)
            return self._json(answer)
        return self._json(answer, {"Mcp-Session-Id": session_id})

    async def _answer(self, entry: HttpSession, message: dict) -> dict | None:
        request_id = jsonrpc.request_id_of(message)
        try:
            answer = await entry.connection.handle(message)
        except errors.GatewayErrorObject as refusal:
            return jsonrpc.failure(request_id, refusal.error)
        except Exception as exc:
            # Something above forgot `@as_error_object`. Answer anyway: a client waiting
            # forever is worse than a generic code, and the log says it was our bug.
            logger.exception("unmapped error serving %s over http", message.get("method"))
            return jsonrpc.failure(request_id, errors.to_error_object(exc))
        if request_id is None or answer is None:
            return None
        return jsonrpc.success(request_id, answer)

    @staticmethod
    def _json(payload: dict, extra: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
        return 200, jsonrpc.encode(payload).encode("utf-8"), extra or {}


class HttpProtocol(asyncio.Protocol):
    """A connection `transport_ws` decided is not a WebSocket, speaking HTTP.

    Reads one request, answers it, closes. The exception is `GET /mcp`, which is the SSE
    stream and stays open until the client or the daemon closes it -- that connection is the
    only long-lived one this module has, and the only reason it exists.

    One request per connection. See `response_bytes` for why keep-alive is absent rather
    than merely unimplemented.
    """

    def __init__(
        self,
        endpoint: StreamableHttp,
        *,
        authorise: Callable[[Head], tuple[int, bytes] | None],
        remote: Any,
        on_stream: Callable[[HttpLink | None], None] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._authorise = authorise
        self._remote = remote
        self._on_stream = on_stream
        self._buffer = bytearray()
        self._head: Head | None = None
        self._transport: asyncio.Transport | None = None
        self._stream: SseStream | None = None
        self._link: HttpLink | None = None
        self._task: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None

    # --- asyncio.Protocol ------------------------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        if self._task is not None:
            # A pipelined second request, or a body arriving after we started. The first is
            # unsupported by design, the second cannot happen because the body is read
            # before dispatch. Either way there is nothing to do with these bytes.
            return
        self._buffer += data
        if self._head is None:
            end = self._buffer.find(b"\r\n\r\n")
            if end < 0:
                if len(self._buffer) > 64 * 1024:
                    self._fail(400, b"Request headers too large\n")
                return
            self._head = parse_head(bytes(self._buffer[:end]))
            del self._buffer[: end + 4]
            if self._head is None:
                self._fail(400, b"Malformed request\n")
                return
        length = self._head.content_length
        if length < 0:
            self._fail(400, b"Malformed Content-Length\n")
            return
        if length > MAX_BODY_BYTES:
            self._fail(413, b"Request body too large\n")
            return
        if len(self._buffer) < length:
            return
        self._task = asyncio.create_task(self._dispatch(self._head, bytes(self._buffer[:length])))

    def connection_lost(self, exc: BaseException | None) -> None:
        for task in (self._keepalive,):
            if task is not None:
                task.cancel()
        if self._stream is not None and self._link is not None:
            stream, link = self._stream, self._link
            self._stream = None
            asyncio.create_task(self._detach(link, stream))

    async def _detach(self, link: HttpLink, stream: SseStream) -> None:
        await link.detach(stream)
        if self._on_stream is not None:
            self._on_stream(None)

    # --- the request -------------------------------------------------------------------------

    async def _dispatch(self, head: Head, body: bytes) -> None:
        try:
            refusal = self._authorise(head)
            if refusal is not None:
                self._fail(refusal[0], refusal[1])
                return
            if head.method == "POST":
                status, payload, extra = await self._endpoint.post(head, body, self._remote)
                self._write(response_bytes(status, payload, "application/json", extra))
                return
            if head.method == "GET":
                await self._open_stream(head)
                return
            if head.method == "DELETE":
                entry = await self._endpoint.drop(head.get(SESSION_HEADER))
                if entry is None:
                    self._fail(404, b"Unknown session\n")
                    return
                await entry.connection.closed()
                self._write(response_bytes(204))
                return
            self._fail(405, b"Use POST, GET or DELETE\n")
        except Exception:
            logger.exception("http dispatch failed")
            self._fail(500, b"Internal error\n")

    async def _open_stream(self, head: Head) -> None:
        """`GET /mcp`: everything the server says unprompted, until the client goes away."""
        if not head.accepts("text/event-stream"):
            self._fail(406, b"This endpoint streams text/event-stream\n")
            return
        entry = self._endpoint.get(head.get(SESSION_HEADER))
        if entry is None:
            self._fail(404, b"Unknown or expired session; send initialize again\n", )
            return
        entry.touch()
        assert self._transport is not None
        self._transport.write(sse_head_bytes({"Mcp-Session-Id": entry.link.session_id}))
        self._stream = SseStream(self._transport)
        self._link = entry.link
        await entry.link.attach(self._stream)
        if self._on_stream is not None:
            self._on_stream(entry.link)
        self._keepalive = asyncio.create_task(self._hold_open(self._stream))

    async def _hold_open(self, stream: SseStream) -> None:
        """Write a comment into an idle stream, so a proxy does not decide it is dead."""
        try:
            while stream.writable:
                await asyncio.sleep(SSE_KEEPALIVE_SECONDS)
                stream.keepalive()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - a dead socket is the ordinary end
            logger.debug("sse keepalive stopped: %s", exc)

    # --- writing ------------------------------------------------------------------------------

    def _write(self, raw: bytes) -> None:
        if self._transport is None or self._transport.is_closing():
            return
        self._transport.write(raw)
        self._transport.close()

    def _fail(self, status: int, body: bytes) -> None:
        self._write(response_bytes(status, body, "text/plain; charset=utf-8"))
