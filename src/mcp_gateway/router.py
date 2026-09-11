"""Forward one call to the backend that owns it, and translate what comes back.

Three methods: `tools/call`, `prompts/get`, `resources/read`. Each resolves a namespaced
name to a backend, forwards the call unchanged, and maps a failure into the client's
address space.

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

## Results are forwarded verbatim

`content`, `annotations`, `structuredContent`, `_meta`, and whatever the next spec revision
adds all pass through untouched. A proxy that reshaped them would be lossy against every
revision it had not been taught -- which is the same reason the daemon parses no models.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp_gateway import errors
from mcp_gateway.backend import Backend, BackendStatus
from mcp_gateway.catalogue import Catalogue
from mcp_gateway.mcp_stdio import MCPProtocolError

logger = logging.getLogger(__name__)


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
        if not backend.running:
            # A successful result with `isError`, not an exception. See the module docstring.
            return {"content": [{"type": "text", "text": _unavailable(backend)}], "isError": True}

        backend.last_call_at = time.time()
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
        if not backend.running:
            # No `isError` on a prompt result, so this has to be a protocol error.
            raise errors.GatewayError(
                _unavailable(backend),
                data={"backend": backend.name, "gatewayStatus": backend.status.value},
            )

        backend.last_call_at = time.time()
        with backend.serving(session):
            try:
                return await backend.client.get_prompt(prompt, arguments)
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
        if not backend.running:
            # MCP's dedicated code: the URI names something not currently readable.
            raise errors.ResourceNotFound(_unavailable(backend), {"backend": backend.name})

        backend.last_call_at = time.time()
        with backend.serving(session):
            try:
                return await backend.client.read_resource(uri)
            except MCPProtocolError as exc:
                exc.backend = backend.name
                raise
