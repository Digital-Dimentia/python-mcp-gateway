"""Bind the gateway to a WebSocket, and decide who is allowed to speak to it.

Lifted from `python-acp`'s `transport_ws.py`: the access-key hook, the loopback bind
refusal, the message-size cap and the keepalive settings are that module's, comments
included, because the reasoning is correct and was learned the hard way. What is *not*
lifted is everything above the frame -- no ACP SDK, no `run_agent`, no `Transport`
conformance. A decoded message goes to `Session.handle`, which is our own method table.

## Two paths and a page on one port

`/mcp` speaks MCP to any number of clients. `/admin` speaks a separate JSON-RPC method
table to the UI. The split is not cosmetic: admin verbs must not appear in the model's tool
list, and a UI should not have to speak MCP to ask which backends are up. `/ui` is not a
socket at all -- it is the UI's own static assets, answered by `webui.py` from the same
`process_request` hook, so the page is same-origin with the two sockets it opens. Anything
else gets a 404 during the handshake, before a connection exists.

## Origin

A request carrying an `Origin` header must name this server, or it is refused. Only
browsers send one, so nothing that speaks to this daemon today is affected -- and a browser
is now a first-class client, which is what turns "any web page can open a socket to
127.0.0.1" from a note into a hole. See `origin_permitted`.

## One connection, one reader, one task per request

Each connection gets its own read loop. A request is dispatched into a task of its own, so
a slow `tools/call` -- which may sit behind a backend waiting on a human -- does not stop
the connection from reading the `notifications/cancelled` that would un-ask it. That is the
same discipline `mcp_stdio.py` applies in the other direction, and for the same reason.

## Framing is ours; dispatch is not

Everything below JSON -- a parse error, a payload that is not an object, a message that is
none of request/notification/response -- is answered here, because there is no id to carry
it upward and no handler to route it to. Everything above goes to the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import secrets as stdlib_secrets
from http import HTTPStatus
from typing import Any, AsyncIterator, Callable, Protocol
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from mcp_gateway import errors, jsonrpc, webui

logger = logging.getLogger(__name__)

#: Matches the backend client's stream limit. `websockets` defaults to 1 MiB, which a
#: single multimodal tool result can exceed; a client that hits the cap gets its connection
#: closed rather than an error, so the two directions must not disagree about the size of a
#: message they will both be asked to carry.
MAX_MESSAGE_BYTES = 50 * 1024 * 1024

#: Environment variable holding the shared access key a client must present.
#:
#: **An environment variable and not a CLI flag, deliberately.** argv is world-readable
#: through `ps` on every platform this runs on, so a `--ws-key` flag would publish the
#: secret to every other account on the machine at the moment it is used to protect it.
ACCESS_KEY_ENV = "MCP_GATEWAY_WS_KEY"

#: The key's name in `gateway.env`, checked when the environment variable is unset. The
#: credential store is the natural home for the daemon's own credential: it is already
#: gitignored, mode-checked, and covered by the redaction filter.
ACCESS_KEY_SECRET_NAME = "WS_ACCESS_KEY"

#: Escape hatch for `refuse_unauthenticated_bind`. `1`, `true` or `yes` binds a
#: non-loopback interface with no key anyway.
ALLOW_UNAUTHENTICATED_ENV = "MCP_GATEWAY_WS_ALLOW_UNAUTHENTICATED"

#: Extra browser origins allowed to open a socket, comma-separated. `*` disables the check.
#:
#: The default set is this server's own address, which is what the shipped UI is served
#: from. This exists for the person running the UI from a Vite dev server on another port,
#: and for nothing else.
ALLOWED_ORIGINS_ENV = "MCP_GATEWAY_WS_ALLOWED_ORIGINS"

#: Query parameter carrying the key: `ws://host:8765/mcp?key=<secret>`.
#:
#: A query parameter is the wrong place for a secret on principle -- it lands in proxy and
#: server access logs, which is where URLs get written down. It is here because it is the
#: one carrier every WebSocket client library can send without custom header support, and a
#: key nobody can present protects nothing. `Authorization: Bearer` is also accepted and is
#: what the bridge sends; retiring the query form is filed as its own issue.
ACCESS_KEY_QUERY_PARAM = "key"

#: Seconds between server keepalive pings, and how long a pong may go unanswered before the
#: connection is dropped. Both are `serve()`'s own current defaults, restated here
#: **because they are client-facing contract rather than an implementation detail.**
#:
#: MCP defines a `ping` method, but a client that correctly sends nothing of its own on an
#: idle connection depends on *these* frames to hold a NAT or proxy mapping open. A daemon
#: makes that the normal case rather than an edge one: a client can sit attached and silent
#: for hours. Inheriting them from `websockets` would leave that dependency written down
#: nowhere and free to change under a pin bump with no test failing.
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0

MCP_PATH = "/mcp"
ADMIN_PATH = "/admin"


class UnauthenticatedBindError(RuntimeError):
    """A non-loopback bind was asked for with no key and no opt-out.

    A `RuntimeError` rather than a `ValueError`: nothing about the arguments is malformed,
    and `errors.to_error_object` maps `ValueError` to `-32602`, which would be a bizarre
    answer to a startup misconfiguration that never reaches a client.
    """


class Connection(Protocol):
    """What `transport_ws` needs from whatever is handling a connection.

    A `Protocol` so this module depends on no concrete session type, which is what keeps
    the two paths symmetrical and lets a test drive the transport with a stub.
    """

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one well-formed message, or return `None` for a notification."""

    async def closed(self) -> None:
        """The peer went away. Cancel anything still running on its behalf."""


ConnectionFactory = Callable[["ClientLink"], Connection]


def access_key_from_env(environ: dict[str, str] | None = None) -> str | None:
    """The configured key, or `None` when there is none.

    An empty value reads as unset. `MCP_GATEWAY_WS_KEY=` in a shell profile or a unit file
    is how someone spells "I turned this off", and treating it as a key that matches only
    the empty string would be a trap: every client that sent no key at all would still be
    refused, while one that sent `?key=` would be let in.
    """
    source = os.environ if environ is None else environ
    return source.get(ACCESS_KEY_ENV) or None


def unauthenticated_bind_allowed(environ: dict[str, str] | None = None) -> bool:
    """Whether the opt-out is set, read strictly.

    Only `1`, `true` and `yes`, case-insensitively. A permissive reading would turn
    `MCP_GATEWAY_WS_ALLOW_UNAUTHENTICATED=0` -- which says the opposite -- into consent.
    """
    source = os.environ if environ is None else environ
    return source.get(ALLOW_UNAUTHENTICATED_ENV, "").strip().lower() in {"1", "true", "yes"}


def is_loopback(host: str | None) -> bool:
    """Whether binding `host` reaches only this machine. **Fails closed.**

    `None` and `""` mean every interface to `serve()`, and a name we cannot parse as an
    address is not resolved here -- a DNS lookup at startup is a side effect this function
    has no business having, and one that could answer differently later. Anything not
    provably loopback is treated as exposed, which is the safe direction to be wrong in.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def refuse_unauthenticated_bind(host: str | None, key: str | None, allowed: bool) -> None:
    """Refuse to serve an unauthenticated gateway to anything but this machine.

    The threat is specific and not hypothetical. This daemon spawns the commands named in
    its config and holds every credential on the machine in one file; a socket anyone can
    open is both arbitrary code execution and a credential oracle, as whoever runs the
    daemon. On loopback that is the design -- the client is the user. On any other
    interface it is a remote shell with no password.

    This lives in the transport rather than in `cli.py` so a caller embedding
    `GatewayServer` in its own program inherits the guard instead of having to remember it.
    """
    if key is not None or allowed or is_loopback(host):
        return
    raise UnauthenticatedBindError(
        f"refusing to bind {host or 'all interfaces'} without an access key: the gateway "
        f"spawns the commands named in its config and holds every credential in "
        f"gateway.env, so an unauthenticated socket off loopback is a remote shell with no "
        f"password. Set {ACCESS_KEY_ENV}=<secret> (or {ACCESS_KEY_SECRET_NAME} in "
        f"gateway.env) and connect to ws://.../mcp?{ACCESS_KEY_QUERY_PARAM}=<secret>, or "
        f"set {ALLOW_UNAUTHENTICATED_ENV}=1 to accept the risk."
    )


def configured_origins(environ: dict[str, str] | None = None) -> frozenset[str]:
    """The extra origins from the environment. Empty when unset."""
    source = os.environ if environ is None else environ
    raw = source.get(ALLOWED_ORIGINS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def own_origins(host: str | None, port: int) -> frozenset[str]:
    """The origins a browser would report for a page this server itself served.

    All three loopback spellings, because a user who binds `127.0.0.1` and then types
    `localhost:8765` has done nothing wrong and must not be met with a 403 they cannot
    diagnose. `https` is included against the day `wss://` lands (`python-mcp-gateway-4rw`);
    an origin nothing can currently serve costs nothing to accept.
    """
    hosts = {host} if host else set()
    if not host or is_loopback(host):
        hosts |= {"127.0.0.1", "localhost", "[::1]"}
    return frozenset(f"{scheme}://{h}:{port}" for h in hosts if h for scheme in ("http", "https"))


def origin_permitted(origin: str | None, allowed: frozenset[str]) -> bool:
    """Whether a connection carrying `origin` may proceed.

    **No `Origin` header means yes.** Only browsers send one, and every non-browser client
    this gateway has -- the bridge, Claude Desktop, the tests -- sends none. Refusing an
    absent header would break all of them to defend against something that cannot happen
    without one.

    A *present* header that does not match is refused, and that is the whole point.
    WebSocket has no same-origin policy: any page on the internet can open a socket to
    `127.0.0.1:8765`, and this daemon spawns the commands in its config and holds every
    credential on the machine. Before there was a UI the exposure was theoretical; a
    browser-facing surface is exactly the thing that makes it real.
    """
    if origin is None:
        return True
    if "*" in allowed:
        return True
    return origin in allowed


def _offered_keys(request: Request) -> list[str]:
    """Every key the request presents, from the query string and the Authorization header."""
    offered = list(parse_qs(urlsplit(request.path).query).get(ACCESS_KEY_QUERY_PARAM, []))
    header = request.headers.get("Authorization")
    if header and header.lower().startswith("bearer "):
        offered.append(header[len("bearer ") :].strip())
    return offered


def _access_check(expected: str | None, origins: Callable[[], frozenset[str]]) -> Any:
    """A `process_request` hook: serve the UI, 404 an unknown path, 401 a client without the key.

    All of it happens during the opening handshake, so a rejected client never reaches
    `initialize` and never becomes a connection at all.

    `origins` is a callable rather than a set because the allowed set names the bound port,
    and with `--port 0` that is not known until after `serve()` has been handed this hook.
    """
    expected_bytes = expected.encode("utf-8") if expected else None

    def process_request(connection: ServerConnection, request: Request) -> Response | None:
        path = urlsplit(request.path).path or "/"
        # Before the key check: the UI's own assets carry no key, because a browser cannot
        # put one on a navigation. See `webui.py` for why that is safe and not a shortcut.
        if webui.is_ui_path(path):
            # The whole target, not the split path: the redirect `/ui` -> `/ui/` has to
            # carry the query string, which is where the access key is.
            return webui.response(request.path)
        if path not in (MCP_PATH, ADMIN_PATH):
            logger.warning("Rejected WebSocket connection to unknown path %r", path)
            return connection.respond(
                HTTPStatus.NOT_FOUND,
                f"No such endpoint. Try {MCP_PATH}, {ADMIN_PATH} or {webui.UI_PATH}.\n",
            )
        origin = request.headers.get("Origin")
        if not origin_permitted(origin, origins()):
            logger.warning(
                "Rejected WebSocket connection from %s to %s: origin %r is not allowed",
                connection.remote_address,
                path,
                origin,
            )
            return connection.respond(
                HTTPStatus.FORBIDDEN,
                f"Origin not allowed. Set {ALLOWED_ORIGINS_ENV} to permit it.\n",
            )
        if expected_bytes is None:
            return None
        offered = _offered_keys(request)
        # Exactly one, so `?key=wrong&key=right` cannot be smuggled past a check that
        # scanned for any match. `compare_digest` keeps the comparison constant-time.
        if len(offered) == 1 and stdlib_secrets.compare_digest(
            offered[0].encode("utf-8"), expected_bytes
        ):
            return None
        logger.warning(
            "Rejected WebSocket connection from %s to %s: %s access key",
            connection.remote_address,
            path,
            "missing" if not offered else "wrong",
        )
        # No detail in the body. A rejected client is unauthenticated by definition, so it
        # has no claim on knowing whether the key was absent, wrong, or duplicated.
        return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

    return process_request


class ClientLink:
    """One WebSocket connection, as the thing above it sees.

    Owns framing and outbound writes. Knows nothing about MCP: what a message *means* is
    the session's, which is what lets `/mcp` and `/admin` share every line of this.
    """

    def __init__(self, websocket: ServerConnection, path: str) -> None:
        self._websocket = websocket
        self.path = path
        self.remote = websocket.remote_address
        self._write_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        """Write one message. Serialised, because several tasks answer on one connection.

        A closed connection is not an error here: a client that hung up mid-call is the
        ordinary case, and raising would turn every disconnect into a logged traceback.
        """
        try:
            async with self._write_lock:
                await self._websocket.send(jsonrpc.encode(message))
        except Exception as exc:  # websockets raises several types for "it's gone"
            logger.debug("send to %s failed (connection gone): %s", self.remote, exc)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self.send(jsonrpc.notification(method, params))

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        """Every inbound frame, so the read loop never touches the socket directly."""
        return self._websocket.__aiter__()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._websocket.close()


class GatewayServer:
    """Serves the gateway over WebSocket: many connections, one shared everything below.

    The split mirrors `python-acp`'s "one agent per socket, one registry per process", and
    for the same reason: `initialize` records *that* client's capabilities and negotiated
    version, so a shared connection object would have the second client silently answered
    with the first one's gates. Everything below a connection -- the supervisor, the
    catalogue, the credential store -- is process-wide, and that is what makes this a
    daemon rather than a launcher.
    """

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        admin_factory: ConnectionFactory | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        access_key: str | None = None,
        allow_unauthenticated: bool = False,
    ) -> None:
        # Before anything else, and in the constructor rather than in `start()`: the point
        # of the guard is that the misconfiguration never gets as far as a listening port.
        refuse_unauthenticated_bind(host, access_key, allow_unauthenticated)
        self._connection_factory = connection_factory
        self._admin_factory = admin_factory
        self._host = host
        self._port = port
        self._access_key = access_key
        self._server: Server | None = None
        self._links: set[ClientLink] = set()

    def _allowed_origins(self) -> frozenset[str]:
        """Origins a browser may open a socket from. Recomputed per request, not per bind.

        Per request because `self.port` is only real once the socket exists -- `--port 0`
        is how every test binds -- and because the environment override should take effect
        on a reload rather than only on a restart.
        """
        return own_origins(self._host, self.port) | configured_origins()

    @property
    def port(self) -> int:
        """The bound port, which is not `self._port` when that was 0.

        Tests bind an ephemeral port; without this they would have to reach into
        `websockets` internals to find out which one they got.
        """
        if self._server is None:
            return self._port
        return next(iter(self._server.sockets)).getsockname()[1]

    @property
    def links(self) -> frozenset[ClientLink]:
        return frozenset(self._links)

    def _report_access_key(self) -> None:
        """Say at startup whether authentication is on, and never say what the key is.

        This exists for the deploy, not for debugging. A key arrives through the
        environment or a credential file, which is exactly the kind of configuration that
        fails silently: an unset variable in a unit file, an interpolation that expanded to
        nothing, a secret mounted after the process started. Every one of those produces a
        daemon that runs perfectly and accepts anybody, and the only other evidence is a
        connection that *should* have been rejected and was not -- which nobody is watching
        for.

        The length is included because it separates "the deploy passed nothing" from "the
        deploy passed something truncated or quoted", which is the difference between the
        two mistakes that actually happen. **The value never is**, at any level: that would
        put the secret in a log file, which is the whole reason a query parameter is the
        wrong carrier in the first place.
        """
        if self._access_key:
            logger.info(
                "access key configured (%d characters); clients must present ?%s=<secret> "
                "or Authorization: Bearer <secret>",
                len(self._access_key),
                ACCESS_KEY_QUERY_PARAM,
            )
            return
        # Not a warning: on loopback this is the ordinary local-development arrangement,
        # and a warning every developer learns to ignore is worth less than an accurate
        # sentence. The bind guard is what refuses this off loopback.
        logger.info(
            "no access key configured (%s unset and %s absent from gateway.env): every "
            "client that can reach %s:%s is accepted",
            ACCESS_KEY_ENV,
            ACCESS_KEY_SECRET_NAME,
            self._host,
            self._port,
        )

    async def start(self) -> None:
        if self._server is not None:
            return
        self._report_access_key()
        self._server = await serve(
            self._handle_client,
            self._host,
            self._port,
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=PING_INTERVAL_SECONDS,
            ping_timeout=PING_TIMEOUT_SECONDS,
            process_request=_access_check(self._access_key, self._allowed_origins),
        )
        logger.info("listening on ws://%s:%s%s", self._host, self.port, MCP_PATH)
        logger.info("admin UI at %s", webui.url(self._host, self.port))

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        await self._server.wait_closed()

    async def broadcast(self, method: str, params: dict | None = None, *, path: str = MCP_PATH) -> int:
        """Send one notification to every connection on `path`. Returns how many got it."""
        targets = [link for link in self._links if link.path == path]
        for link in targets:
            await link.notify(method, params)
        return len(targets)

    async def _handle_client(self, websocket: ServerConnection) -> None:
        path = urlsplit(websocket.request.path).path or "/"
        factory = self._admin_factory if path == ADMIN_PATH else self._connection_factory
        if factory is None:
            await websocket.close(code=1011, reason="endpoint not available")
            return

        link = ClientLink(websocket, path)
        connection = factory(link)
        self._links.add(link)
        logger.info("client connected to %s from %s", path, link.remote)
        in_flight: set[asyncio.Task[None]] = set()
        try:
            await self._read_loop(link, connection, in_flight)
        finally:
            self._links.discard(link)
            # Cancel this connection's work before telling the session it is gone. A
            # client that hung up must not leave a backend working on a reply nobody will
            # read -- `MCPStdioClient.request` turns that cancellation into a
            # `notifications/cancelled` on the backend's wire.
            for task in list(in_flight):
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            await connection.closed()
            logger.info("client disconnected from %s (%s)", path, link.remote)

    async def _read_loop(
        self, link: ClientLink, connection: Connection, in_flight: set[asyncio.Task[None]]
    ) -> None:
        async for raw in link:
            try:
                message = jsonrpc.decode(raw)
            except Exception:
                logger.debug("parse error from %s", link.remote)
                await link.send(
                    jsonrpc.failure(None, errors.error_object(errors.PARSE_ERROR, "invalid JSON"))
                )
                continue

            kind = jsonrpc.classify(message)
            if kind is jsonrpc.Kind.INVALID:
                await link.send(
                    jsonrpc.failure(
                        jsonrpc.request_id_of(message),
                        errors.error_object(errors.INVALID_REQUEST, "not a JSON-RPC message"),
                    )
                )
                continue

            # One task per inbound message, so a slow `tools/call` -- which may be waiting
            # on a backend that is waiting on a human -- does not stop this loop from
            # reading the `notifications/cancelled` that would un-ask it.
            task = asyncio.create_task(self._serve_one(link, connection, message))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)

    async def _serve_one(
        self, link: ClientLink, connection: Connection, message: dict[str, Any]
    ) -> None:
        request_id = jsonrpc.request_id_of(message)
        try:
            answer = await connection.handle(message)
        except asyncio.CancelledError:
            raise
        except errors.GatewayErrorObject as mapped:
            if request_id is not None:
                await link.send(jsonrpc.failure(request_id, mapped.error))
            return
        except Exception as exc:
            # Something above forgot `@as_error_object`. Log it as the bug it is, and still
            # answer, because a client waiting forever is worse than a generic code.
            logger.exception("unmapped error serving %s", message.get("method"))
            if request_id is not None:
                await link.send(jsonrpc.failure(request_id, errors.to_error_object(exc)))
            return
        if request_id is not None and answer is not None:
            await link.send(jsonrpc.success(request_id, answer))


def resolve_access_key(store: Any = None, environ: dict[str, str] | None = None) -> str | None:
    """The key to require, from the environment first and `gateway.env` second.

    The environment wins so a deployment can override a checked-out credential file without
    editing it -- a container that mounts its own secret, a systemd unit with a
    `LoadCredential`. Both being unset means no key, which the bind guard then refuses off
    loopback.
    """
    from_env = access_key_from_env(environ)
    if from_env:
        return from_env
    if store is not None:
        return store.get(ACCESS_KEY_SECRET_NAME) or None
    return None
