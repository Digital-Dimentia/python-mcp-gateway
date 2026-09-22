"""An MCP session with a backend that is a URL rather than a process.

`MCPClient` in `mcp_stdio.py` owns everything above the byte stream: ids, pending futures,
the handshake, pagination, cancellation and server requests. This module is only the
transport underneath it -- MCP's Streamable HTTP, spoken as a client. See `mcp_http.md` for
the conversation, and for why there is an HTTP/1.1 client here rather than a dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit

from mcp_gateway import __version__, protocol
from mcp_gateway.mcp_stdio import MCPClient, MCPProtocolError, clip_middle

logger = logging.getLogger("mcp_gateway.mcp_http")

#: The list notifications a renewed session stands in for. A backend that lost our session
#: was very likely restarted, and nothing says it came back publishing the same things --
#: so after a renewal the gateway is told each listing may have changed, exactly as if the
#: server had said so itself. See "A session that expires" in `mcp_http.md`.
_LIST_CHANGED = {
    "tools": "notifications/tools/list_changed",
    "prompts": "notifications/prompts/list_changed",
    "resources": "notifications/resources/list_changed",
}

#: Request headers this module writes itself, which a `headers:` block may therefore not
#: set. Framing headers would let a config line break the request; the MCP ones would let it
#: impersonate a session. Lower-case, compared case-insensitively.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "accept",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
    }
)


#: How much of an error body is carried into a failure, and how much of that is taken from
#: the front. See `clip_middle`.
_BODY_CLIP_WIDTH = 600
_BODY_CLIP_HEAD = 380


class _HttpError(Exception):
    """A response that is not the one the conversation needed. Never leaves this module."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status


@dataclass
class _Response:
    """One response, head read and body not yet. Owns its connection, which it closes."""

    status: int
    headers: dict[str, str]
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    async def chunks(self, limit: int) -> AsyncIterator[bytes]:
        """The body, de-framed, as it arrives. Stops at the end of the body or of the socket.

        `limit` caps any single declared length -- a chunk or a Content-Length -- so a
        server cannot make this allocate whatever it names.
        """
        reader = self.reader
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = await reader.readuntil(b"\r\n")
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    return
                if size > limit:
                    raise MCPProtocolError(f"HTTP chunk of {size} bytes exceeds {limit}")
                data = await reader.readexactly(size)
                await reader.readexactly(2)
                yield data
        elif "content-length" in self.headers:
            remaining = int(self.headers["content-length"])
            if remaining > limit:
                raise MCPProtocolError(f"HTTP body of {remaining} bytes exceeds {limit}")
            if remaining:
                yield await reader.readexactly(remaining)
        else:
            # Neither: the body runs to the end of the connection, which we asked to close.
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                yield data

    async def body(self, limit: int) -> bytes:
        parts: list[bytes] = []
        total = 0
        async for chunk in self.chunks(limit):
            total += len(chunk)
            if total > limit:
                raise MCPProtocolError(f"HTTP body exceeds {limit} bytes")
            parts.append(chunk)
        return b"".join(parts)

    async def events(self, limit: int) -> AsyncIterator[tuple[str | None, str]]:
        """Parse the body as Server-Sent Events, yielding `(id, data)` per message event.

        Only unnamed events and ones named `message` carry JSON-RPC; anything else a
        server sends on the stream is not addressed to an MCP client and is skipped.
        Comment lines -- a keepalive is one -- fall out of the same rule.
        """
        buffer = b""
        data: list[str] = []
        event_id: str | None = None
        name = ""
        async for chunk in self.chunks(limit):
            buffer += chunk
            if len(buffer) > limit:
                raise MCPProtocolError(f"SSE event exceeds {limit} bytes")
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                if not line:
                    if data and name in ("", "message"):
                        yield event_id, "\n".join(data)
                    data, name = [], ""
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if key == "data":
                    data.append(value)
                elif key == "event":
                    name = value
                elif key == "id":
                    event_id = value

    def close(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()


@dataclass
class MCPHttpClient(MCPClient):
    """An MCP session over Streamable HTTP: one POST per message, SSE where the server wants.

    `headers` holds **resolved** values -- a credential, usually -- and so is never logged,
    never returned, and never compared anywhere but in `supervisor.spawn_identity`'s digest.
    """

    url: str = field(kw_only=True)
    headers: Mapping[str, str] = field(default_factory=dict)
    #: For `https`: `None` means a default context, which trusts the system store and
    #: honours `SSL_CERT_FILE` the way OpenSSL always does.
    ssl_context: ssl.SSLContext | None = None

    # Per message, the same ceiling the stdio reader puts on a line.
    _MESSAGE_LIMIT = 8 * 1024 * 1024
    # How long `stop` gives the server to acknowledge the session's DELETE.
    _DELETE_TIMEOUT = 2.0
    # The GET stream's reconnect delay grows to this and stays there.
    _LISTEN_RETRY_CAP = 30.0

    def __post_init__(self) -> None:
        super().__post_init__()
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {self.url!r}")
        self._tls = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port or (443 if self._tls else 80)
        default_port = self._port == (443 if self._tls else 80)
        host = f"[{self._host}]" if ":" in self._host else self._host
        self._host_header = host if default_port else f"{host}:{self._port}"
        self._path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        #: What the server named this session on the `initialize` response. `None` both
        #: before the handshake and for a server that does not use sessions.
        self.session_id: str | None = None
        self._started = False
        self._closed = False
        #: One task per request in flight, keyed by its id, each reading that request's
        #: response. Keyed so `cancel_request` can hang up on the one it abandons.
        self._exchanges: dict[Any, asyncio.Task[None]] = {}
        #: Fire-and-forget work -- a renewal a notification's 404 asked for -- held so it is
        #: not collected mid-flight and so `stop` can cancel it.
        self._background: set[asyncio.Task[None]] = set()
        self._listener: asyncio.Task[None] | None = None
        self._renew_lock = asyncio.Lock()
        self._session_lost = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        # Nothing to open: HTTP has no connection to hold, and `initialize` is the first
        # thing that can tell a reachable server from an unreachable one.
        if self._closed:
            raise MCPProtocolError("MCP HTTP session closed")
        self._started = True

    async def initialize(self) -> dict[str, Any]:
        try:
            result = await super().initialize()
        except MCPProtocolError as exc:
            hint = await self._legacy_sse_hint()
            if hint is None:
                raise
            raise MCPProtocolError(hint) from exc
        self._start_listener()
        return result

    async def _legacy_sse_hint(self) -> str | None:
        """Was that a server speaking the 2024-11-05 HTTP+SSE transport rather than a dead one?

        Only ever asked after the handshake has already failed, and only to say something
        better than `HTTP 405` -- a `url` that answers a POST with a refusal and a GET with
        an `endpoint` event is not broken, it is old, and an operator who cannot tell those
        apart reads the same four lines of `curl` output the next person does.

        Bounded hard: one GET, a couple of seconds, the first few kilobytes. The old
        transport announces its POST endpoint in the stream's first event, so nothing later
        would change the answer, and this runs on the path where a backend is already
        failing to start.
        """
        try:
            response = await asyncio.wait_for(self._send("GET", None), timeout=2.0)
        except (OSError, asyncio.TimeoutError, MCPProtocolError):
            return None
        try:
            if response.status != 200 or response.content_type != "text/event-stream":
                return None
            prefix = await asyncio.wait_for(self._read_prefix(response), timeout=2.0)
        except (OSError, asyncio.TimeoutError, MCPProtocolError, asyncio.IncompleteReadError):
            return None
        finally:
            response.close()
        # `events()` drops this one: it is named, and named events carry no JSON-RPC. Here
        # the name *is* the evidence, so the raw lines are what get looked at.
        if not any(line.strip() == "event: endpoint" for line in prefix.splitlines()):
            return None
        return (
            f"{self.url} speaks the deprecated HTTP+SSE transport (MCP 2024-11-05), which "
            "this gateway does not; front it with a Streamable HTTP proxy and point `url` "
            "at that"
        )

    async def _read_prefix(self, response: _Response, limit: int = 4096) -> str:
        parts: list[bytes] = []
        total = 0
        async for chunk in response.chunks(self._MESSAGE_LIMIT):
            parts.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
        return b"".join(parts).decode("utf-8", errors="replace")

    async def stop(self) -> None:
        if not self._started or self._closed:
            self._closed = True
            return
        self._closed = True
        await self._cancel_server_requests()
        tasks = [*self._exchanges.values(), *self._background]
        if self._listener is not None:
            tasks.append(self._listener)
        self._exchanges.clear()
        self._background.clear()
        self._listener = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 -- logged; teardown must finish
                logger.debug("MCP HTTP task failed on shutdown", exc_info=True)
        await self._end_session()
        self._fail_pending(MCPProtocolError("MCP HTTP session closed"))

    async def _end_session(self) -> None:
        """Tell the server the session is over. A courtesy: 405 and silence are both fine."""
        if self.session_id is None:
            return
        try:
            response = await asyncio.wait_for(
                self._send("DELETE", None), timeout=self._DELETE_TIMEOUT
            )
            response.close()
        except (OSError, asyncio.TimeoutError, MCPProtocolError) as exc:
            logger.debug("MCP HTTP session DELETE to %s failed: %s", self.url, exc)
        self.session_id = None

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def _write(self, payload: dict[str, Any]) -> None:
        """Send one message. A request is handed to a task; anything else waits for its `202`.

        **A request does not wait here**, because `request` holds the write lock around this
        call and a tool call can take minutes: a task of its own sends it and reads the
        response, which resolves the pending future like any other inbound message -- and
        any failure along the way settles that future too, so nothing is left to time out.

        Everything else -- a notification, or our answer to a server's request -- waits for
        its acknowledgement, so `notifications/initialized` has landed before the first
        request after it is sent. A server that refuses requests before it has been told
        the handshake is over is conforming, and a race here would be ours.
        """
        if not self._started or self._closed:
            raise MCPProtocolError("MCP HTTP session not running")
        if "id" in payload and "method" in payload:
            request_id = payload["id"]
            task = asyncio.create_task(self._exchange(payload))
            self._exchanges[request_id] = task
            task.add_done_callback(lambda done, rid=request_id: self._forget(rid, done))
            return
        try:
            response = await asyncio.wait_for(
                self._send("POST", payload), timeout=self.request_timeout
            )
            try:
                await asyncio.wait_for(self._accept_ack(response), timeout=self.request_timeout)
            finally:
                response.close()
        except (OSError, asyncio.TimeoutError) as exc:
            raise MCPProtocolError(f"cannot reach {self.url}: {exc}") from exc
        except _HttpError as exc:
            if exc.status == 404 and self.session_id is not None:
                self._renew_later()
            raise MCPProtocolError(f"{self.url}: {exc}") from exc

    def _forget(self, request_id: Any, task: asyncio.Task[None]) -> None:
        if self._exchanges.get(request_id) is task:
            del self._exchanges[request_id]

    async def _accept_ack(self, response: _Response) -> None:
        """What a notification or a response is answered with: `202`, or any 2xx at a push."""
        if 200 <= response.status < 300:
            return
        raise _HttpError(response.status, await self._error_detail(response))

    async def cancel_request(self, request_id: int, reason: str | None = None) -> None:
        # The notification first, then the hang-up: MCP says closing a stream is not a
        # cancellation, so without the notification the server keeps working regardless.
        await super().cancel_request(request_id, reason)
        task = self._exchanges.pop(request_id, None)
        if task is not None:
            task.cancel()

    async def _exchange(self, payload: dict[str, Any]) -> None:
        """Send one request, read its response -- JSON or an SSE stream -- and route it.

        Whatever else happens, the request's future is settled before this returns: an
        unreachable server, a stream that ends early, a status that is not a reply, and a
        server that lost our session each become an `MCPProtocolError` on the caller rather
        than a wait for `request_timeout`.
        """
        request_id = payload["id"]
        method = payload.get("method")
        retried = False
        try:
            if method != "initialize":
                if self._session_lost:
                    # An earlier renewal failed. Try again before sending into no session.
                    await self._renew_session(stale=None)
                elif self._renew_lock.locked():
                    # A renewal is in flight, and sending now would go out with no session
                    # id at all. Wait for the new one instead.
                    async with self._renew_lock:
                        pass
            while True:
                # The id this attempt goes out with, not whatever `session_id` says by the
                # time a 404 comes back: two requests refused together must agree on which
                # session was lost, or the second renews the first one's replacement.
                sent_with = self.session_id
                response = await self._send("POST", payload)
                try:
                    await self._consume(payload, response)
                    break
                except _HttpError as exc:
                    renewable = sent_with is not None or self._session_lost
                    if exc.status != 404 or method == "initialize" or retried or not renewable:
                        raise
                    # The server does not know our session any more: it restarted, or it
                    # expires idle ones. MCP's answer is a fresh `initialize`, and then
                    # this request again, once, in the new session.
                    retried = True
                    await self._renew_session(stale=sent_with)
                finally:
                    response.close()
            if request_id in self._pending:
                raise MCPProtocolError(f"{method}: the response stream ended without a reply")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- every failure settles the caller's future
            if isinstance(exc, MCPProtocolError):
                error = exc
            elif isinstance(exc, (OSError, asyncio.TimeoutError)):
                error = MCPProtocolError(f"cannot reach {self.url}: {exc}")
            else:
                error = MCPProtocolError(f"{self.url}: {exc}")
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_exception(error)

    async def _consume(self, payload: dict[str, Any], response: _Response) -> None:
        if payload.get("method") == "initialize" and response.status == 200:
            self._adopt_session(response.headers.get("mcp-session-id"))
        kind = response.content_type
        if response.status == 200 and kind == "text/event-stream":
            async for _event_id, data in response.events(self._MESSAGE_LIMIT):
                await self._deliver(data)
                if payload["id"] not in self._pending:
                    # Our answer has arrived. MCP lets the server close here, and it lets us
                    # stop listening: anything it sends later has the GET stream to go to.
                    return
            return
        if response.status == 200 and kind == "application/json":
            await self._deliver((await response.body(self._MESSAGE_LIMIT)).decode("utf-8"))
            return
        # Not on a 404: that one is about the session, and it is retried rather than
        # answered -- a JSON-RPC error in its body must not reach the caller first.
        detail = await self._error_detail(response, deliver=response.status != 404)
        raise _HttpError(response.status, detail)

    async def _error_detail(self, response: _Response, *, deliver: bool = False) -> str:
        """A refusal's body, if short and readable -- and a JSON-RPC error is delivered.

        Some servers answer a bad request with `400` and a JSON-RPC error carrying the id,
        which is a better message than the status line; routing it lets it reach the
        caller as the server's own error.
        """
        try:
            body = await asyncio.wait_for(response.body(64 * 1024), timeout=2.0)
        except (asyncio.TimeoutError, OSError, MCPProtocolError, asyncio.IncompleteReadError):
            return ""
        text = body.decode("utf-8", errors="replace").strip()
        if response.content_type == "application/json":
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                message = None
            if deliver and isinstance(message, dict) and "error" in message:
                if message.get("id") in self._pending:
                    await self._handle_message(message)
        # The same clip the stdio client gives a stderr line, for the same reason: what a
        # server puts in an error body ends with the part that explains it.
        return clip_middle(text, width=_BODY_CLIP_WIDTH, head=_BODY_CLIP_HEAD)

    async def _deliver(self, data: str) -> None:
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON MCP message from %s", self.url)
            return
        for item in message if isinstance(message, list) else [message]:
            if isinstance(item, dict):
                await self._handle_message(item)

    def _adopt_session(self, value: str | None) -> None:
        if value is None:
            self.session_id = None
            return
        # MCP: visible ASCII only. Anything else would be a header we then cannot send.
        if not value or any(not 0x21 <= ord(ch) <= 0x7E for ch in value):
            raise MCPProtocolError(f"{self.url}: unusable Mcp-Session-Id")
        self.session_id = value

    # ------------------------------------------------------------------
    # A session that expires
    # ------------------------------------------------------------------

    async def _renew_session(self, stale: str | None) -> None:
        """Run `initialize` again, once, however many requests noticed the 404 together."""
        async with self._renew_lock:
            if self.session_id != stale and not self._session_lost:
                return  # somebody else already renewed it
            logger.warning("backend at %s lost its MCP session; starting a new one", self.url)
            self.session_id = None
            self.protocol_version = None
            self._session_lost = True
            if self._listener is not None:
                self._listener.cancel()
                self._listener = None
            await super().initialize()
            self._session_lost = False
            self._start_listener()
        for capability, method in _LIST_CHANGED.items():
            if self.supports(capability):
                await self._handle_notification(method, {})
        # And what the new session does *not* carry over: whatever the gateway had set on
        # the old one. This module knows a renewal happened and nothing else -- not the
        # backend's name, not who subscribed to what -- so it says so and lets the gateway
        # repair it. See "A session that expires" in `mcp_http.md`.
        await self._handle_notification(protocol.SESSION_RENEWED, {})

    def _renew_later(self) -> None:
        stale = self.session_id

        async def renew() -> None:
            try:
                await self._renew_session(stale=stale)
            except Exception as exc:  # noqa: BLE001 -- logged; the next request retries
                logger.warning("backend at %s: could not renew its session: %s", self.url, exc)

        task = asyncio.create_task(renew())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------
    # The GET stream
    # ------------------------------------------------------------------

    def _start_listener(self) -> None:
        if self._closed or self._listener is not None:
            return
        self._listener = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        """Hold the stream a server uses for what it says unprompted, reopening it if it drops.

        `405` is the server saying it has no such stream, which is allowed and final. A
        dropped stream is reopened with `Last-Event-ID` when the server numbered its events,
        so one that supports resumption replays what we missed.
        """
        delay = 0.5
        last_event_id: str | None = None
        while not self._closed:
            extra = {"Last-Event-ID": last_event_id} if last_event_id else None
            try:
                response = await self._send("GET", None, extra=extra)
            except (OSError, asyncio.TimeoutError) as exc:
                logger.debug("MCP GET stream to %s did not open: %s", self.url, exc)
            else:
                try:
                    if response.status == 405:
                        logger.debug("%s offers no GET stream", self.url)
                        return
                    if response.status == 404 and self.session_id is not None:
                        self._listener = None
                        self._renew_later()
                        return
                    if response.status == 200 and response.content_type == "text/event-stream":
                        delay = 0.5
                        async for event_id, data in response.events(self._MESSAGE_LIMIT):
                            if event_id:
                                last_event_id = event_id
                            await self._deliver(data)
                    else:
                        logger.warning(
                            "MCP GET stream to %s refused: HTTP %s", self.url, response.status
                        )
                except (OSError, asyncio.IncompleteReadError, MCPProtocolError) as exc:
                    logger.debug("MCP GET stream to %s dropped: %s", self.url, exc)
                finally:
                    response.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._LISTEN_RETRY_CAP)

    # ------------------------------------------------------------------
    # HTTP/1.1
    # ------------------------------------------------------------------

    async def _send(
        self,
        method: str,
        payload: dict[str, Any] | None,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> _Response:
        """Open a connection, send one request, and read the response's head.

        One connection per request, closed after it -- the same shape the gateway's own
        server side has, and the reason there is no pool to get wrong.

        Only the connect is timed. A server answering in JSON sends its head when the tool
        has finished, so the head can legitimately take as long as the call; the caller's
        own `request_timeout` is what bounds that, and it hangs up through `cancel_request`.
        """
        context = None
        if self._tls:
            context = self.ssl_context or ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self._host,
                self._port,
                ssl=context,
                server_hostname=self._host if context else None,
                limit=self._MESSAGE_LIMIT,
            ),
            timeout=self.request_timeout,
        )
        try:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            lines = [
                f"{method} {self._path} HTTP/1.1",
                f"Host: {self._host_header}",
                f"User-Agent: mcp-gateway/{__version__}",
                "Connection: close",
                (
                    "Accept: text/event-stream"
                    if method == "GET"
                    else "Accept: application/json, text/event-stream"
                ),
            ]
            if payload is not None:
                lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
            if self.session_id is not None:
                lines.append(f"Mcp-Session-Id: {self.session_id}")
            if self.protocol_version is not None:
                lines.append(f"MCP-Protocol-Version: {self.protocol_version}")
            for name, value in {**self.headers, **(extra or {})}.items():
                lines.append(f"{name}: {value}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
        except BaseException:
            writer.close()
            raise
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(status_line.split(" ", 2)[1])
        except (IndexError, ValueError):
            writer.close()
            raise MCPProtocolError(f"{self.url}: not an HTTP response") from None
        headers: dict[str, str] = {}
        for line in header_lines:
            if line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        return _Response(status=status, headers=headers, reader=reader, writer=writer)
