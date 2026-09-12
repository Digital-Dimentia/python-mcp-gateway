"""One `/mcp` connection's state and method table.

A `Session` per connection; everything below it -- the supervisor, the catalogue, the
credential store -- is shared by the whole process. That split is what makes this a daemon
rather than a launcher, and it is not a style choice: `initialize` records *that* client's
declared capabilities and *that* connection's negotiated protocol version, so a shared
object would have the second client silently answered with the first one's gates.

## What a client may do before `initialize`

`initialize` and `ping`, and nothing else. A client that jumps straight to `tools/list` is
answered `-32600` naming the problem rather than served, because its capabilities are
unknown at that point and answering would mean guessing them.

`ping` is exempt because a load balancer or a supervisor may well ping before any client
has spoken, and refusing that would make the daemon look down when it is up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from mcp_gateway import errors, jsonrpc, protocol
from mcp_gateway.transport_ws import ClientLink

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

#: Answerable before the handshake completes. See the module docstring.
_PRE_INITIALIZE = frozenset({protocol.INITIALIZE, protocol.PING})


class Session:
    """The MCP conversation on one connection."""

    def __init__(self, link: ClientLink, gateway: Any) -> None:
        self.link = link
        self.gateway = gateway
        self.connected_at = time.time()
        self.initialized = False
        self.protocol_version: str | None = None
        #: What this client said *it* can do. The union of these across live sessions is
        #: what the gateway declares downward to each backend; see `gateway.py`.
        self.client_capabilities: dict[str, Any] = {}
        self.client_info: dict[str, Any] = {}
        #: Requests this client has in flight, by its own id, so `notifications/cancelled`
        #: can reach the right task. The cancellation then propagates down through
        #: `MCPStdioClient.request` as a `notifications/cancelled` on the backend's wire.
        self._in_flight: dict[Any, asyncio.Task[Any]] = {}
        self._handlers: dict[str, Handler] = {
            protocol.INITIALIZE: self._initialize,
            protocol.PING: self._ping,
            protocol.TOOLS_LIST: self._tools_list,
            protocol.TOOLS_CALL: self._tools_call,
            protocol.PROMPTS_LIST: self._prompts_list,
            protocol.PROMPTS_GET: self._prompts_get,
            protocol.RESOURCES_LIST: self._resources_list,
            protocol.RESOURCES_TEMPLATES_LIST: self._resources_templates_list,
            protocol.RESOURCES_READ: self._resources_read,
            protocol.COMPLETION_COMPLETE: self._complete,
        }

    # --- the transport's interface ------------------------------------------------------

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        kind = jsonrpc.classify(message)
        method = message.get("method")
        params = message.get("params") or {}

        if kind is jsonrpc.Kind.NOTIFICATION:
            await self._notification(method, params)
            return None
        if kind is jsonrpc.Kind.RESPONSE:
            # An answer to something *we* asked this client -- a forwarded `roots/list`,
            # `sampling/createMessage` or `elicitation/create`. See `gateway.py`.
            self.gateway.resolve_client_response(self, message)
            return None

        if method not in self._handlers:
            raise errors.GatewayErrorObject(errors.MethodNotFound(str(method)).to_error_object())
        if not self.initialized and method not in _PRE_INITIALIZE:
            raise errors.GatewayErrorObject(
                errors.error_object(
                    errors.INVALID_REQUEST,
                    f"{method} before initialize; the gateway does not know this client's "
                    f"capabilities yet",
                )
            )
        return await self._dispatch(method, params, jsonrpc.request_id_of(message))

    async def closed(self) -> None:
        """The client went away."""
        for task in list(self._in_flight.values()):
            task.cancel()
        self._in_flight.clear()
        await self.gateway.session_closed(self)

    # --- dispatch -----------------------------------------------------------------------

    @errors.as_error_object
    async def _dispatch(self, method: str, params: dict, request_id: Any) -> dict[str, Any]:
        """Run one handler, registered so `notifications/cancelled` can find it.

        The task is *this* coroutine's own, obtained from `current_task`, rather than a new
        one: the transport already gave every message a task, and wrapping again would make
        the cancellation land on a wrapper while the real work carried on.
        """
        task = asyncio.current_task()
        if request_id is not None and task is not None:
            self._in_flight[request_id] = task
        try:
            return await self._handlers[method](params)
        finally:
            if request_id is not None:
                self._in_flight.pop(request_id, None)

    async def _notification(self, method: str | None, params: dict) -> None:
        if method == protocol.INITIALIZED:
            self.initialized = True
            return
        if method == protocol.CANCELLED:
            request_id = params.get("requestId")
            task = self._in_flight.get(request_id)
            if task is not None:
                logger.debug("client cancelled request %s", request_id)
                task.cancel()
            return
        logger.debug("ignoring client notification %s", method)

    # --- handlers -----------------------------------------------------------------------

    async def _initialize(self, params: dict) -> dict[str, Any]:
        """Answered **immediately, from configuration**, never from live backend state.

        Clients time `initialize` out, and a daemon may hold a dozen backends that each
        take an `npx` cold start to come up. Blocking the handshake behind them is how a
        gateway becomes unusable on a cold cache. Backends are started when the daemon
        starts, not when a client arrives; a client that lists before the first sweep
        finishes waits on the supervisor's readiness event instead, which is a wait it can
        see the result of.
        """
        version, agreed = protocol.negotiate_version(params.get("protocolVersion"))
        self.protocol_version = version
        self.client_capabilities = params.get("capabilities") or {}
        self.client_info = params.get("clientInfo") or {}
        if not agreed:
            logger.info(
                "client proposed %r, countering with %s",
                params.get("protocolVersion"),
                version,
            )
        # The handshake is not complete until `notifications/initialized`, but a client's
        # capabilities are usable now -- and the gateway needs them now, because they
        # change what it declares to backends.
        await self.gateway.session_ready(self)
        return {
            "protocolVersion": version,
            "capabilities": protocol.advertised_capabilities(),
            "serverInfo": self.gateway.server_info(),
            "instructions": self.gateway.instructions(),
        }

    async def _ping(self, _params: dict) -> dict[str, Any]:
        return {}

    async def _tools_list(self, params: dict) -> dict[str, Any]:
        return await self.gateway.list_tools(params)

    async def _tools_call(self, params: dict) -> dict[str, Any]:
        return await self.gateway.call_tool(self, params)

    async def _prompts_list(self, params: dict) -> dict[str, Any]:
        return await self.gateway.list_prompts(params)

    async def _prompts_get(self, params: dict) -> dict[str, Any]:
        return await self.gateway.get_prompt(self, params)

    async def _resources_list(self, params: dict) -> dict[str, Any]:
        return await self.gateway.list_resources(params)

    async def _resources_templates_list(self, params: dict) -> dict[str, Any]:
        return await self.gateway.list_resource_templates(params)

    async def _resources_read(self, params: dict) -> dict[str, Any]:
        return await self.gateway.read_resource(self, params)

    async def _complete(self, params: dict) -> dict[str, Any]:
        return await self.gateway.complete(self, params)
