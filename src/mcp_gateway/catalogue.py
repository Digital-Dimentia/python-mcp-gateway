"""Aggregate every backend's tools, prompts and resources into one namespaced listing.

Process-wide and shared by every connection: the listing is a property of the backend pool,
not of who is asking.

## Cached per backend, invalidated by `list_changed`

A `tools/list` that fanned out to twelve subprocesses on every call would make the cheapest
method in the protocol the most expensive, and clients call it often. So each backend's
listing is cached, and the cache is dropped when that backend emits its `list_changed`
notification or is restarted -- which is exactly what the notification is *for*.

The cache is per backend rather than global, so one backend changing its tools does not
force a refetch from the other eleven.

## A backend that fails to list is skipped, not fatal

A listing is a fan-out, and something in a fan-out will eventually be broken. One backend
timing out must not empty the catalogue: the model would see a gateway with nothing in it
and conclude the tools do not exist. The failure is logged and recorded on the backend,
where `gateway__backend_health` will report it.

## Names a client would reject are dropped

A composed name over 64 characters, or carrying a character real clients refuse, is left out
and recorded in `skipped_tools`. Publishing it would hand the model a tool whose own client
rejects the call -- a protocol error mid-turn that the model cannot act on, which is strictly
worse than the tool not being there. The health entry is what keeps the drop from being
silent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp_gateway import naming
from mcp_gateway.backend import Backend
from mcp_gateway.mcp_stdio import MCPProtocolError
from mcp_gateway.supervisor import Supervisor

logger = logging.getLogger(__name__)


class Catalogue:
    """The merged view of every running backend's primitives."""

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        self._tools: dict[str, list[dict[str, Any]]] = {}
        self._prompts: dict[str, list[dict[str, Any]]] = {}
        self._resources: dict[str, list[dict[str, Any]]] = {}
        self._templates: dict[str, list[dict[str, Any]]] = {}

    def invalidate(self, name: str, *, kind: str | None = None) -> None:
        """Drop one backend's cached listing. `kind` narrows it to one primitive."""
        caches = {
            "tools": self._tools,
            "prompts": self._prompts,
            "resources": self._resources,
            "templates": self._templates,
        }
        for key, cache in caches.items():
            if kind is None or key == kind or (kind == "resources" and key == "templates"):
                cache.pop(name, None)

    def invalidate_all(self) -> None:
        for cache in (self._tools, self._prompts, self._resources, self._templates):
            cache.clear()

    # --- fetching -----------------------------------------------------------------------

    async def _fetch(
        self, backend: Backend, cache: dict[str, list], capability: str, fetch
    ) -> list[dict[str, Any]]:
        """One backend's listing, from cache or the wire. Never raises."""
        if backend.name in cache:
            return cache[backend.name]
        if not backend.running or not backend.supports(capability):
            return []
        try:
            items = await fetch(backend)
        except (MCPProtocolError, asyncio.TimeoutError, OSError) as exc:
            # Logged and recorded rather than raised: see the module docstring.
            logger.warning("backend %r failed to list %s: %s", backend.name, capability, exc)
            backend.error = f"listing {capability} failed: {exc}"
            return []
        cache[backend.name] = items
        return items

    async def _gather(self, cache: dict[str, list], capability: str, fetch) -> list[tuple[Backend, list]]:
        """Every running backend's listing, fetched concurrently."""
        backends = [b for b in self.supervisor.running if b.supports(capability)]
        results = await asyncio.gather(
            *(self._fetch(b, cache, capability, fetch) for b in backends)
        )
        return list(zip(backends, results))

    # --- tools --------------------------------------------------------------------------

    async def tools(self) -> list[dict[str, Any]]:
        """Every backend's tools, namespaced. Order follows `servers.yaml`."""
        published: list[dict[str, Any]] = []
        for backend, items in await self._gather(self._tools, "tools", lambda b: b.client.list_tools()):
            skipped: list[str] = []
            for tool in items:
                name = tool.get("name")
                if not isinstance(name, str) or not name:
                    continue
                public = naming.compose(backend.name, name)
                if not naming.is_publishable(public):
                    skipped.append(name)
                    continue
                # Copied, not mutated: the cached entry is the backend's own answer, and a
                # later reader (health, /admin) must see it as the backend gave it.
                entry = dict(tool)
                entry["name"] = public
                published.append(entry)
            if skipped:
                logger.warning(
                    "backend %r: %d tool(s) dropped, name too long or illegal once namespaced: %s",
                    backend.name,
                    len(skipped),
                    ", ".join(skipped),
                )
            backend.skipped_tools = skipped
        return published

    async def find_tool(self, public_name: str) -> tuple[Backend, str] | None:
        """Resolve a namespaced tool name to its backend.

        A pure string split, then a lookup of the *backend* -- never of the tool. The tool
        name is handed to the backend as it gave it; whether it still exists is the
        backend's answer to give, not a cache's to guess. That is what keeps this correct
        after a `list_changed` nobody has refetched yet.
        """
        try:
            server, tool = naming.split(public_name)
        except naming.NamingError:
            return None
        backend = self.supervisor.get(server)
        return None if backend is None else (backend, tool)

    # --- prompts ------------------------------------------------------------------------

    async def prompts(self) -> list[dict[str, Any]]:
        published: list[dict[str, Any]] = []
        for backend, items in await self._gather(
            self._prompts, "prompts", lambda b: b.client.list_prompts()
        ):
            for prompt in items:
                name = prompt.get("name")
                if not isinstance(name, str) or not name:
                    continue
                entry = dict(prompt)
                entry["name"] = naming.compose(backend.name, name)
                published.append(entry)
        return published

    def find_prompt(self, public_name: str) -> tuple[Backend, str] | None:
        try:
            server, prompt = naming.split(public_name)
        except naming.NamingError:
            return None
        backend = self.supervisor.get(server)
        return None if backend is None else (backend, prompt)

    # --- resources ----------------------------------------------------------------------

    async def resources(self) -> list[dict[str, Any]]:
        published: list[dict[str, Any]] = []
        for backend, items in await self._gather(
            self._resources, "resources", lambda b: b.client.list_resources()
        ):
            for resource in items:
                uri = resource.get("uri")
                if not isinstance(uri, str) or not uri:
                    continue
                entry = dict(resource)
                entry["uri"] = naming.encode_resource_uri(backend.name, uri)
                if isinstance(resource.get("name"), str):
                    entry["name"] = naming.compose_display_name(backend.name, resource["name"])
                published.append(entry)
        return published

    async def resource_templates(self) -> list[dict[str, Any]]:
        published: list[dict[str, Any]] = []
        for backend, items in await self._gather(
            self._templates, "resources", lambda b: b.client.list_resource_templates()
        ):
            for template in items:
                uri = template.get("uriTemplate")
                if not isinstance(uri, str) or not uri:
                    continue
                entry = dict(template)
                # `encode_resource_uri` leaves RFC 6570 braces unescaped, so the expression
                # survives and the client can still expand it.
                entry["uriTemplate"] = naming.encode_resource_uri(backend.name, uri)
                if isinstance(template.get("name"), str):
                    entry["name"] = naming.compose_display_name(backend.name, template["name"])
                published.append(entry)
        return published

    def find_resource(self, public_uri: str) -> tuple[Backend, str] | None:
        try:
            server, uri = naming.decode_resource_uri(public_uri)
        except naming.NamingError:
            return None
        backend = self.supervisor.get(server)
        return None if backend is None else (backend, uri)

    # --- counts, for the health tools ---------------------------------------------------

    def counts(self, name: str) -> dict[str, int | None]:
        """Cached counts for one backend. `None` where nothing has been listed yet.

        `None` rather than `0` because they are different facts, and a health report that
        conflated them would say "this backend has no tools" about one nobody has asked.
        """
        return {
            "tool_count": len(self._tools[name]) if name in self._tools else None,
            "prompt_count": len(self._prompts[name]) if name in self._prompts else None,
            "resource_count": len(self._resources[name]) if name in self._resources else None,
        }
