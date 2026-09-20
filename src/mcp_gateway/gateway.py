"""The composition root: owns everything shared, builds a `Session` per connection.

Nothing else constructs a supervisor, a catalogue or a store. That concentration is what
lets `cli.py` stay a command line and lets a test drive the whole daemon in-process with
two clients attached.

## What is shared and what is per-connection

Per connection: the negotiated protocol version, the client's declared capabilities, and
that client's in-flight requests. Everything else -- backends, credentials, config -- is
process-wide. A daemon with three clients attached runs one copy of each backend, and all
three see the same live state. That is the difference between a gateway and a launcher.

## Reverse passthrough and the origin-connection rule

A backend may ask `roots/list`, `sampling/createMessage` or `elicitation/create`. Those must
reach *a* client, and with several attached there is no general answer to "which one".

The rule: **the connection whose call is currently in flight.** `router` sets a
`contextvars.ContextVar` around every forwarded call, so a backend request raised while
serving connection A goes to A. That covers every real case, because a backend asks these
questions *in response to* a tool call.

A *spontaneous* backend request -- one arriving with no call in flight -- gets `-32601`.
Picking an arbitrary connection to interrupt a human on is not a fallback, it is a bug with
a plausible shape.

Downward, each backend is told the **union** of what the currently-attached clients
declared. A backend cannot renegotiate after `initialize`, so the union is the only honest
answer at the moment it starts, and the origin rule above is what guarantees a request we
accepted can always be placed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from pathlib import Path
from typing import Any

from mcp_gateway import __version__, errors, jsonrpc, naming, protocol
from mcp_gateway.admin import Admin, error_result, is_admin_tool, text_result, tool_definitions
from mcp_gateway.admin_channel import AdminConnection, LogStream
from mcp_gateway.backend import Backend, BackendStatus
from mcp_gateway.catalogue import Catalogue
from mcp_gateway.clipboard import Workbench
from mcp_gateway.config import ConfigError, GatewayConfig
from mcp_gateway.config import load as load_config
from mcp_gateway.logging_redaction import RedactingFilter, install_redaction
from mcp_gateway.mcp_stdio import (
    MCPClientCapabilities,
    MCPProtocolError,
    UnsupportedServerRequest,
)
from mcp_gateway.notifications import Notifier, Subscriptions
from mcp_gateway.panels import PanelStore
from mcp_gateway.router import Router
from mcp_gateway.secret_providers import build_store
from mcp_gateway.secrets import SecretError, SecretStore
from mcp_gateway.session import Session
from mcp_gateway.supervisor import Supervisor
from mcp_gateway.transport_ws import ClientLink, GatewayServer

logger = logging.getLogger(__name__)

#: How often idle backends are swept. A backend sleeps up to this long after its
#: `idle_ttl`, which is a granularity rather than a delay: the point is reclaiming a
#: process nobody is using, and a few seconds either way changes nothing about that.
IDLE_SWEEP_SECONDS = 15.0

#: How often a failed backend is reconsidered.
#:
#: A granularity, not a delay: what decides when an attempt actually happens is
#: `Backend.restart_backoff_seconds`, which is 0 for the first retry and then doubles to a
#: one-minute cap. This only has to be fine enough that the common case -- a backend whose
#: dependency was a few seconds behind it at startup -- recovers while somebody is still
#: watching, rather than a minute later.
RECOVERY_SWEEP_SECONDS = 5.0

class Gateway:
    """Everything the daemon owns, and the handlers a `Session` calls into."""

    def __init__(
        self,
        config: GatewayConfig,
        store: SecretStore,
        *,
        config_path: Path,
        env_path: Path,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.config = config
        self.store = store
        self.config_path = config_path
        self.env_path = env_path
        self.host = host
        self.port = port
        self.admin = Admin(self)
        #: The admin UI's two right-hand columns, as the page last published them, and
        #: the source of both `admin.clipboard.get` and `gateway__clipboard`. One per
        #: process rather than per connection: the point of it is that the model on
        #: `/mcp` reads what the person did on `/admin`. See `clipboard.md`.
        self.workbench = Workbench()
        #: The live panel URLs: minted on `/admin`, served by the transport's HTTP hook.
        #: One per process for the same reason as the workbench above -- the minter and the
        #: server cannot reach each other except through the object they share. See
        #: `panels.md`.
        self.panels = PanelStore()
        self._server_request_handler = self.backend_request
        self.notifier = Notifier(self._broadcast)
        #: Who is watching which resource. See `notifications.md`.
        self.subscriptions = Subscriptions()
        self._idle_sweeper: asyncio.Task[None] | None = None
        self._recovery_sweeper: asyncio.Task[None] | None = None
        self.supervisor = Supervisor(
            config,
            store,
            client_capabilities=self.client_capability_union,
            on_server_request=self._server_request_handler,
            on_notification=self.backend_notification,
        )
        self.catalogue = Catalogue(self.supervisor)
        self.router = Router(self.catalogue)
        self.server: GatewayServer | None = None
        self._sessions: set[Session] = set()
        #: Requests the gateway has put *to* a client, awaiting that client's answer.
        self._client_requests: dict[Any, asyncio.Future] = {}
        self._next_client_request_id = 0
        #: Backends whose session is being repaired after a renewal, so a renewal provoked
        #: by the repair itself does not start a second one. See `_session_renewed`.
        self._renewing: set[str] = set()
        #: Serialises reloads. A SIGHUP arriving while a client calls `gateway__reload_config`
        #: would otherwise interleave stops and starts on the same backends.
        self._reload_lock = asyncio.Lock()
        #: Held so a reload can re-install redaction without losing it. The key itself is
        #: **not** re-read on reload: rotating it would drop every attached client, so it
        #: binds at startup only.
        self._access_key: str | None = None
        #: Captures redacted log records for `admin.logs.tail`. Attached in `start()`, after
        #: the redaction filter, so it never sees a record the filter has not scrubbed.
        self.log_stream: LogStream | None = None
        #: Set by `cli` so a signal can end `serve_forever` without cancelling mid-write.
        self.shutdown_requested = asyncio.Event()

    # --- what a Session calls -----------------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """What a client is told this server is, at `initialize`.

        `name` is the identifier clients key their own config off; `title` is the display
        string beside it, and both come from `branding:` when the deployment sets one. An
        unbranded gateway reports exactly what it always has -- see `branding.md`.
        """
        branding = self.config.branding
        return {"name": branding.name, "title": branding.title, "version": __version__}

    def instructions(self) -> str:
        """Told to the model at `initialize`, so it knows the namespace exists.

        Without this a model sees `github__create_issue` and has no reason to believe the
        prefix means anything, which costs it the ability to reason about *which* service a
        failure came from.
        """
        live = [b.name for b in self.backends() if b.running]
        listing = ", ".join(live) if live else "none currently running"
        return (
            "This is an MCP gateway. Tools are namespaced as <server>__<tool>, prompts the "
            f"same way, and resources as mcpgw://<server>/<uri>. Backends: {listing}. "
            "The gateway's own tools are gateway__list_backends, gateway__backend_health, "
            "gateway__restart_backend and gateway__reload_config; call the first two to "
            "find out why a tool you expected is missing."
        )

    def bind_address(self) -> str:
        port = self.server.port if self.server is not None else self.port
        return f"{self.host}:{port}"

    def backends(self) -> list[Backend]:
        return self.supervisor.all

    def backend(self, name: str | None) -> Backend | None:
        return self.supervisor.get(name)

    def describe_connections(self) -> list[dict[str, Any]]:
        """Every open connection, on either path.

        Enumerated from the **server's links**, not from `_sessions`. `_sessions` holds only
        `/mcp` connections that have completed `initialize`, so reporting from it would tell
        an operator there is nothing attached while a UI sits on `/admin` and a client is
        mid-handshake -- which is exactly when someone looks.
        """
        by_link = {session.link: session for session in self._sessions}
        links = self.server.links if self.server is not None else frozenset()
        described = []
        for link in links:
            session = by_link.get(link)
            described.append(
                {
                    "path": link.path,
                    "client_info": session.client_info if session else {},
                    "protocol_version": session.protocol_version if session else None,
                    "connected_at": session.connected_at if session else None,
                    "initialized": bool(session and session.initialized),
                }
            )
        return described

    def client_capability_union(self) -> MCPClientCapabilities:
        """What to declare to a backend: the union across live sessions.

        Recomputed on connect and disconnect. A backend started while one client was
        attached keeps whatever it was told -- MCP has no renegotiation -- which is why a
        newly-declared capability only reaches backends started after it, and why the
        origin-connection rule has to be able to refuse.

        A capability block is a promise, and `MCPStdioClient` refuses outright to declare
        one with no `on_server_request` behind it -- correctly, because declaring something
        nothing answers is worse than declaring nothing: it entitles a backend to send a
        request and get a `-32601` it was told would not happen.

        `sampling` is deliberately absent from what we can declare: `MCPClientCapabilities`
        has no field for it, so the block cannot be built wrong. The gateway would happily
        forward a `sampling/createMessage` to a client that declared it, but the lifted
        capability type predates that and adding the field is its own change.
        """
        # MCP Apps is computed outside the handler guard below, and has to be: it entitles a
        # backend to no request at all, so there is nothing for a handler to answer. Gating
        # it on one would make a gateway that forwards nothing upward also unable to carry a
        # panel, which are unrelated things.
        ui_mime_types = self._ui_app_mime_types()

        if self._server_request_handler is None:
            return MCPClientCapabilities(ui_app_mime_types=ui_mime_types)
        declared: set[str] = set()
        for session in self._sessions:
            declared.update(session.client_capabilities)
        return MCPClientCapabilities(
            roots="roots" in declared,
            elicitation="elicitation" in declared,
            ui_app_mime_types=ui_mime_types,
        )

    def _ui_app_mime_types(self) -> tuple[str, ...]:
        """The panel content types some attached client says it can render.

        A union rather than a constant, and it is the one capability here that is not a
        claim about this process: the gateway renders nothing. What a backend needs to know
        is what the host on the far side of the gateway can render, so that is what it is
        told -- and when two clients disagree, a backend offering for the union lets each
        host take the one it understands and ignore the other.

        Sorted so a backend restarted with the same clients attached is handed the same
        block, rather than one that reorders with set iteration and looks like a change.
        """
        mime_types: set[str] = set()
        for session in self._sessions:
            extensions = session.client_capabilities.get("extensions")
            if not isinstance(extensions, dict):
                continue
            block = extensions.get(protocol.UI_EXTENSION_ID)
            if not isinstance(block, dict):
                continue
            declared = block.get("mimeTypes")
            if isinstance(declared, list):
                mime_types.update(item for item in declared if isinstance(item, str))
        return tuple(sorted(mime_types))

    async def session_ready(self, session: Session) -> None:
        self._sessions.add(session)

    async def session_closed(self, session: Session) -> None:
        self._sessions.discard(session)
        # Its subscriptions go with it. Not unsubscribed downward: the backend's own send
        # is cheap, nothing is listening any more, and a shutdown that waits on a round
        # trip per subscription is a shutdown that hangs on a wedged backend.
        self.subscriptions.drop_session(session)
        # The one client that wanted `debug` leaving is the commonest reason for the union
        # to fall, and nothing else would recompute it.
        if session.log_level is not None:
            await self.apply_log_level()

    def resolve_client_response(self, session: Session, message: dict[str, Any]) -> None:
        """Resolve an answer to something the gateway asked this client.

        A response naming an id we never issued is dropped with a log line, not an error: a
        client that answers twice, or answers after we gave up, is misbehaving in a way that
        costs us nothing, and there is no id to complain against.
        """
        future = self._client_requests.pop(message.get("id"), None)
        if future is None or future.done():
            logger.debug("unmatched response from client: id=%s", message.get("id"))
            return
        if "error" in message:
            future.set_exception(ClientRefused(message["error"]))
        else:
            future.set_result(message.get("result"))

    async def ask_client(
        self, session: Session, method: str, params: dict, timeout: float = 120.0
    ) -> Any:
        """Send a request *to* a client and wait for its answer.

        The timeout is generous because the thing on the other end is frequently a person:
        `elicitation/create` puts a question in front of a human, and thirty seconds is not
        a realistic budget for one. It exists at all so a client that never answers cannot
        pin a backend's request open forever.
        """
        self._next_client_request_id += 1
        request_id = f"gw-{self._next_client_request_id}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._client_requests[request_id] = future
        await session.link.send(jsonrpc.request(request_id, method, params))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._client_requests.pop(request_id, None)

    async def backend_request(self, name: str, method: str, params: dict) -> dict[str, Any]:
        """A backend asked us something. Forward it to the client whose call is in flight.

        **The origin-connection rule.** `Backend.serving` marks the session around every
        forwarded call, so a request raised while serving connection A goes to A. That covers
        every real case, because a backend asks these questions *in response to* a call it is
        servicing.

        It is refused when the origin is unclear -- no call in flight, or two different
        clients mid-call on the same backend. MCP gives no correlation between a
        server-to-client request and the call that provoked it, so guessing would put one
        client's question in front of another client's user. `UnsupportedServerRequest`
        becomes the `-32601` the backend was told to expect for anything unanswerable.
        """
        if method not in protocol.RELAYED_SERVER_REQUESTS:
            raise UnsupportedServerRequest(f"{method} is not relayed by the gateway")
        backend = self.supervisor.get(name)
        session = backend.origin_session() if backend is not None else None
        if session is None:
            logger.info(
                "backend %r asked %s with no unambiguous client call in flight; refusing",
                name,
                method,
            )
            raise UnsupportedServerRequest(
                f"{method} arrived with no single client request in flight, so there is no "
                f"client to ask"
            )
        if not self._session_declares(session, method):
            raise UnsupportedServerRequest(
                f"the client serving this call did not declare the capability for {method}"
            )
        logger.debug("forwarding %s from backend %r to its calling client", method, name)
        try:
            return await self.ask_client(session, method, params)
        except ClientRefused as refused:
            # The client answered with an error. That is its answer, and it belongs to the
            # backend verbatim -- not turned into a gateway failure.
            raise MCPProtocolError(
                refused.message, code=refused.code, data=refused.data
            ) from None
        except asyncio.TimeoutError:
            raise UnsupportedServerRequest(f"the client did not answer {method} in time") from None

    @staticmethod
    def _session_declares(session: Session, method: str) -> bool:
        needed = {
            protocol.ROOTS_LIST: "roots",
            protocol.SAMPLING_CREATE_MESSAGE: "sampling",
            protocol.ELICITATION_CREATE: "elicitation",
        }[method]
        return needed in session.client_capabilities

    # --- MCP handlers -------------------------------------------------------------------

    async def list_tools(self, _params: dict) -> dict[str, Any]:
        """The gateway's own tools first, then every backend's.

        The meta-tools lead deliberately: a model scanning a long list should meet
        `gateway__list_backends` before it gives up on finding the tool it wanted.

        No `nextCursor`. The gateway has already walked every backend's pagination to build
        this, so there is nothing left to page; inventing a cursor would mean holding
        per-client state for no gain.
        """
        await self.supervisor.wait_ready()
        return {
            "tools": tool_definitions([b.name for b in self.supervisor.all])
            + await self.catalogue.tools()
        }

    async def call_tool(self, session: Session, params: dict) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise errors.InvalidParams("tools/call requires a 'name'")
        arguments = params.get("arguments") or {}
        if is_admin_tool(name):
            return await self.admin.call(name, arguments)
        return await self.router.call_tool(session, name, arguments)

    async def list_prompts(self, _params: dict) -> dict[str, Any]:
        """`{"prompts": []}` with no prompt-capable backend.

        Exactly what a real server with no prompts returns, and the reason `protocol.py` can
        advertise `prompts` unconditionally without stranding anybody on a `-32601`.
        """
        await self.supervisor.wait_ready()
        return {"prompts": await self.catalogue.prompts()}

    async def get_prompt(self, session: Session, params: dict) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise errors.InvalidParams("prompts/get requires a 'name'")
        return await self.router.get_prompt(session, name, params.get("arguments"))

    async def list_resources(self, _params: dict) -> dict[str, Any]:
        await self.supervisor.wait_ready()
        return {"resources": await self.catalogue.resources()}

    async def list_resource_templates(self, _params: dict) -> dict[str, Any]:
        await self.supervisor.wait_ready()
        return {"resourceTemplates": await self.catalogue.resource_templates()}

    async def read_resource(self, session: Session, params: dict) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise errors.InvalidParams("resources/read requires a 'uri'")
        return await self.router.read_resource(session, uri)

    async def subscribe_resource(self, session: Session, params: dict) -> dict[str, Any]:
        """`resources/subscribe`: tell me when this changes.

        The backend is asked first and the subscription recorded only if it agreed, so a
        refusal leaves no state behind and a client that retries is not fighting a
        half-registered entry. A repeat of a subscription this session already holds is a
        no-op that still answers `{}` -- the set semantics MCP leaves unstated, chosen so
        that a client resubscribing after its own reconnect logic costs nothing.
        """
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise errors.InvalidParams("resources/subscribe requires a 'uri'")
        if uri not in self.subscriptions.uris_of(session):
            await self.router.subscribe_resource(session, uri, on=True)
            self.subscriptions.add(session, uri)
        return {}

    async def unsubscribe_resource(self, session: Session, params: dict) -> dict[str, Any]:
        """`resources/unsubscribe`: stop telling me.

        Forgotten locally first and unconditionally. A client that asked to stop has
        stopped, whatever the backend then does with the request -- and if the backend
        refuses or is gone, the notification it keeps sending now reaches nobody.
        """
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise errors.InvalidParams("resources/unsubscribe requires a 'uri'")
        had = self.subscriptions.remove(session, uri)
        if had:
            await self.router.subscribe_resource(session, uri, on=False)
        return {}

    async def set_log_level(self, session: Session, params: dict) -> dict[str, Any]:
        """`logging/setLevel`: this client's threshold, and the backends' as a consequence.

        Two levels, not one. The client's is remembered on its session and is what filters
        the relay on the way up; what goes *down* to the backends is the most verbose level
        any live session has asked for, because one backend process serves every client and
        MCP has no per-subscriber level on the wire. A client asking for `debug` therefore
        makes the daemon noisier for itself alone -- everyone else still sees only what
        they asked for.
        """
        level = params.get("level")
        if not isinstance(level, str) or level not in protocol.LOG_LEVELS:
            raise errors.InvalidParams(
                f"logging/setLevel requires a 'level' from {list(protocol.LOG_LEVELS)}"
            )
        session.log_level = level
        await self.apply_log_level()
        return {}

    async def apply_log_level(self, only: str | None = None) -> None:
        """Push the union of every session's threshold down to the backends.

        `only` names one backend, for a process that has just replaced another and never
        heard the level -- the same replay a subscription needs, and for the same reason: a
        client that asked for `debug` an hour ago is not going to ask again.

        A backend that does not declare `logging` is skipped rather than refused; a backend
        that fails the call is logged and left alone, because a level that would not take is
        not worth failing a client's request over.
        """
        level = protocol.most_verbose([s.log_level for s in self._sessions if s.log_level])
        if level is None:
            return
        backends = [b for b in self.supervisor.running if b.supports("logging")]
        for backend in backends:
            if only is not None and backend.name != only:
                continue
            try:
                await backend.client.set_log_level(level)
            except Exception as exc:
                logger.debug("backend %r refused logging/setLevel %s: %s", backend.name, level, exc)

    async def complete(self, session: Session, params: dict) -> dict[str, Any]:
        """`completion/complete`: what values one argument might take.

        An empty `value` is not an error but the commonest case there is -- a client asks
        the moment the box is focused, before anything has been typed -- so it defaults
        rather than being required.

        `context` is forwarded whole rather than reduced to its `arguments`, so a member a
        later revision adds survives a gateway that predates it.
        """
        ref = params.get("ref")
        argument = params.get("argument")
        if not isinstance(ref, dict) or not isinstance(argument, dict):
            raise errors.InvalidParams(
                "completion/complete requires a 'ref' and an 'argument'"
            )
        if not isinstance(argument.get("name"), str) or not argument["name"]:
            raise errors.InvalidParams("completion/complete requires an argument 'name'")
        argument = dict(argument, value=argument.get("value") or "")
        context = params.get("context")
        return await self.router.complete(
            session, ref, argument, context if isinstance(context, dict) else None
        )

    async def backend_notification(self, name: str, method: str, params: dict) -> None:
        """A backend said something unprompted. See `notifications.md` for the whole table."""
        kind = self.notifier.kind_of(method)
        if kind is not None:
            logger.debug("backend %r reports %s changed", name, kind)
            self.catalogue.invalidate(name, kind=kind)
            await self.notifier.list_changed(method)
            return
        if method == protocol.PROGRESS:
            # Relayed verbatim to the originating connection only: the `progressToken` is
            # the client's own, forwarded down unchanged on the call that started the work.
            backend = self.supervisor.get(name)
            session = backend.origin_session() if backend is not None else None
            if session is not None:
                await session.link.notify(method, params)
            return
        if method == protocol.MESSAGE:
            self.notifier.log_backend_message(name, params)
            await self._relay_log_message(name, params)
            return
        if method == protocol.RESOURCES_UPDATED:
            await self._resource_updated(name, params)
            return
        if method == protocol.SESSION_RENEWED:
            await self._session_renewed(name)
            return
        logger.debug("dropping %s from backend %r", method, name)

    async def _session_renewed(self, name: str) -> None:
        """A `url` backend is answering in a session we did not set anything on.

        The same repair a restarted process gets, for the same reason: the subscriptions
        and the log level belonged to the session that went away, and a client that asked
        for either is not going to ask again. The listings are already being re-fetched --
        the transport raised `list_changed` before this arrived -- so only the two pieces
        of state nothing else replays are done here.

        Guarded against itself: replaying a subscription is a request like any other, and
        one that finds the new session gone too renews again and arrives back here. Without
        the guard that nests, one repair deep per renewal, for as long as the server keeps
        losing sessions.
        """
        if name in self._renewing:
            return
        self._renewing.add(name)
        try:
            await self.resubscribe(name)
            await self.apply_log_level(only=name)
        finally:
            self._renewing.discard(name)

    async def _relay_log_message(self, name: str, params: dict) -> None:
        """Forward one backend `notifications/message` to the clients that asked for it.

        Still logged locally first -- an operator reading the daemon's log should not have
        to be a connected client to see a backend complain. What this adds is the client's
        copy, filtered by the level *that* client set and tagged with which backend spoke.

        The tag goes in `logger`, the field MCP already has for it, rather than an invented
        one: a client aggregating three backends' logs needs to tell them apart, and a
        gateway that flattened them into one stream would be lossy in a way nothing
        downstream could undo.
        """
        level = str(params.get("level", "info"))
        origin = params.get("logger")
        payload = dict(params, logger=f"{name}/{origin}" if isinstance(origin, str) and origin else name)
        for session in list(self._sessions):
            if session.log_level is None:
                continue
            if not protocol.level_admits(session.log_level, level):
                continue
            try:
                await session.link.notify(protocol.MESSAGE, payload)
            except Exception as exc:  # pragma: no cover - a dead socket is not our problem
                logger.debug("failed to relay a log message to a session: %s", exc)

    async def _resource_updated(self, name: str, params: dict) -> None:
        """Relay one `notifications/resources/updated`, rewritten and narrowly addressed.

        The backend names its own URI; the client only ever saw the `mcpgw://` form, so the
        rewrite is what makes the notification mean anything to it. Everything else in
        `params` is forwarded untouched -- a later revision's `title` rides along.

        Delivered per session rather than broadcast, and per session failures are isolated:
        a client whose socket died mid-relay must not cost the others their notification.
        """
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            logger.debug("backend %r sent resources/updated with no uri", name)
            return
        public_uri = naming.encode_resource_uri(name, uri)
        sessions = self.subscriptions.sessions_for(public_uri)
        if not sessions:
            logger.debug("no subscriber for %s", public_uri)
            return
        payload = dict(params, uri=public_uri)
        for session in sessions:
            try:
                await session.link.notify(protocol.RESOURCES_UPDATED, payload)
            except Exception as exc:  # pragma: no cover - a dead socket is not our problem
                logger.debug("failed to relay resources/updated to a session: %s", exc)

    async def resubscribe(self, name: str) -> None:
        """Replay this backend's subscriptions against the process that just replaced it.

        A restarted backend knows nothing about what anyone subscribed to, and the client
        has no way to find that out -- its subscription would simply stop producing, which
        looks exactly like a resource that stopped changing. One that cannot be replayed is
        dropped rather than retried: it is gone either way, and keeping it would leave the
        registry claiming a subscription that does not exist.
        """
        backend = self.supervisor.get(name)
        if backend is None or not backend.running:
            return
        for public_uri in self.subscriptions.uris_for(name):
            try:
                await self.router.subscribe_resource(None, public_uri, on=True)
            except Exception as exc:
                logger.warning("could not resubscribe %s after restart: %s", public_uri, exc)
                self.subscriptions.drop_uri(public_uri)

    async def _broadcast(self, method: str, params: dict | None) -> int:
        if self.server is None:
            return 0
        return await self.server.broadcast(method, params)

    # --- admin operations ---------------------------------------------------------------

    async def restart_backend(self, name: str) -> bool:
        restarted = await self.supervisor.restart(name)
        # Announced whether or not the restart succeeded: whatever it published before is
        # not what it publishes now either way, and a caller who asked for this is watching.
        # The recovery sweeper is the one caller that announces only on success -- see
        # `_recover_failed_backends` for why an unattended retry is different.
        await self._announce_backend_change(name)
        return restarted

    async def _announce_backend_change(self, name: str) -> None:
        """Forget what a backend published, tell the clients, and put its session back.

        A client holding the old listing has no way to find out on its own. A restart is a
        catalogue change exactly as a reload is, and the two arrive here by different doors
        only because one of them re-reads a file first: an operator who restarts a backend
        after editing it watches an open UI go on showing what that backend used to publish.
        One notification per kind, debounced by the notifier like every other announcement.
        """
        self.catalogue.invalidate(name)
        for method in (
            protocol.TOOLS_LIST_CHANGED,
            protocol.PROMPTS_LIST_CHANGED,
            protocol.RESOURCES_LIST_CHANGED,
        ):
            await self.notifier.list_changed(method)
        await self.resubscribe(name)
        await self.apply_log_level(only=name)

    async def reload(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Re-read both files and apply the difference.

        **If either file fails to parse, nothing changes at all.** `config.load` and
        `secrets.load` build new objects and raise; there is never a moment when half a
        catalogue has been applied. That load-then-replace ordering is the whole guarantee,
        and it is why a typo in `servers.yaml` costs an operator an error message rather than
        a daemon with six backends missing.

        Serialised on a lock: two reloads racing -- a SIGHUP arriving while a client calls
        `gateway__reload_config` -- would interleave stops and starts on the same backends.
        """
        async with self._reload_lock:
            try:
                config = load_config(self.config_path)
                # Off the event loop: a provider is third-party code that may spend a
                # second on a network round trip, and the sessions already attached must
                # not stall on it. `build_store` builds a new object or raises, so moving
                # it to a thread costs nothing of the atomicity below -- there is still no
                # moment where half a store has been applied.
                store = await asyncio.to_thread(build_store, config, self.env_path)
            except (ConfigError, SecretError) as refusal:
                logger.error("reload refused, nothing changed: %s", refusal)
                return error_result(f"reload refused, nothing changed: {refusal}")

            if dry_run:
                return text_result(
                    {"dry_run": True, **self.supervisor.plan_reload(config, store).as_dict()}
                )

            plan = await self.supervisor.reload(config, store)
            # A removed backend's subscriptions have nothing left to point at; a changed
            # one is a new process that has never heard of them. See `notifications.md`.
            for name in plan.removed:
                self.subscriptions.drop_backend(name)
            self.config = config
            self.store = store
            # Re-install redaction with the new store: a rotated credential's *old* value
            # must stop being scrubbed, or a log line legitimately containing it becomes
            # unreadable, and the new value must start.
            filt = install_redaction(store, extra=[self._access_key] if self._access_key else [])
            if self.log_stream is not None:
                # The stream carries its own copy of the filter, so re-installing on the
                # root's handlers is not enough: without this the stream would keep scrubbing
                # the rotated-away value and stop scrubbing the new one.
                for existing in list(self.log_stream.filters):
                    if isinstance(existing, RedactingFilter):
                        self.log_stream.removeFilter(existing)
                self.log_stream.addFilter(filt)
            self.catalogue.invalidate_all()

            # One notification per kind after the whole sweep, not one per backend. See
            # `notifications.md`: eight restarted backends must not cost a client eight
            # fan-out refetches to reach the same answer.
            for method in (
                protocol.TOOLS_LIST_CHANGED,
                protocol.PROMPTS_LIST_CHANGED,
                protocol.RESOURCES_LIST_CHANGED,
            ):
                await self.notifier.list_changed(method)

            for name in plan.changed:
                await self.resubscribe(name)
            # `added` too, unlike the resubscribe above: nobody can have subscribed to a
            # backend that did not exist, but a client's level predates it and applies.
            for name in plan.changed + plan.added:
                await self.apply_log_level(only=name)

            logger.info(
                "reload: %d added, %d restarted, %d removed, %d unchanged, %d failed",
                len(plan.added),
                len(plan.changed),
                len(plan.removed),
                len(plan.unchanged),
                len(plan.failed),
            )
            return text_result(plan.as_dict())

    # --- lifecycle ----------------------------------------------------------------------

    def make_session(self, link: ClientLink) -> Session:
        return Session(link, self)

    def make_admin_connection(self, link: ClientLink) -> AdminConnection:
        return AdminConnection(link, self)

    async def start(
        self,
        *,
        access_key: str | None = None,
        allow_unauthenticated: bool = False,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        """Bring backends up, **then** bind the socket.

        In that order deliberately: the first client to attach should find a warm pool, and
        a daemon with no clients still has to be able to answer `/admin` about what is
        running. Binding first would let a client connect into a half-populated catalogue
        and cache an answer that was true for a second.
        """
        self._access_key = access_key
        self._attach_log_stream()
        await self.start_backends()
        self.server = GatewayServer(
            self.make_session,
            self.make_admin_connection,
            host=self.host,
            port=self.port,
            access_key=access_key,
            allow_unauthenticated=allow_unauthenticated,
            tls=tls,
            panel_store=self.panels,
        )
        await self.server.start()

    def _attach_log_stream(self) -> None:
        """Put the log capture on the root logger, **after** the redaction filter.

        Ordering is the whole safety argument: this handler formats records and sends them
        over a socket, so it must never be the thing that reaches a credential first. The
        filter lives on the root logger's existing handlers, so a handler added here would
        bypass it -- hence the filter is copied onto this one explicitly.
        """
        if self.log_stream is not None:
            return
        self.log_stream = LogStream()
        root = logging.getLogger()
        for handler in root.handlers:
            for existing in handler.filters:
                if isinstance(existing, RedactingFilter):
                    self.log_stream.addFilter(existing)
        root.addHandler(self.log_stream)

    async def start_backends(self) -> None:
        await self.supervisor.start_all()
        if any(b.spec.idle_ttl is not None for b in self.supervisor.all):
            self._idle_sweeper = asyncio.create_task(self._sweep_idle_backends())
        self._recovery_sweeper = asyncio.create_task(self._recover_failed_backends())

    async def _recover_failed_backends(self) -> None:
        """Try a failed backend again once its backoff has elapsed.

        **Unconditional, unlike the idle sweeper beside it, and the difference is the
        point.** `idle_ttl` is a feature a deployment opts into, so a daemon nobody
        configured it on should not carry a task waking up to find nothing to do. Recovery
        is not a feature anybody opts into: every deployment wants a backend that failed at
        startup to come back, and the work when nothing has failed is one pass over a list
        that is usually empty.

        What this fixes is ordinary rather than exotic. Compose starts a gateway alongside
        the containers it proxies; whichever one is not listening in the second the gateway
        dials it was recorded as failed and stayed that way, with `curl` on the box reaching
        the port perfectly well. A `SIGHUP` did not help either, because reload restarts
        only what *changed* and nothing had.

        Nothing new decides the timing. `Backend._fail` already sets the floor, and
        `restart_backoff_seconds` already ramps 0, 1, 2, 4, 8 ... to a one-minute cap -- so
        a dependency a moment late is retried almost at once, and a backend that is simply
        broken is dialled once a minute rather than hammered. That cap is also the answer to
        the obvious objection: a `url:` backend is somebody else's process, and once a minute
        against a server the operator configured is politeness, where never is a gateway that
        needs nursing after every deploy.

        Only `FAILED` is reconsidered. `DISABLED` and `STOPPED` are somebody's decision and
        `IDLE` is the sleep feature working; reviving any of those would be this loop
        overruling a person.
        """
        try:
            while True:
                await asyncio.sleep(RECOVERY_SWEEP_SECONDS)
                await self.recover_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the sweeper must not take the daemon down
            logger.exception("recovery sweep failed")

    async def recover_once(self) -> list[str]:
        """One pass of the loop above, returning the backends that came back.

        Separate from the timer for the same reason `supervisor.sweep_idle` is: the rule
        about *which* backends are eligible is worth testing without a test that sleeps.
        """
        recovered: list[str] = []
        for backend in self.supervisor.all:
            if backend.status is not BackendStatus.FAILED or backend.cooling_off:
                continue
            logger.info("backend %r: retrying a failed start", backend.name)
            if await self.supervisor.restart(backend.name):
                recovered.append(backend.name)
                # Announced only on success. A failed attempt changes nothing a client could
                # act on, and announcing one every minute for a backend that is simply broken
                # would be a notification storm about no news.
                await self._announce_backend_change(backend.name)
        return recovered

    async def _sweep_idle_backends(self) -> None:
        """Put unused backends to sleep, on a timer. See `supervisor.sweep_idle`.

        Started only when some backend actually sets `idle_ttl`, so a deployment that does
        not use the feature does not get a task waking up forever to find nothing to do.
        """
        try:
            while True:
                await asyncio.sleep(IDLE_SWEEP_SECONDS)
                await self.supervisor.sweep_idle(exempt=self._subscribed_backends())
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the sweeper must not take the daemon down
            logger.exception("idle sweep failed")

    def _subscribed_backends(self) -> frozenset[str]:
        """Backends a client is watching a resource on, which must not be slept.

        A sleeping process cannot send `notifications/resources/updated`, and a client
        watching a resource cannot tell silence from nothing having changed. Reclaiming a
        process at the cost of quietly breaking a feature the client asked for is not a
        trade the daemon gets to make on its own.
        """
        return frozenset(
            naming.decode_resource_uri(uri)[0]
            for uri in self.subscriptions.watched_uris()
        )

    async def stop(self) -> None:
        for task in (self._idle_sweeper, self._recovery_sweeper):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._idle_sweeper = None
        self._recovery_sweeper = None
        await self.notifier.flush()
        if self.log_stream is not None:
            logging.getLogger().removeHandler(self.log_stream)
            self.log_stream = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
        await self.supervisor.close_all()

    async def serve_forever(self) -> None:
        assert self.server is not None, "call start() first"
        await self.server.serve_forever()


class ClientRefused(Exception):
    """A client answered a forwarded request with a JSON-RPC error.

    Carried as its own type so `backend_request` can hand the backend the client's *answer*
    -- code, message and data intact -- rather than replacing it with a gateway failure. The
    client refusing an elicitation is information the backend asked for.
    """

    def __init__(self, error: dict[str, Any]) -> None:
        self.code = error.get("code")
        self.message = error.get("message", "the client refused the request")
        self.data = error.get("data")
        super().__init__(self.message)
