"""The shared backend pool: start them all, keep them, stop them all.

One `Supervisor` per process, not per connection. Three clients attached to one daemon use
**one** copy of each backend -- one `npx` cold start total, one live state that everybody
including the admin UI observes identically. That is the difference between a gateway and a
launcher.

## Eager, concurrent, and before the socket binds

Every enabled backend is started at daemon start, all at once, each under its own
`startup_timeout`. Concurrently because twelve backends that each take two seconds to spawn
should cost two seconds, not twenty-four.

A backend that fails is **skipped and logged**, and the sweep continues -- unless it is
marked `required`, which is the opt-in for a deployment where silent degradation is
unacceptable. One expired token must not cost the operator every other tool.

## The readiness event

`ready` is set when the first sweep finishes. Listing methods wait on it, briefly, so a
client that attaches mid-sweep gets the whole catalogue rather than a truthful-looking
partial one -- an empty `tools/list` is indistinguishable to a model from "this gateway has
nothing", and it caches that conclusion.

The wait is bounded. Past the ceiling we answer with whatever is up, because a client
blocked forever is worse than a client told less than the whole truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp_gateway.backend import Backend, BackendStatus, resolve_env
from mcp_gateway.config import GatewayConfig, ServerSpec
from mcp_gateway.mcp_stdio import MCPClientCapabilities
from mcp_gateway.secrets import SecretStore

logger = logging.getLogger(__name__)

#: How long a listing waits for the first sweep before answering with what it has. Sits
#: above a typical `npx` cold start and below every client's own request timeout.
READY_CEILING_SECONDS = 25.0

#: How long a backend about to be replaced gets to finish its in-flight calls.
DRAIN_TIMEOUT_SECONDS = 5.0


@dataclass
class ReloadPlan:
    """What a reload did, or would do. Names only -- never a value, never a digest."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "removed": self.removed,
            "restarted": self.changed,
            "unchanged": self.unchanged,
            "failed": self.failed,
        }


class Supervisor:
    """Owns every `Backend`, running or not."""

    def __init__(
        self,
        config: GatewayConfig,
        store: SecretStore,
        *,
        client_capabilities: Callable[[], MCPClientCapabilities] | None = None,
        on_server_request: Any = None,
        on_notification: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        #: A callable, not a value: the capability union changes as clients come and go, and
        #: a backend started later must be told what is true *then*.
        self._client_capabilities = client_capabilities or MCPClientCapabilities
        self._on_server_request = on_server_request
        self._on_notification = on_notification
        self.ready = asyncio.Event()
        self._backends: dict[str, Backend] = {}

    # --- access -------------------------------------------------------------------------

    def __iter__(self):
        return iter(self._backends.values())

    def __len__(self) -> int:
        return len(self._backends)

    @property
    def all(self) -> list[Backend]:
        return list(self._backends.values())

    @property
    def running(self) -> list[Backend]:
        return [backend for backend in self._backends.values() if backend.running]

    def get(self, name: str | None) -> Backend | None:
        return self._backends.get(name) if name else None

    # --- lifecycle ----------------------------------------------------------------------

    def _build(self, name: str) -> Backend:
        return Backend(
            spec=self.config.servers[name],
            store=self.store,
            client_capabilities=self._client_capabilities(),
            on_server_request=self._make_server_request_handler(name),
            on_notification=self._make_notification_handler(name),
        )

    def _make_server_request_handler(self, name: str):
        if self._on_server_request is None:
            return None

        async def handler(method: str, params: dict) -> dict:
            return await self._on_server_request(name, method, params)

        return handler

    def _make_notification_handler(self, name: str):
        if self._on_notification is None:
            return None

        async def handler(method: str, params: dict) -> None:
            await self._on_notification(name, method, params)

        return handler

    async def start_all(self) -> None:
        """Build every backend, start the enabled ones concurrently, then signal readiness.

        A `Backend` is constructed for **every** catalogue entry, disabled ones included, so
        `gateway__list_backends` can say why each one is not serving. An object that existed
        only while healthy could not.
        """
        self._backends = {name: self._build(name) for name in self.config.servers}
        enabled = [b for b in self._backends.values() if b.spec.enabled]
        if enabled:
            logger.info("starting %d backend(s)", len(enabled))
            await asyncio.gather(*(self._start_one(b) for b in enabled))
        self.ready.set()
        live = [b.name for b in self.running]
        logger.info(
            "%d of %d backend(s) running%s",
            len(live),
            len(enabled),
            f": {', '.join(live)}" if live else "",
        )

    async def _start_one(self, backend: Backend) -> None:
        """Start one backend, converting a `required` failure into a startup refusal."""
        started = await backend.start()
        if not started and backend.spec.required:
            raise RequiredBackendFailed(backend.name, backend.error or "did not start")

    async def restart(self, name: str) -> bool:
        backend = self._backends.get(name)
        if backend is None:
            return False
        # Rebuild the capability block: clients may have come or gone since it last started,
        # and a restart is the only moment a backend can be told something new.
        backend.client_capabilities = self._client_capabilities()
        return await backend.restart()

    # --- reload -------------------------------------------------------------------------

    def spawn_identity(self, spec: ServerSpec, store: SecretStore) -> str:
        """A digest of everything that decides what this backend's process *is*.

        **Resolved, not templated.** Hashing the `${VAR}` templates would make rotating a
        token in `gateway.env` look like no change at all -- and reloading after a rotation
        is the headline reason this daemon exists. Resolving first is what makes the rotation
        register as a difference.

        A digest rather than the values, so nothing that might be logged, diffed, or returned
        over `/admin` ever holds a credential. Only "changed" or "unchanged" is ever reported;
        the digest itself is never emitted either, because a digest of a low-entropy secret is
        a secret.
        """
        resolved = resolve_env(spec, store)
        material = [
            spec.command,
            "\x00".join(spec.args),
            spec.cwd or "",
            spec.env_mode,
            f"{spec.timeout}",
            f"{spec.startup_timeout}",
            "1" if spec.enabled else "0",
            "\x00".join(f"{k}={v}" for k, v in sorted(resolved.values.items())),
        ]
        return hashlib.sha256("\x1f".join(material).encode("utf-8")).hexdigest()

    def plan_reload(self, config: GatewayConfig, store: SecretStore) -> ReloadPlan:
        """What `reload` would do. Pure -- it touches nothing."""
        old_names = set(self.config.servers)
        new_names = set(config.servers)

        plan = ReloadPlan()
        plan.added = sorted(new_names - old_names)
        plan.removed = sorted(old_names - new_names)
        for name in sorted(old_names & new_names):
            before = self.spawn_identity(self.config.servers[name], self.store)
            after = self.spawn_identity(config.servers[name], store)
            if before == after:
                plan.unchanged.append(name)
            else:
                plan.changed.append(name)
        return plan

    async def reload(self, config: GatewayConfig, store: SecretStore) -> ReloadPlan:
        """Apply a new config and credential store, disturbing as little as possible.

        **An unchanged backend is left completely alone -- not restarted.** In a daemon with
        agents mid-turn, reloading to add one server must not drop in-flight work on the
        other eleven. That property is the whole reason the diff compares resolved spawn
        identity rather than simply restarting everything.

        A backend that is *changed* cannot be updated in place: a running process cannot be
        handed a new environment. It is stopped and respawned.
        """
        plan = self.plan_reload(config, store)

        for name in plan.removed:
            backend = self._backends.pop(name, None)
            if backend is not None:
                await backend.stop()

        self.config = config
        self.store = store

        for name in plan.changed:
            old = self._backends.pop(name, None)
            if old is not None:
                old.status = BackendStatus.RESTARTING
                await self._drain(old)
                await old.stop()

        for name in plan.changed + plan.added:
            backend = self._build(name)
            self._backends[name] = backend
            if backend.spec.enabled and not await backend.start():
                plan.failed.append({"name": name, "error": backend.error or "did not start"})

        # Unchanged backends keep their `Backend` object, and therefore their process, their
        # uptime and their restart count. But their spec object is the *old* config's, so
        # swap in the new equivalent -- identical by definition, and holding one stale object
        # would make `gateway__list_backends` report from a config that is no longer loaded.
        for name in plan.unchanged:
            self._backends[name].spec = config.servers[name]
            self._backends[name].store = store

        return plan

    async def _drain(self, backend: Backend, timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
        """Give a backend's in-flight calls a moment to finish before it is replaced.

        Bounded, then it stops regardless: a call that will never return must not be able to
        pin a reload open. A client whose call is cut off gets the `restarting` error, which
        says what happened.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while backend._calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if backend._calls:
            logger.info(
                "backend %r still had %d call(s) in flight after %.0fs; replacing anyway",
                backend.name,
                sum(backend._calls.values()),
                timeout,
            )

    async def close_all(self) -> None:
        """Stop every backend, whatever state each is in.

        `return_exceptions=True` because one backend that will not die must not prevent the
        other eleven from being reaped -- the shutdown ladder in `mcp_stdio` already escalates
        to SIGKILL, so anything raising here is exceptional twice over.
        """
        await asyncio.gather(
            *(backend.stop() for backend in self._backends.values()), return_exceptions=True
        )

    async def wait_ready(self, ceiling: float = READY_CEILING_SECONDS) -> bool:
        """Wait for the first sweep. Returns whether it finished in time."""
        try:
            await asyncio.wait_for(self.ready.wait(), ceiling)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "backends still starting after %.0fs; answering with what is up", ceiling
            )
            return False


class RequiredBackendFailed(RuntimeError):
    """A backend marked `required: true` did not start.

    A `RuntimeError` rather than a `ValueError` for the same reason `UnauthenticatedBindError`
    is: nothing about the arguments is malformed, and `ValueError` maps to `-32602`, which
    would be a bizarre answer to a startup refusal that never reaches a client.
    """

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"required backend {name!r} did not start: {reason}")
        self.name = name
        self.reason = reason
