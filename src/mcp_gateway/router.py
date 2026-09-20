"""Forward one call to the backend that owns it, and translate what comes back.

Five methods: `tools/call`, `prompts/get`, `resources/read`, and the two halves of
`resources/subscribe`. Each resolves a namespaced name to a backend, forwards the call
unchanged, and maps a failure into the client's address space.

## A dead backend answers differently per method, deliberately

This is the most user-visible decision in the project.

**`tools/call` on a known-but-down backend returns a successful result carrying
`isError: true`** -- not a protocol error. MCP's own contract is that tool-level failure is a
successful result with `isError`, *so the model can read it and adapt*. A model that gets
"Backend 'github' is not running: missing secret GITHUB_TOKEN" can tell the human GitHub is
not configured. A model that gets `-32603` gets a turn that died on a protocol error it has
no way to act on.

That matters more in a daemon than it would in a launcher: a backend can die at any point in
a week of uptime, and the client that notices is mid-turn.

**`prompts/get` and `resources/read` have no such escape hatch** -- there is no `isError` on
either result -- so they answer `-32603` and `-32002` respectively.

**An unknown backend prefix or an unparseable name is `-32602`**, whatever the method. That
is a caller mistake rather than a runtime condition, and telling the two apart is the whole
value of the distinction.

## Results are forwarded verbatim, with two named corrections

`content`, `annotations`, `structuredContent`, `_meta`, and whatever the next spec revision
adds all pass through untouched. A proxy that reshaped them would be lossy against every
revision it had not been taught -- which is the same reason the daemon parses no models.

The two exceptions both live in `_public_contents`, on `resources/read` alone: the item's
`uri` is re-addressed into the space the client actually asked in, and a `_meta.ui` on an
item declaring a UI profile is cleaned. See that method and `router.md` for why each is a
correction rather than a reshaping.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any

from mcp_gateway import errors, naming, ui_apps
from mcp_gateway.backend import Backend, BackendStatus
from mcp_gateway.catalogue import Catalogue
from mcp_gateway.mcp_stdio import MCPProtocolError

logger = logging.getLogger(__name__)


async def _awake(backend: Backend, catalogue: Catalogue) -> bool:
    """Wake `backend` if it is asleep, and say whether it is usable now.

    Every forwarding path calls this before its `running` check, so a slept backend costs
    the first caller a spawn and nobody an error. A backend that is down for any other
    reason -- failed, disabled, cooling off in the restart backoff -- is untouched, and the
    `running` check after this one is what still refuses it.
    """
    if backend.wakeable:
        await catalogue.supervisor.wake(backend.name)
    return backend.running


def _unavailable(backend: Backend) -> str:
    """Why this backend cannot serve, in a sentence a model can relay to a human."""
    if backend.status is BackendStatus.RESTARTING:
        # A distinct sentence, because this one resolves itself: a caller that retries in a
        # moment will succeed, where "missing secret GITHUB_TOKEN" needs a human.
        return (
            f"Backend {backend.name!r} is restarting as part of a configuration reload. "
            f"Retry in a moment."
        )
    reason = backend.error or f"status is {backend.status.value}"
    return (
        f"Backend {backend.name!r} is not running: {reason}. "
        f"Call gateway__backend_health for details."
    )


class Router:
    """Resolves a public name to a backend and forwards the call."""

    def __init__(self, catalogue: Catalogue) -> None:
        self.catalogue = catalogue

    def _unknown(self, kind: str, name: str) -> errors.GatewayError:
        known = [b.name for b in self.catalogue.supervisor.all]
        return errors.InvalidParams(
            f"unknown {kind} {name!r}", {kind: name, "knownBackends": known}
        )

    async def call_tool(self, session: Any, public_name: str, arguments: dict) -> dict[str, Any]:
        found = await self.catalogue.find_tool(public_name)
        if found is None:
            raise self._unknown("tool", public_name)
        backend, tool = found
        if not await _awake(backend, self.catalogue):
            # A successful result with `isError`, not an exception. See the module docstring.
            return {"content": [{"type": "text", "text": _unavailable(backend)}], "isError": True}

        backend.touch()
        # `serving` is what lets a `roots/list` or `elicitation/create` raised *by this call*
        # find its way back to this client. See `Backend.origin_session`.
        with backend.serving(session):
            try:
                return await backend.client.call_tool(tool, arguments)
            except MCPProtocolError as exc:
                exc.backend = backend.name
                raise

    async def get_prompt(
        self, session: Any, public_name: str, arguments: dict | None
    ) -> dict[str, Any]:
        found = self.catalogue.find_prompt(public_name)
        if found is None:
            raise self._unknown("prompt", public_name)
        backend, prompt = found
        if not await _awake(backend, self.catalogue):
            # No `isError` on a prompt result, so this has to be a protocol error.
            raise errors.GatewayError(
                _unavailable(backend),
                data={"backend": backend.name, "gatewayStatus": backend.status.value},
            )

        backend.touch()
        with backend.serving(session):
            try:
                return await backend.client.get_prompt(prompt, arguments)
            except MCPProtocolError as exc:
                exc.backend = backend.name
                raise

    async def complete(
        self,
        session: Any,
        ref: dict[str, Any],
        argument: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Argument completion, routed by what the `ref` names.

        The `ref` is the routing key and the only thing rewritten: a `ref/prompt` carries a
        namespaced name and a `ref/resource` a `mcpgw://` URI, both of which are resolved
        the same way `prompts/get` and `resources/read` resolve theirs. What comes back is
        forwarded untouched -- `values` are *argument values* in the backend's own
        vocabulary, an animal id or a country name, never names or URIs in the gateway's
        address space. Rewriting one would corrupt the exact string the client is about to
        send back as an argument.
        """
        kind = ref.get("type")
        if kind == "ref/prompt":
            name = ref.get("name")
            if not isinstance(name, str) or not name:
                raise errors.InvalidParams("a ref/prompt needs a 'name'", {"ref": ref})
            found = self.catalogue.find_prompt(name)
            if found is None:
                raise self._unknown("prompt", name)
            backend, local = found
            backend_ref = dict(ref, name=local)
        elif kind == "ref/resource":
            uri = ref.get("uri")
            if not isinstance(uri, str) or not uri:
                raise errors.InvalidParams("a ref/resource needs a 'uri'", {"ref": ref})
            found = self.catalogue.find_resource(uri)
            if found is None:
                raise errors.ResourceNotFound(
                    f"no resource at {uri!r}",
                    {"uri": uri, "knownBackends": [b.name for b in self.catalogue.supervisor.all]},
                )
            backend, local = found
            backend_ref = dict(ref, uri=local)
        else:
            # A revision naming a ref type we do not know is not something to guess at: there
            # is no way to tell which backend it means, so there is nowhere to send it.
            raise errors.InvalidParams(f"unknown completion ref type {kind!r}", {"ref": ref})

        if not await _awake(backend, self.catalogue):
            # No `isError` on a completion result, so this has to be a protocol error -- the
            # `prompts/get` rule.
            raise errors.GatewayError(
                _unavailable(backend),
                data={"backend": backend.name, "gatewayStatus": backend.status.value},
            )

        if not backend.client.supports("completions"):
            # Nothing reached the backend, so nothing is stamped on it. An empty list is what
            # a completions-capable server returns for an argument it cannot suggest for, and
            # it is the only answer that does not misrepresent somebody: see `protocol.py`.
            logger.debug("backend %r declares no completions; answering empty", backend.name)
            return {"completion": {"values": [], "total": 0, "hasMore": False}}

        if context is not None and backend.client.protocol_version == "2024-11-05":
            # `context` postdates that revision. Sending it anyway is a member the server
            # never agreed to read: ignored by a lenient one, refused by a strict one, and
            # the request is perfectly well-formed without it -- a cascade simply degrades
            # into an unfiltered list, which is better than a hint that fails the call.
            logger.debug(
                "backend %r negotiated 2024-11-05; dropping completion context", backend.name
            )
            context = None

        backend.touch()
        with backend.serving(session):
            try:
                return await backend.client.complete(backend_ref, argument, context)
            except MCPProtocolError as exc:
                exc.backend = backend.name
                raise

    async def read_resource(self, session: Any, public_uri: str) -> dict[str, Any]:
        found = self.catalogue.find_resource(public_uri)
        if found is None:
            raise errors.ResourceNotFound(
                f"no resource at {public_uri!r}",
                {
                    "uri": public_uri,
                    "knownBackends": [b.name for b in self.catalogue.supervisor.all],
                },
            )
        backend, uri = found
        if not await _awake(backend, self.catalogue):
            # MCP's dedicated code: the URI names something not currently readable.
            raise errors.ResourceNotFound(_unavailable(backend), {"backend": backend.name})

        backend.touch()
        with backend.serving(session):
            try:
                result = await backend.client.read_resource(uri)
            except MCPProtocolError as exc:
                exc.backend = backend.name
                raise
        return self._public_contents(backend.name, result)

    @staticmethod
    def _public_contents(server: str, result: dict[str, Any]) -> dict[str, Any]:
        """Re-address each content item, and clean the one `_meta` a host turns into policy.

        ## The address

        The one place this module reshapes a result, and it is a correction rather than an
        exception: a backend answers a read by naming *its own* URI, which is an address the
        client never used and cannot use. Every other listing the gateway publishes is
        rewritten, `notifications/resources/updated` is rewritten, and a content item that
        was not left a client unable to match the answer to the question -- harmless while
        nobody matched them, and not harmless once a panel is addressed this way.

        ## The `_meta.ui`

        Only on an item declaring a UI profile, and only its `ui` member. Narrow because the
        verbatim rule is a real one, and taken because SEP-1865 says the content-item
        `_meta.ui` **overrides** the listing-level one -- so cleaning only the listing would
        be cleaning the value that loses, and a hostile `csp` would reach a host by the one
        path that wins. Verbatim forwarding is a rule about fields we have not been taught;
        `csp` is one we have.

        Rebuilt rather than mutated: `read_resource` may be answering from a backend object
        a caller still holds.
        """
        contents = result.get("contents")
        if not isinstance(contents, list):
            return result
        rewritten = []
        for item in contents:
            if not isinstance(item, dict):
                rewritten.append(item)
                continue
            uri = item.get("uri")
            if isinstance(uri, str) and uri:
                item = {**item, "uri": naming.encode_resource_uri(server, uri)}
            if ui_apps.profile_of(item.get("mimeType")) is not None:
                cleaned = ui_apps.sanitize_resource_meta(item.get("_meta"))
                if cleaned is not None:
                    item = {**item, "_meta": cleaned}
            rewritten.append(item)
        return {**result, "contents": rewritten}

    async def subscribe_resource(self, session: Any, public_uri: str, *, on: bool) -> None:
        """Forward one half of a subscription. `on` picks subscribe or unsubscribe.

        The refusal a backend without `resources.subscribe` would give is anticipated rather
        than relayed, because `-32601` from down there names a method the *client* did call
        and the gateway does implement -- an answer that reads as a gateway bug. `-32002`
        says the true thing: this URI is not one you can watch.

        Unsubscribing is best-effort in one direction only. The gateway forgets the
        subscription whatever the backend says, because a client that asked to stop must be
        able to stop; a backend that then keeps sending is filtered out on the way up, since
        nothing is subscribed to that URI any more.
        """
        found = self.catalogue.find_resource(public_uri)
        if found is None:
            raise errors.ResourceNotFound(
                f"no resource at {public_uri!r}",
                {
                    "uri": public_uri,
                    "knownBackends": [b.name for b in self.catalogue.supervisor.all],
                },
            )
        backend, uri = found
        if not await _awake(backend, self.catalogue):
            raise errors.ResourceNotFound(_unavailable(backend), {"backend": backend.name})
        if not backend.supports_option("resources", "subscribe"):
            raise errors.ResourceNotFound(
                f"backend {backend.name!r} does not support resource subscriptions",
                {"uri": public_uri, "backend": backend.name},
            )

        backend.touch()
        # `serving` is skipped for a replay, which has no session behind it. Registering
        # `None` as one would make `origin_session` see two callers and answer "unclear",
        # so a real client's elicitation racing a restart would be dropped.
        scope = backend.serving(session) if session is not None else nullcontext()
        with scope:
            try:
                if on:
                    await backend.client.subscribe_resource(uri)
                else:
                    await backend.client.unsubscribe_resource(uri)
            except MCPProtocolError as exc:
                exc.backend = backend.name
                raise
