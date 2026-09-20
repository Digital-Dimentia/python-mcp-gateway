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

from mcp_gateway import naming, ui_apps
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
        """One backend's listing, from cache or the wire. Never raises.

        The cache is consulted before anything else, which is what lets a *sleeping* backend
        keep advertising. Its listing did not change by the process going away -- the same
        command with the same config publishes the same tools -- so tearing the process down
        and continuing to answer from what it last said is the whole trick that makes an
        idle teardown invisible to a client.

        The honest limit: a backend cannot tell us its listing changed while it was asleep,
        so a `tools/call` can arrive for a tool the woken process no longer has. That call
        fails as an unknown tool and the backend's own `list_changed` corrects the listing
        moments later, which is the same self-correcting path a live backend's change takes.
        """
        if backend.name in cache:
            return cache[backend.name]
        # No cache and asleep: this is a lazy backend nobody has used yet, and there is no
        # answer to give without asking it. Waking to list is the cost of `lazy`, paid once.
        if backend.asleep:
            await self.supervisor.wake(backend.name)
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
        """Every backend's listing, fetched concurrently. Sleeping ones included.

        `running` alone would have been right when a backend that is not running is a
        backend that is broken. A slept one is neither broken nor gone, and dropping it here
        would make every idle teardown show up as tools vanishing from a client's list --
        the exact symptom the feature exists to avoid.

        `supports` is not consulted for a sleeping backend either: it answers from the
        handshake, which a slept process no longer has. `_fetch` returns its cached listing
        without asking, and a lazy one with no cache is woken there and checked then. Only
        an enabled backend can be asleep -- a disabled one is `DISABLED` and never IDLE --
        so there is no second condition to write here.
        """
        backends = [
            b
            for b in self.supervisor.all
            if (b.running and b.supports(capability)) or b.asleep
        ]
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
            unresolved: list[str] = []
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
                # The panel reference, moved into the same address space as the name above.
                # `rewrite_tool_meta` rebuilds every dict it touches rather than mutating,
                # because `tool` here is the *cached* entry and the cache is the backend's
                # own answer -- see `ui_apps.md`, "Nothing here mutates its argument".
                new_meta, referenced = ui_apps.rewrite_tool_meta(
                    tool.get("_meta"), backend.name, tool=name
                )
                if new_meta is not None:
                    entry["_meta"] = new_meta
                if referenced is not None and not self._publishes(backend.name, referenced):
                    unresolved.append(referenced)
                published.append(entry)
            if skipped:
                logger.warning(
                    "backend %r: %d tool(s) dropped, name too long or illegal once namespaced: %s",
                    backend.name,
                    len(skipped),
                    ", ".join(skipped),
                )
            if unresolved:
                logger.debug(
                    "backend %r: %d tool(s) reference a ui:// resource it does not list: %s",
                    backend.name,
                    len(unresolved),
                    ", ".join(sorted(set(unresolved))),
                )
            backend.skipped_tools = skipped
            backend.unresolved_ui_templates = sorted(set(unresolved))
        return published

    def _publishes(self, server: str, uri: str) -> bool:
        """Whether `server`'s cached resource listing already names `uri`.

        **Advisory, and true when we do not know.** This reads the cache and never fetches:
        making `tools()` wake a sleeping backend to check a panel reference would couple two
        fan-outs for a question that cannot be answered authoritatively anyway --
        `resources/list` is paginated and cached, and a `ui://` template may legitimately
        appear only under `resources/templates/list`.

        So a miss is a log line and a health field, never a dropped reference. A panel that
        really is absent fails at `resources/read` with a clean `-32002`, which is a better
        answer than a tool that silently lost its interface. It also keeps the two caches
        independent: because this never changes the published bytes, invalidating a
        backend's resources does not have to invalidate its tools.
        """
        listings = (self._resources.get(server), self._templates.get(server))
        if all(items is None for items in listings):
            return True
        for items in listings:
            for item in items or ():
                if item.get("uri") == uri or item.get("uriTemplate") == uri:
                    return True
        return False

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
                cleaned = ui_apps.sanitize_resource_meta(resource.get("_meta"))
                if cleaned is not None:
                    entry["_meta"] = cleaned
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
                cleaned = ui_apps.sanitize_resource_meta(template.get("_meta"))
                if cleaned is not None:
                    entry["_meta"] = cleaned
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
