from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from mcp_gateway import __version__
from mcp_gateway.protocol import (
    MCP_PROTOCOL_VERSION as _MCP_PROTOCOL_VERSION,
)
from mcp_gateway.protocol import (
    SUPPORTED_MCP_PROTOCOL_VERSIONS as _SUPPORTED_MCP_PROTOCOL_VERSIONS,
)

logger = logging.getLogger("mcp_gateway.mcp_stdio")

# DIVERGENCE FROM THE TEMPLATE (3 of 3). Upstream held the protocol constants here,
# because it spoke MCP in one direction only. The gateway speaks it in both, and the
# server side in `transport_ws.py` has to negotiate against the same set -- so two copies
# would be two things to keep in step, and a mismatch between what we accept from a
# backend and what we accept from a client is a silent interop bug rather than a failure.
# They live in `protocol.py`; this is the only non-stdlib import the module gains.

ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class UnsupportedServerRequest(Exception):
    """An `on_server_request` handler's way of saying "not me".

    Without it a handler has only two outcomes — a result, or an exception that
    becomes `-32603`. Neither is right for a method the handler does not
    implement: `-32603` says *we broke*, when the truth is *we never offered
    this*. That distinction became load-bearing the moment this client started
    declaring capabilities, because a server may still send a request outside
    what we declared and MUST be told which kind of "no" it got.

    `_handle_server_request` turns this into `-32601`, the same reply a client
    with no handler at all gives.
    """


class MalformedServerRequest(Exception):
    """An `on_server_request` handler's way of saying "your params are wrong".

    The third answer a handler needs, and the only one it cannot express without
    this: a result means yes, `UnsupportedServerRequest` means "we never offered
    this", and every other exception means "we broke". A server that sent a
    request we *do* serve, with params we cannot read, is none of those — it is
    the server's mistake, and `-32603` would pin it on us.

    `_handle_server_request` turns this into `-32602`, which is what a JSON-RPC
    peer says about bad params in either direction.
    """


@dataclass(frozen=True)
class MCPClientCapabilities:
    """What this client promises an MCP server it can answer.

    A capability block is a **promise**, not a wish list: MCP says a server MUST
    NOT use a capability the client did not declare, and by symmetry a client
    that declares one MUST be able to answer it. Declaring something nothing
    answers is worse than declaring nothing, because the server will call it and
    strand itself on a `-32601` it was told would not happen. So this is
    deliberately explicit — the caller states what it wired up, and
    `MCPStdioClient.initialize` refuses to send a non-empty block when no
    `on_server_request` handler exists to receive the traffic.

    **`sampling` has no field, on purpose.** There is no LLM in this runtime, so
    `sampling/createMessage` has no answer here and never will; leaving the
    field out means the block cannot be built wrong rather than trusting every
    caller to remember. `roots` and `elicitation` are the two we can actually
    serve — see `gateway.py`, which forwards both up to a client connection.
    """

    #: We can answer `roots/list`. `gateway.py` sets this when an attached client
    #: declared `roots` of its own, and answers by forwarding the request up: the
    #: gateway never invents roots, because it has no filesystem view to invent them from.
    roots: bool = False
    #: We will send `notifications/roots/list_changed`. Only meaningful with
    #: `roots`, and false today.
    roots_list_changed: bool = False
    #: We can answer `elicitation/create`. Requires the negotiated revision to be
    #: `2025-06-18` or later — which is why `_MCP_PROTOCOL_VERSION` moved.
    #: `gateway.py` answers it by forwarding the question to a client connection.
    elicitation: bool = False

    def __post_init__(self) -> None:
        if self.roots_list_changed and not self.roots:
            raise ValueError(
                "roots_list_changed without roots: there is no list to change"
            )

    def to_wire(self) -> dict[str, Any]:
        """The `capabilities` member of the `initialize` request.

        Absent means unsupported, so an undeclared capability contributes no key
        at all rather than a `false`.
        """
        block: dict[str, Any] = {}
        if self.roots:
            block["roots"] = {"listChanged": self.roots_list_changed}
        if self.elicitation:
            block["elicitation"] = {}
        return block


class MCPProtocolError(RuntimeError):
    """Raised when the MCP service responds with an error.

    Carries the originating JSON-RPC error `code` and `data` when the failure
    came from the server as an error *response*. Both are `None` for failures
    this client raises itself — timeouts, transport death, malformed results —
    because those have no server-assigned code. Callers use that difference to
    decide whether a code can be forwarded or must fall back to a generic one.

    A failed tool call is **not** one of these. `tools/call` reports tool-level
    failure through `isError` on a successful result; see `call_tool`.
    """

    #: How `errors.to_error_object` recognises "this came off a backend's wire" without
    #: importing this module. See `errors.md`: a marker inverts a dependency that would
    #: otherwise be a cycle, since `errors` is imported by `config` and `naming`.
    mcp_error = True

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = None,
        backend: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        #: Which backend produced it. Set by `backend.py` as the error passes through, so
        #: the name reaches `data["backend"]` in the client-facing error object without
        #: every call site having to remember to pass it.
        self.backend = backend

    @classmethod
    def from_error_response(cls, error: Any) -> "MCPProtocolError":
        """Build from the `error` member of a JSON-RPC error response.

        A conforming server sends `{"code": int, "message": str}` with optional
        `data`. Anything else is kept as an unstructured message with no code,
        so a malformed server degrades to the generic path instead of putting
        junk on the client-facing wire.
        """
        if not isinstance(error, dict):
            return cls(f"MCP error: {error}")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, int):
            return cls(f"MCP error: {error}")
        text = message if isinstance(message, str) and message else "Unknown MCP error"
        return cls(f"MCP error {code}: {text}", code=code, data=error.get("data"))


@dataclass
class MCPStdioClient:
    """JSON-RPC client speaking to an MCP server over the subprocess's stdio.

    MCP is bidirectional: the server may send requests and notifications of its
    own at any time, not only responses to ours. A background read loop consumes
    every stdout message and routes it by shape, so nothing is dropped and the
    server never blocks waiting on a request we ignored.

    **The read loop answers nothing itself.** Each server request is handled in a
    task of its own, because a handler may wait on a human — `elicitation/create`
    does — and a read loop parked inside one would stop reading everything else on
    the connection, including the response to the call that provoked it.
    """

    command: Sequence[str]
    request_timeout: float = 30.0
    #: Environment variables for the subprocess. How they combine with this process's
    #: own environment is decided by `env_replace`.
    env: Mapping[str, str] | None = None
    #: DIVERGENCE FROM THE TEMPLATE (1 of 3). Whether `env` **replaces** the environment
    #: rather than overlaying it.
    #:
    #: Upstream had two states -- `None` meaning inherit everything, a mapping meaning
    #: overlay -- and defended that with: a server command almost always needs `PATH` and
    #: `HOME`, and it is not a sandbox boundary either way, because whoever supplies `env`
    #: also supplies `command`.
    #:
    #: **That last clause is exactly what is different here, and it is the whole product.**
    #: The operator writes one file holding every credential for every backend, and the
    #: claim being made is that each backend receives only its own. Inherit the parent
    #: environment and the Slack backend gets `GITHUB_TOKEN` the moment the operator
    #: exported it in the shell that started the daemon -- and the consolidation claim is
    #: simply false. A daemon's parent environment is also whatever launchd or a login
    #: shell handed it, which routinely includes `ANTHROPIC_API_KEY`, AWS credentials and
    #: `SSH_AUTH_SOCK`, and it persists for weeks.
    #:
    #: So there is a third state. `backend.py` builds a complete environment -- a base
    #: allowlist, the server's `env_passthrough`, then its own resolved `env` -- and passes
    #: it with `env_replace=True`. The default stays `False` so the two-state behaviour
    #: above is unchanged for any other caller.
    env_replace: bool = False
    #: DIVERGENCE FROM THE TEMPLATE (4 of 4). Working directory for the subprocess.
    #:
    #: Upstream had no such field: every server it launched was named by a client that had
    #: already chosen where it ran. Here `servers.yaml` names a `cwd` per backend, and the
    #: obvious alternative -- chdir'ing this process -- is not available, because the
    #: daemon is shared by every backend and by the socket it serves.
    #:
    #: `None` means "inherit the daemon's", which is `create_subprocess_exec`'s own
    #: default. The directory is **not** created: a `cwd` that does not exist is a
    #: configuration error worth a clear failure at startup, not something to paper over.
    cwd: str | None = None
    on_server_request: ServerRequestHandler | None = field(default=None)
    on_notification: NotificationHandler | None = field(default=None)
    #: What `initialize` will promise this server. Empty by default: a client
    #: that has wired up no handler can answer nothing, and saying so is the
    #: only honest block. See `MCPClientCapabilities`.
    client_capabilities: MCPClientCapabilities = field(default=MCPClientCapabilities())

    # Bare assignments (no annotation) stay class attributes, not fields.
    _STDERR_CHUNK = 4096
    # asyncio caps stream readers at 64 KiB by default, which a large
    # resources/read response can exceed. Raise it so whole messages fit.
    _STREAM_LIMIT = 8 * 1024 * 1024
    # Cursor pagination is driven entirely by the server, so a broken or hostile
    # one can keep handing out cursors forever. Bound the walk and fail loudly.
    _MAX_LIST_PAGES = 100
    # Shutdown budget, per the MCP stdio shutdown sequence: how long a server
    # gets to exit on EOF before SIGTERM, and after SIGTERM before SIGKILL.
    _STOP_STDIN_TIMEOUT = 2.0
    _STOP_TERMINATE_TIMEOUT = 2.0

    def __post_init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        # The version both sides settled on, set by initialize() and None until
        # then. A handshake that fails negotiation leaves it None.
        self.protocol_version: str | None = None
        # What the server said it can do, kept from the initialize result. `None`
        # until the handshake completes, and `{}` for a server that declared
        # nothing -- two different facts, which is why the default is not `{}`.
        self.server_capabilities: dict[str, Any] | None = None
        self._id = 0
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        # One task per server request in flight. They are not awaited by the read
        # loop -- see `_handle_message` -- so something has to hold a reference or
        # the event loop may garbage-collect a running task mid-answer.
        self._server_requests: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._STREAM_LIMIT,
            env=self._child_environment(),
            cwd=self.cwd,
        )
        self._stdout_task = asyncio.create_task(self._read_loop(self._proc))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))

    def _child_environment(self) -> dict[str, str] | None:
        """The subprocess's environment. `None` means "inherit this process's".

        Three states, not two. See the `env_replace` field for why the third exists.
        """
        if self.env is None:
            return None
        if self.env_replace:
            return dict(self.env)
        return {**os.environ, **self.env}

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # Before the subprocess goes: a handler still waiting on a human is waiting
        # for a session that is being torn down, and holding it open would spend
        # both shutdown timeouts on an answer that has nowhere left to go.
        await self._cancel_server_requests()
        try:
            await self._shutdown_process(proc)
        finally:
            # The read loop is cancelled only after the process is gone, so the
            # server's final stdout output is still consumed on the way out.
            await self._cancel_task("_stdout_task")
            await self._cancel_task("_stderr_task")
            # Again, and this is the call that actually guarantees the set is empty:
            # the read loop was still running during the shutdown above, so a server
            # getting one last request out would have been tracked after the first
            # sweep. Nothing can create one now.
            await self._cancel_server_requests()
            self._fail_pending(MCPProtocolError("MCP process stopped"))
            self._proc = None

    async def _shutdown_process(self, proc: asyncio.subprocess.Process) -> None:
        """Shut the server down the way the MCP stdio transport prescribes.

        Close the server's stdin, wait for it to exit on EOF, escalate to
        SIGTERM, wait again, then SIGKILL. Starting at SIGTERM would signal
        every server that shuts down cleanly on EOF -- which is most of them,
        and is the documented contract they are written against.
        """
        if proc.returncode is not None:
            return

        await self._close_stdin(proc)
        if await self._wait_for_exit(proc, self._STOP_STDIN_TIMEOUT):
            return

        if self._signal(proc, "terminate") and await self._wait_for_exit(
            proc, self._STOP_TERMINATE_TIMEOUT
        ):
            return

        self._signal(proc, "kill")
        await proc.wait()

    async def _close_stdin(self, proc: asyncio.subprocess.Process) -> None:
        """Close the server's stdin and wait for the pipe to actually shut."""
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            if not stdin.is_closing():
                stdin.close()
            await asyncio.wait_for(stdin.wait_closed(), timeout=self._STOP_STDIN_TIMEOUT)
        except Exception:
            # Best-effort: a broken pipe here just means the server is already
            # gone, and nothing about it should stop the rest of the shutdown.
            logger.debug("Closing MCP stdin did not complete cleanly", exc_info=True)

    @staticmethod
    async def _wait_for_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, action: str) -> bool:
        """Send SIGTERM/SIGKILL, tolerating a process reaped in the meantime."""
        try:
            getattr(proc, action)()
        except ProcessLookupError:
            return False
        return True

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        """Run the MCP handshake and settle on a protocol version.

        Negotiation is a real round trip, not a formality: we propose
        `_MCP_PROTOCOL_VERSION`, and the server replies with the revision it
        will actually use — which need not be the one we asked for. A server
        that cannot speak our proposal MUST counter with one it supports, and a
        client that cannot speak the counter MUST hang up instead of carrying
        on. So a rejected version stops the subprocess before the error escapes,
        and `notifications/initialized` is never sent on that path: half a
        handshake is worse than none, because the mismatch would otherwise
        resurface later as unrelated-looking failures.

        The `capabilities` block is the other half of the same handshake, and it
        is checked before it is sent — see `_declared_capabilities`.
        """
        result = await self.request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": self._declared_capabilities(),
                # DIVERGENCE FROM THE TEMPLATE (2 of 3). Imported rather than written out
                # again: a second version literal is a second thing to forget to bump, and
                # this one is what a backend's own logs will name when it reports trouble.
                "clientInfo": {"name": "mcp-gateway", "version": __version__},
            },
        )
        try:
            self.protocol_version = self._agreed_protocol_version(result)
        except MCPProtocolError:
            await self.stop()
            raise
        # Kept, not read and discarded. MCP's rule runs both ways: a client MUST NOT
        # use a capability the server did not declare, and a server that omits
        # `prompts` answers `prompts/list` with `-32601`. Without this the only way to
        # find that out is to ask and be refused, which cannot tell a server with no
        # prompts from one whose listing is broken. See `supports`.
        capabilities = result.get("capabilities")
        self.server_capabilities = capabilities if isinstance(capabilities, dict) else {}
        await self.notify("notifications/initialized")
        return result

    def supports(self, capability: str) -> bool:
        """Whether the server declared `capability` in its `initialize` result.

        Presence is the whole test: MCP capability values are option blocks
        (`{"listChanged": true}`) and an empty one still means the feature is there,
        so `bool(block)` would read `"prompts": {}` -- the commonest form there is --
        as unsupported.

        **True before the handshake**, because `server_capabilities` is `None` then and
        the honest answer to "may I call this" is not "no". A caller reaching a method
        before `initialize` has a worse problem than a capability check can describe,
        and refusing here would replace its real error with a misleading one.
        """
        if self.server_capabilities is None:
            return True
        return capability in self.server_capabilities

    def supports_option(self, capability: str, option: str) -> bool:
        """Whether a capability's *option block* sets one flag, e.g. `resources.subscribe`.

        Unlike `supports`, presence is not enough: these are booleans inside the block, and
        `"resources": {}` means resources without subscriptions. Also true before the
        handshake, and for the same reason.
        """
        if self.server_capabilities is None:
            return True
        block = self.server_capabilities.get(capability)
        return isinstance(block, dict) and bool(block.get(option))

    def _declared_capabilities(self) -> dict[str, Any]:
        """The capability block to send, refusing to promise what nobody answers.

        Every declared capability becomes a request the server is entitled to
        send, and `on_server_request` is the only thing that can answer one.
        Declaring without a handler is a conformance bug in *this* process, not
        a bad input, so it is a `RuntimeError` and it fires before the promise
        reaches the wire rather than as a `-32601` the server gets much later.

        The check is presence, not coverage: one callable stands behind every
        capability and nothing here can tell which methods it actually handles.
        A handler that is offered a declared method it does not implement should
        raise `UnsupportedServerRequest`.
        """
        block = self.client_capabilities.to_wire()
        if block and self.on_server_request is None:
            raise RuntimeError(
                f"MCP client declares {sorted(block)} but has no on_server_request "
                "handler to answer them"
            )
        return block

    @staticmethod
    def _agreed_protocol_version(result: dict[str, Any]) -> str:
        """Validate the server's `initialize` answer, or raise.

        The result MUST carry `protocolVersion`; a server that omits it is not
        speaking the lifecycle we asked for, so an absent field is as fatal as
        an unusable one.
        """
        supported = ", ".join(sorted(_SUPPORTED_MCP_PROTOCOL_VERSIONS))
        version = result.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise MCPProtocolError(
                "MCP initialize result omitted protocolVersion; "
                f"mcp-gateway proposed {_MCP_PROTOCOL_VERSION} and supports {supported}"
            )
        if version not in _SUPPORTED_MCP_PROTOCOL_VERSIONS:
            raise MCPProtocolError(
                f"Unsupported MCP protocol version {version} from server; "
                f"mcp-gateway proposed {_MCP_PROTOCOL_VERSION} and supports {supported}"
            )
        if version != _MCP_PROTOCOL_VERSION:
            logger.info(
                "MCP server countered protocol version %s with %s",
                _MCP_PROTOCOL_VERSION,
                version,
            )
        return version

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._list_all("tools/list", "tools")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke an MCP tool.

        **A tool that fails is not a protocol error.** MCP reports tool-level
        failure as a *successful* JSON-RPC result carrying `isError: true` and
        content explaining the failure — deliberately, so the caller can read
        what went wrong. Only a JSON-RPC error response (the tool is unknown,
        the arguments are invalid) raises `MCPProtocolError`.

        `isError` is optional on the wire and defaults to false. It is filled in
        here so callers can read the flag unconditionally rather than each
        re-deriving the default.
        """
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})

        content = result.get("content", [])
        if not isinstance(content, list):
            raise MCPProtocolError("Invalid tools/call response: 'content' must be an array")

        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise MCPProtocolError("Invalid tools/call response: 'isError' must be a boolean")

        result["content"] = content
        result["isError"] = is_error
        return result

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list_all("prompts/list", "prompts")

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("prompts/get", {"name": name, "arguments": arguments or {}})

    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._list_all("resources/list", "resources")

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        """The server's URI *templates* — a separate method from `resources/list`.

        MCP publishes a template like `greeting://{name}` through
        `resources/templates/list` only, with its own result key and its own field
        name: `{"resourceTemplates": [{uriTemplate, name, description, mimeType}]}`.
        Note `uriTemplate`, not `uri`.

        That separation is why a server whose resources are *all* templates — a
        filesystem server publishing `file:///{path}` — answers `resources/list` with an
        empty page while having plenty to read. A caller that asks only the one method
        reports it as having nothing, which is a confident wrong picture rather than a
        missing feature. The gateway asks both and merges them; see `catalogue.py`.

        Both methods sit behind the same `resources` capability, and templates are
        optional within it: a server that declares `resources` may still answer `-32601`
        here. That is the caller's to absorb as an empty section.
        """
        return await self._list_all("resources/templates/list", "resourceTemplates")

    async def _list_all(self, method: str, key: str) -> list[dict[str, Any]]:
        """Walk an MCP cursor-paginated list method to exhaustion.

        MCP list results carry `nextCursor` when more pages exist; the client
        re-issues the request with `cursor` set until the field is absent.
        **An absent `nextCursor` is the only terminator** — an empty page in the
        middle of a walk is legal and does not mean the end.

        The loop is entirely server-driven, so it is bounded twice: a cursor the
        server has already handed out, and a hard page ceiling. Either one raises
        `MCPProtocolError` rather than hanging the bridge forever.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for _ in range(self._MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            response = await self.request(method, params)

            page = response.get(key, [])
            if not isinstance(page, list):
                raise MCPProtocolError(f"Invalid {method} response")
            items.extend(page)

            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return items
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MCPProtocolError(f"Invalid nextCursor in {method} response")
            if next_cursor in seen_cursors:
                raise MCPProtocolError(f"{method} repeated cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise MCPProtocolError(f"{method} exceeded {self._MAX_LIST_PAGES} pages")

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one resource. `resources/read` params are `{uri}` and nothing else.

        There is deliberately no `arguments` here. A *templated* resource —
        `greeting://{name}`, advertised by `resources/templates/list` rather than
        `resources/list` — is expanded **client-side** into a concrete URI (RFC 6570)
        and only then read. So the substitution never crosses the wire: passing one
        would be a param the spec does not define, which a real server ignores while
        a permissive mock quietly honours it.

        The gateway forwards templates to its own client and lets *that* client expand
        them, so the substitution still never happens here.
        """
        return await self.request("resources/read", {"uri": uri})

    async def set_log_level(self, level: str) -> dict[str, Any]:
        """Ask this server to send `notifications/message` at `level` and above.

        The gateway sends the most verbose level *any* of its clients asked for and filters
        per client on the way up, because one process is serving all of them and MCP has
        no per-subscriber level on the wire.
        """
        return await self.request("logging/setLevel", {"level": level})

    async def subscribe_resource(self, uri: str) -> dict[str, Any]:
        """Ask to be told when one resource changes: `resources/subscribe`.

        The result is an empty object on success; what matters is that it did not raise.
        A server that does not offer `subscribe` answers `-32601`, and that refusal is
        propagated rather than swallowed -- see `notifications.md`.
        """
        return await self.request("resources/subscribe", {"uri": uri})

    async def unsubscribe_resource(self, uri: str) -> dict[str, Any]:
        """Stop being told: `resources/unsubscribe`."""
        return await self.request("resources/unsubscribe", {"uri": uri})

    async def complete(
        self,
        ref: dict[str, Any],
        argument: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask for the values one argument might take: `completion/complete`.

        `ref` names what is being filled in — a prompt by name, or a resource template by
        its `uriTemplate`, in **this backend's own spelling**. Unwrapping the gateway's
        namespace is the caller's job, because the caller is the one that resolved which
        backend this is.

        `context` is **omitted rather than sent as null** when there is none. Presence is
        meaningful in MCP, and a `null` where a server expects an object is the kind of
        thing a strict validator refuses and a permissive one ignores — a difference that
        shows up as one server's inexplicable `-32602`.

        Whether `context` may be sent at all is likewise the caller's call: it does not
        exist before `2025-03-26`, and this method does not know what was negotiated. See
        `router.py`.
        """
        params: dict[str, Any] = {"ref": ref, "argument": argument}
        if context is not None:
            params["context"] = context
        return await self.request("completion/complete", params)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        async with self._write_lock:
            await self._write(payload)

    async def cancel_request(self, request_id: int, reason: str | None = None) -> None:
        """Tell the server to stop working on a request whose answer we abandoned.

        JSON-RPC has no in-band cancel, so a request we stop waiting for leaves
        the server computing a reply nobody will read. MCP's remedy is
        `notifications/cancelled` carrying the `requestId` and an optional
        human-readable `reason`.

        This is the *whole* "tell the MCP server to stop" path, kept as one
        reusable method rather than buried in the timeout branch, so cancelling
        for any other reason — a client's `notifications/cancelled`, a connection that dropped —
        is this same call with a different `reason`.

        Two deliberate properties:

        - **It never raises.** A dead subprocess has nothing left to cancel, and
          a failure here must not mask the failure that prompted the cancel.
          That covers the OSError family too: `_write` reaches `drain()`, so a
          subprocess dying *mid-write* — after the `is_closing()` guard passed —
          surfaces as `BrokenPipeError`/`ConnectionResetError` rather than
          `MCPProtocolError`, and letting one escape would replace the timeout
          error the caller is about to raise with an unrelated OSError.
        - **It does not touch `_pending`.** Whoever abandoned the request owns
          that future; this method only puts the notification on the wire.

        The notification and a real response can cross in flight — that race is
        expected on both sides, and a late reply for a forgotten id is discarded
        by `_resolve_response`.
        """
        params: dict[str, Any] = {"requestId": request_id}
        if reason:
            params["reason"] = reason
        try:
            await self.notify("notifications/cancelled", params)
        except (MCPProtocolError, OSError):
            # Best-effort by contract: log where the courtesy went undelivered.
            logger.debug("Could not cancel MCP request %r", request_id, exc_info=True)

    async def _abandon(self, request_id: int, method: str, reason: str) -> None:
        """Stop waiting for a request, and tell the server to stop working on it.

        The forgetting comes first: a reply that crosses the notification in flight then
        finds no pending future and is discarded by `_resolve_response`, which is the
        outcome we want either way.

        `initialize` is the one request a client MUST NOT cancel — there is no session for
        the server to abandon yet, and the lifecycle defines no state after a cancelled
        handshake — so it is forgotten without a notification.

        The notification goes out under `asyncio.shield` because one caller of this is an
        `except asyncio.CancelledError` handler: if a *second* cancellation lands while
        the write is in flight, the shielded write still completes and the cancellation
        still reaches us at the `await`. `cancel_request` itself never raises.
        """
        self._pending.pop(request_id, None)
        if method == "initialize":
            return
        await asyncio.shield(self.cancel_request(request_id, reason))

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        # The lock covers id allocation and the write only. Waiting for the reply
        # happens outside it, so concurrent requests pipeline instead of queueing.
        async with self._write_lock:
            self._id += 1
            request_id = self._id
            self._pending[request_id] = future
            try:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            except BaseException:
                self._pending.pop(request_id, None)
                raise

        try:
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            await self._abandon(
                request_id,
                method,
                f"mcp-gateway timed out after {self.request_timeout}s waiting for {method}",
            )
            raise MCPProtocolError(
                f"Timed out waiting for MCP response to {method}"
            ) from exc
        except asyncio.CancelledError:
            # Our *caller* was cancelled — a client's `notifications/cancelled`, or the
            # connection dropping out from under the call. A timeout and a cancellation
            # leave the server in exactly the same state: working on a reply nobody will
            # read. Same remedy, different reason text, and the cancellation still
            # propagates: returning here would tell asyncio the cancel did not take.
            await self._abandon(
                request_id, method, f"mcp-gateway abandoned {method}: the caller was cancelled"
            )
            raise
        finally:
            self._pending.pop(request_id, None)

        error = response.get("error")
        if error:
            raise MCPProtocolError.from_error_response(error)
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"Invalid response for method {method}")
        return result

    async def _write(self, payload: dict[str, Any]) -> None:
        stdin = self._proc.stdin if self._proc is not None else None
        # is_closing() covers the window inside stop() where stdin has been
        # closed but the process has not been reaped yet.
        if stdin is None or stdin.is_closing():
            raise MCPProtocolError("MCP process not running")
        stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await stdin.drain()

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        """Consume every stdout message for the life of the subprocess."""
        stream = proc.stdout
        if stream is None:
            return
        reason = "MCP process closed stdout"
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON MCP stdout line")
                    continue
                if not isinstance(message, dict):
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("MCP read loop failed", exc_info=True)
            reason = f"MCP read loop failed: {exc}"
        self._fail_pending(MCPProtocolError(reason))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if method is None:
            self._resolve_response(message_id, message)
            return

        if not isinstance(method, str) or not method:
            return

        if message_id is None:
            await self._handle_notification(method, params)
            return

        # A task, not an await: `on_server_request` may take arbitrarily long --
        # `elicitation/create` is forwarded up to a client and waits on a human --
        # and awaiting it here would stall the read loop for the whole duration.
        # Nothing else on this connection could be read meanwhile, including the
        # response to the very call that provoked the request. Replies may now leave
        # out of arrival order, which JSON-RPC allows: the id is what matches them.
        task = asyncio.create_task(self._handle_server_request(message_id, method, params))
        self._server_requests.add(task)
        task.add_done_callback(self._server_requests.discard)

    def _resolve_response(self, message_id: Any, message: dict[str, Any]) -> None:
        future = self._pending.pop(message_id, None) if message_id is not None else None
        if future is None:
            logger.debug("Discarding MCP response with unknown id %r", message_id)
            return
        if not future.done():
            future.set_result(message)

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.debug("MCP server notification: %s", method)
        if self.on_notification is None:
            return
        try:
            await self.on_notification(method, params)
        except Exception:
            # A misbehaving handler must not kill the read loop.
            logger.debug("MCP notification handler failed for %s", method, exc_info=True)

    async def _handle_server_request(
        self, request_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        """Answer a request the server sent us.

        Every server request gets a reply, even an error one — leaving it
        unanswered strands the server waiting on us.
        """
        logger.debug("MCP server request: %s", method)
        try:
            if method == "ping":
                # Protocol plumbing, not application logic: always answered here.
                result: dict[str, Any] = {}
            elif self.on_server_request is not None:
                result = await self.on_server_request(method, params)
            else:
                await self._respond_error(
                    request_id, -32601, f"Unsupported method: {method}", method
                )
                return
        except MalformedServerRequest as exc:
            # The server's mistake, not ours: it used a capability we really do
            # declare, with params this client cannot read.
            await self._respond_error(request_id, -32602, str(exc), method)
            return
        except UnsupportedServerRequest:
            # The handler exists but does not serve this method — which is a
            # different answer from "we broke", and the server needs to be able
            # to tell them apart. Same reply as having no handler at all.
            await self._respond_error(
                request_id, -32601, f"Unsupported method: {method}", method
            )
            return
        except Exception as exc:
            logger.debug("MCP server request handler failed for %s", method, exc_info=True)
            await self._respond_error(request_id, -32603, str(exc), method)
            return

        await self._respond(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, method=method
        )

    async def _respond_error(self, request_id: Any, code: int, message: str, method: str) -> None:
        """Reply to a server request with a JSON-RPC error.

        `method` is the method being answered, not the failure text. It exists
        only for `_respond`'s log line: when the reply itself cannot be written,
        the useful fact is which request went unanswered.
        """
        await self._respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            method=method,
        )

    async def _respond(self, payload: dict[str, Any], method: str) -> None:
        try:
            async with self._write_lock:
                await self._write(payload)
        except MCPProtocolError:
            # The process is already gone; nothing useful to do with the reply.
            logger.debug("Could not reply to MCP server request %s", method, exc_info=True)

    def _fail_pending(self, exc: MCPProtocolError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Continuously read the server's stderr so its pipe buffer cannot fill.

        stderr is piped, so nothing consuming it means the OS buffer fills and the
        server blocks mid-write — deadlocking every request on this client. Lines
        are logged at debug, which surfaces them under the CLI's --debug flag.
        """
        stream = proc.stderr
        if stream is None:
            return
        buffer = b""
        try:
            while True:
                chunk = await stream.read(self._STDERR_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    self._log_stderr_line(line)
                if len(buffer) > self._STDERR_CHUNK:
                    # One line longer than a chunk; flush it rather than letting
                    # the buffer grow without bound.
                    self._log_stderr_line(buffer)
                    buffer = b""
        except asyncio.CancelledError:
            raise
        except Exception:
            # Draining is best-effort; it must never take down the client.
            logger.debug("MCP stderr drain stopped early", exc_info=True)
        finally:
            if buffer:
                self._log_stderr_line(buffer)

    @staticmethod
    def _log_stderr_line(line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            logger.debug("MCP server stderr: %s", text)

    async def _cancel_server_requests(self) -> None:
        """Abandon every server request still being answered.

        The server is told nothing, because there is nothing useful to tell it: it
        is about to lose the connection either way, and a reply written into a
        closing stdin is no better than silence.
        """
        # A snapshot, and the live set is left alone: each task's done callback
        # discards itself from it, and awaiting below is exactly when those callbacks
        # run — iterating the set itself would change size mid-loop. Emptying it here
        # instead would strand a task created after this line with nothing tracking it.
        tasks = tuple(self._server_requests)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 -- logged; one handler must not strand the rest
                logger.debug("MCP server request handler failed on shutdown", exc_info=True)

    async def _cancel_task(self, attribute: str) -> None:
        task: asyncio.Task[None] | None = getattr(self, attribute)
        setattr(self, attribute, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
