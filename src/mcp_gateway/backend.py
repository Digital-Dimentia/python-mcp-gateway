"""One backend's lifetime: spec + credentials -> environment -> subprocess -> handshake.

`config.py` decides what a launch would look like; this module resolves credentials into
it and performs it. `supervisor.py` owns the *set* of these.

## The curated environment is the product

A `Backend` builds its child's environment **from nothing** and passes it with
`env_replace=True`. It does not overlay the daemon's own environment, and that is the one
decision this project exists to make.

The daemon holds every credential on the machine. If it forwarded its own environment to
each child, then the Slack backend would receive `GITHUB_TOKEN` the moment the operator
exported it in the shell that started the daemon -- and the consolidation claim would be
false in the most embarrassing possible way. A daemon's environment is also whatever
launchd or a login shell handed it: `ANTHROPIC_API_KEY`, AWS credentials, `SSH_AUTH_SOCK`,
and it persists for weeks.

So the child gets exactly three layers, in order:

1. `BASE_ALLOWLIST` -- process-shaping variables, nothing secret, and a backend genuinely
   cannot run without most of them.
2. the server's own `env_passthrough` names, taken from `os.environ` when present. The one
   explicit door to a parent variable, named per server.
3. the server's `env` block, `${VAR}`-resolved from the credential store. This layer wins.

`env_mode: inherit` is the escape hatch, and it logs a WARNING naming the server every time
it is used, so a deployment cannot end up relying on it quietly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mcp_gateway.config import ENV_MODE_INHERIT, ServerSpec
from mcp_gateway.mcp_http import MCPHttpClient
from mcp_gateway.mcp_stdio import (
    MCPClient,
    MCPClientCapabilities,
    MCPProtocolError,
    MCPStdioClient,
)
from mcp_gateway.secrets import MissingSecret, SecretStore, expand_path, interpolate

logger = logging.getLogger(__name__)

#: The longest a backend is made to wait between one failed start and the next attempt.
#:
#: A ceiling rather than an ever-doubling delay, because the thing being protected against
#: is a caller in a loop, not a caller who is wrong forever. A minute is long enough that a
#: hot loop costs nothing measurable and short enough that an operator who fixed the problem
#: is not left staring at a backend that refuses to come up.
RESTART_BACKOFF_CAP_SECONDS = 60.0


def restart_backoff_seconds(consecutive_failures: int) -> float:
    """How long after a failed start the next attempt may be made.

    **Zero for the first retry, and deliberately.** A failed start is usually followed by a
    human fixing the thing that broke -- a missing token, a command not on PATH -- and then
    asking for a restart; making *that* attempt wait would be punishing the one caller who
    already knows what was wrong. Repetition is what is being damped, so the delay starts on
    the second attempt and doubles from there: 0, 1, 2, 4, 8, ... up to the cap.
    """
    if consecutive_failures < 2:
        return 0.0
    return min(2.0 ** (consecutive_failures - 2), RESTART_BACKOFF_CAP_SECONDS)

#: Variables every child gets under `curated`. Nothing here is a credential, and a backend
#: genuinely cannot run without most of them.
#:
#: The proxy and CA entries are allowlisted because a backend behind a corporate
#: TLS-intercepting proxy cannot work without them, and a proxy URL *can* embed
#: `user:pass`. That is an accepted, documented exception; the redaction filter covers the
#: logging side of it.
BASE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # POSIX process shaping.
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
        # Windows. CPython will not start without SYSTEMROOT.
        "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        # Corporate TLS and proxy configuration.
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
    }
)

#: Deliberately **not** in the allowlist, recorded here so the omission reads as a decision
#: rather than an oversight. Each of these changes *which code* a backend executes, which
#: is a supply-chain surface rather than a convenience. A backend that needs a particular
#: interpreter names an absolute `command`.
DELIBERATELY_EXCLUDED: frozenset[str] = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NODE_PATH", "NODE_OPTIONS"}
)


class BackendStatus(str, Enum):
    """What `gateway__list_backends` reports. A `str` enum so it serialises as itself."""

    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    #: Not running, and that is the plan. Distinct from STOPPED, which reads as "somebody
    #: turned this off", and from FAILED, which reads as "this is broken" -- an operator
    #: glancing at `gateway__list_backends` has to be able to tell the three apart, because
    #: only one of them is a reason to go and look at something.
    IDLE = "idle"


@dataclass
class ResolvedEnv:
    """A child environment, plus what could not be resolved to build it."""

    values: dict[str, str] = field(default_factory=dict)
    missing: list[MissingSecret] = field(default_factory=list)
    #: The `${VAR}` names that *were* resolved. Used by the reload diff, never logged.
    resolved_keys: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


def resolve_env(spec: ServerSpec, store: SecretStore, *, environ: dict[str, str] | None = None) -> ResolvedEnv:
    """Build the child environment for `spec`. Never raises on a missing secret.

    Missing references are collected rather than raised so the caller can decide -- skip
    this one server, or refuse to start -- with the full list in hand. See `config.md`.
    """
    source = os.environ if environ is None else environ
    values: dict[str, str] = {}

    if spec.env_mode == ENV_MODE_INHERIT:
        logger.warning(
            "backend %r runs with env_mode=inherit: it receives this process's entire "
            "environment, including credentials meant for other backends",
            spec.name,
        )
        values.update(source)
    else:
        for key in BASE_ALLOWLIST:
            if key in source:
                values[key] = source[key]

    for key in spec.env_passthrough:
        if key in source:
            values[key] = source[key]
        else:
            logger.debug("backend %r: env_passthrough names %s, which is unset here", spec.name, key)

    missing: list[MissingSecret] = []
    for key, template in spec.env.items():
        values[key] = interpolate(
            template, store, where=f"servers.{spec.name}.env.{key}", missing=missing
        )

    return ResolvedEnv(values=values, missing=missing, resolved_keys=tuple(sorted(spec.env)))


@dataclass
class ResolvedHeaders:
    """A `url` backend's request headers, plus what could not be resolved to build them."""

    values: dict[str, str] = field(default_factory=dict)
    missing: list[MissingSecret] = field(default_factory=list)
    #: Names of headers whose resolved value would break the request -- a line break that
    #: came out of the credential store, where `config.py` could not see it.
    malformed: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.malformed


def resolve_headers(spec: ServerSpec, store: SecretStore) -> ResolvedHeaders:
    """Build the request headers for a `url` backend. Never raises on a missing secret.

    The HTTP counterpart of `resolve_env`, and deliberately a much smaller one: there is no
    allowlist and no passthrough, because nothing about the daemon's own environment has
    any business in a request to somebody else's server. The `headers` block is the whole
    of what is sent.
    """
    values: dict[str, str] = {}
    missing: list[MissingSecret] = []
    malformed: list[str] = []
    for key, template in spec.headers.items():
        value = interpolate(
            template, store, where=f"servers.{spec.name}.headers.{key}", missing=missing
        )
        # Checked after resolution: config.py refuses a line break in the template, but a
        # value from gateway.env or a provider is only seen here. One would split the request.
        if any(ch in value for ch in "\r\n\x00"):
            malformed.append(key)
        values[key] = value
    return ResolvedHeaders(values=values, missing=missing, malformed=malformed)


def resolve_cwd(spec: ServerSpec, store: SecretStore) -> str | None:
    """The working directory for this backend, `${VAR}`- and `~`-expanded."""
    if spec.cwd is None:
        return None
    return expand_path(spec.cwd, store, where=f"servers.{spec.name}.cwd")


@dataclass
class Backend:
    """One configured backend, whether or not it is currently running.

    A `Backend` exists for every entry in the catalogue, including disabled and failed
    ones. That is deliberate: `gateway__list_backends` has to be able to say *why* a
    backend is not serving, and an object that only exists while healthy could not.
    """

    spec: ServerSpec
    store: SecretStore
    client_capabilities: MCPClientCapabilities = field(default=MCPClientCapabilities())
    on_server_request: Any = None
    on_notification: Any = None

    status: BackendStatus = BackendStatus.STOPPED
    error: str | None = None
    client: MCPClient | None = None
    started_at: float | None = None
    restart_count: int = 0
    consecutive_failures: int = 0
    #: Monotonic time before which another `start` is refused, or `None` for no floor. Set
    #: by `_fail` and cleared by a start that works; see `restart_backoff_seconds`.
    _retry_at: float | None = None
    last_call_at: float | None = None
    #: The same event on the monotonic clock, which is the only one an idle measurement may
    #: use: `last_call_at` is wall time because `admin.status` reports it to a human, and a
    #: clock that an NTP step can move backwards would have the idle sweeper tear down a
    #: backend that was busy a second ago. Set together by `touch`.
    last_call_monotonic: float | None = None
    skipped_tools: list[str] = field(default_factory=list)
    #: Sessions with a call in flight on this backend, with a count each (one session can
    #: have several). This is how a backend's own `roots/list` or `elicitation/create` finds
    #: the client to ask -- see `origin_session` and `gateway.md`.
    _calls: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        if not self.spec.enabled:
            self.status = BackendStatus.DISABLED

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def running(self) -> bool:
        return self.status is BackendStatus.RUNNING and self.client is not None

    @property
    def uptime_seconds(self) -> float | None:
        return None if self.started_at is None else time.monotonic() - self.started_at

    @property
    def retry_after_seconds(self) -> float:
        """Seconds until another start attempt is allowed. `0.0` when one is.

        Reported by `admin.status` and `gateway__backend_health` rather than kept private: a
        backend that is down *and not being retried yet* is a different situation from one
        that is simply down, and an operator watching a restart do nothing deserves to see
        which.
        """
        if self._retry_at is None:
            return 0.0
        return max(0.0, self._retry_at - time.monotonic())

    @property
    def cooling_off(self) -> bool:
        return self.retry_after_seconds > 0.0

    @property
    def protocol_version(self) -> str | None:
        return None if self.client is None else self.client.protocol_version

    @property
    def server_capabilities(self) -> dict[str, Any] | None:
        return None if self.client is None else self.client.server_capabilities

    def supports(self, capability: str) -> bool:
        return self.client is not None and self.client.supports(capability)

    def supports_option(self, capability: str, option: str) -> bool:
        return self.client is not None and self.client.supports_option(capability, option)

    @contextmanager
    def serving(self, session: Any):
        """Mark `session` as having a call in flight on this backend, for its duration."""
        self._calls[session] += 1
        try:
            yield
        finally:
            self._calls[session] -= 1
            if self._calls[session] <= 0:
                del self._calls[session]

    @property
    def serving_anyone(self) -> bool:
        """Whether any session has a call in flight here. The idle sweep asks before it
        tears anything down: a `tools/call` that has been waiting on a human for ten minutes
        is the longest-running thing this daemon does, and it is not idle."""
        return bool(self._calls)

    def origin_session(self) -> Any | None:
        """The one session this backend is currently serving, or `None` if that is unclear.

        **Explicitly tracked rather than read from a `contextvars.ContextVar`**, and the
        reason is worth writing down because the contextvar version looks correct and is not:
        a backend's server-to-client request is dispatched from `MCPStdioClient`'s *read
        loop* task, which was created in `start()` -- long before any call. A task copies its
        context at creation, so the read loop's context can never contain a value set by a
        later call, and every forwarded request would find nothing.

        `None` when no call is in flight (a spontaneous request: nobody to ask) and also when
        **two different sessions** have calls in flight at once. MCP provides no correlation
        between a server-to-client request and the call that provoked it, so with two clients
        mid-call the honest answer is that we cannot tell whose human to interrupt -- and
        guessing has a fifty percent chance of putting one client's question in front of
        another client's user.
        """
        sessions = list(self._calls)
        return sessions[0] if len(sessions) == 1 else None

    async def start(self) -> bool:
        """Resolve, spawn and handshake. Returns whether the backend is now serving.

        Never raises for an expected failure -- a missing secret, a command that is not
        installed, a server that does not answer `initialize`. The daemon must start with
        the backends it can and report the rest, so every one of those becomes
        `status=FAILED` with `error` set and a `False` return.
        """
        if not self.spec.enabled:
            self.status = BackendStatus.DISABLED
            return False

        if self._refused_for_backoff():
            return False

        self.status = BackendStatus.STARTING
        self.error = None

        client = self._http_client() if self.spec.url is not None else self._stdio_client()
        if isinstance(client, str):
            return self._fail(client)

        try:
            await asyncio.wait_for(self._handshake(client), timeout=self.spec.startup_timeout)
        except asyncio.TimeoutError:
            await self._stop_client(client)
            return self._fail(
                f"did not complete initialize within {self.spec.startup_timeout:g}s"
            )
        except (OSError, MCPProtocolError) as exc:
            await self._stop_client(client)
            return self._fail(str(exc))

        self.client = client
        self.status = BackendStatus.RUNNING
        self.started_at = time.monotonic()
        self.consecutive_failures = 0
        self._retry_at = None
        logger.info(
            "backend %r running (%s, MCP %s)",
            self.name,
            self.spec.url if self.spec.url is not None else f"pid {self.pid}",
            client.protocol_version,
        )
        return True

    def _stdio_client(self) -> MCPStdioClient | str:
        """The client for a backend that is a process, or why it cannot be built."""
        resolved = resolve_env(self.spec, self.store)
        if not resolved.complete:
            return "; ".join(str(problem) for problem in resolved.missing)

        try:
            cwd = resolve_cwd(self.spec, self.store)
        except ValueError as exc:
            return str(exc)

        return MCPStdioClient(
            command=[self.spec.command, *self.spec.args],
            request_timeout=self.spec.timeout,
            env=resolved.values,
            # The whole point. See the module docstring and the `env_replace` field.
            env_replace=self.spec.env_mode != ENV_MODE_INHERIT,
            on_server_request=self.on_server_request,
            on_notification=self.on_notification,
            client_capabilities=self.client_capabilities,
            cwd=cwd,
        )

    def _http_client(self) -> MCPHttpClient | str:
        """The client for a backend that is a URL, or why it cannot be built.

        Same shape as the process case on purpose: a missing `${TOKEN}` in `headers` fails
        the start with the same message a missing one in `env` does, so the UI and
        `--check` report it identically. See `mcp_http.md`.
        """
        resolved = resolve_headers(self.spec, self.store)
        if resolved.missing:
            return "; ".join(str(problem) for problem in resolved.missing)
        if resolved.malformed:
            return (
                f"header(s) {sorted(resolved.malformed)} resolved to a value containing a "
                f"line break, which would split the request"
            )
        return MCPHttpClient(
            url=self.spec.url,
            headers=resolved.values,
            request_timeout=self.spec.timeout,
            on_server_request=self.on_server_request,
            on_notification=self.on_notification,
            client_capabilities=self.client_capabilities,
        )

    async def _handshake(self, client: MCPClient) -> None:
        """Spawn and negotiate, as one unit under `startup_timeout`.

        Timing the two together rather than separately is what the operator actually cares
        about: a backend that spawns instantly and then never answers `initialize` is as
        unusable as one that never spawns, and two budgets would let it consume both.
        """
        await client.start()
        await client.initialize()

    async def _stop_client(self, client: MCPClient) -> None:
        try:
            await client.stop()
        except Exception as exc:  # pragma: no cover - teardown of an already-broken child
            logger.debug("backend %r: error stopping a failed client: %s", self.name, exc)

    def _refused_for_backoff(self) -> bool:
        """Whether the backoff floor has yet to pass, logging the refusal if so.

        Refused, not queued, and **nothing about the backend changes**: `status` and `error`
        are left saying why it is actually down rather than being overwritten with a
        complaint about timing, the failure count does not grow, and the floor does not move.
        A refusal is not an attempt.

        What this is for is a caller in a loop -- a model holding `gateway__restart_backend`,
        a script, a finger on the UI's button -- which would otherwise buy a process spawn
        and a whole `startup_timeout` per turn. See `restart_backoff_seconds`.
        """
        waiting = self.retry_after_seconds
        if waiting <= 0.0:
            return False
        logger.warning(
            "backend %r not restarted: %d consecutive failures, next attempt in %.1fs",
            self.name,
            self.consecutive_failures,
            waiting,
        )
        return True

    def _fail(self, reason: str) -> bool:
        self.status = BackendStatus.FAILED
        self.error = reason
        self.client = None
        self.started_at = None
        self.consecutive_failures += 1
        self._retry_at = time.monotonic() + restart_backoff_seconds(self.consecutive_failures)
        logger.error("backend %r not started: %s", self.name, reason)
        return False

    async def stop(self) -> None:
        """Stop the subprocess, with the lifted close-stdin -> SIGTERM -> SIGKILL ladder."""
        client, self.client = self.client, None
        self.started_at = None
        if client is not None:
            await self._stop_client(client)
        if self.status is not BackendStatus.DISABLED:
            self.status = BackendStatus.STOPPED

    def touch(self) -> None:
        """Record that this backend was just used, on both clocks. See `last_call_monotonic`."""
        self.last_call_at = time.time()
        self.last_call_monotonic = time.monotonic()

    async def sleep(self) -> None:
        """Stop the process, but say it was on purpose.

        Not `stop()` with a different label. `stop` is an operator or a shutdown acting on
        the backend; this is the daemon reclaiming a process nobody is using, and the two
        differ in what happens next: a slept backend is woken by the next call, a stopped
        one waits for someone to ask. The failure counters are left alone for the same
        reason -- going to sleep is not a failure, and letting it feed the restart backoff
        would make a quiet backend progressively slower to wake.
        """
        if self.status is not BackendStatus.RUNNING:
            return
        client, self.client = self.client, None
        self.started_at = None
        if client is not None:
            await self._stop_client(client)
        self.status = BackendStatus.IDLE

    @property
    def asleep(self) -> bool:
        """Whether a call would have to wake this backend first."""
        return self.status is BackendStatus.IDLE

    @property
    def wakeable(self) -> bool:
        """Whether waiting on `supervisor.wake` could get this backend running.

        `STARTING` counts, and that is the whole point of having this beside `asleep`. Six
        calls landing on one sleeping backend at once means the first flips it to `STARTING`
        and the other five must *wait* for that start rather than conclude it is unavailable
        -- which is exactly what checking `asleep` alone made them do.
        """
        return self.status in (BackendStatus.IDLE, BackendStatus.STARTING)

    def idle_for(self, now: float) -> float:
        """Seconds since this backend last did anything, by the clock `now` came from.

        Measured from the last call, or from the start if there has never been one -- a
        backend nobody has used since it came up is idle, not exempt.
        """
        # `is not None`, not `or`: `time.monotonic()` is allowed to be near zero -- on Linux
        # it counts from boot -- and a falsy check would silently measure from `started_at`
        # on a machine that had just come up.
        since = self.last_call_monotonic
        if since is None:
            since = self.started_at
        return 0.0 if since is None else max(0.0, now - since)

    async def restart(self) -> bool:
        # Checked before anything is torn down, rather than being left to `start`. A backend
        # that is cooling off is a backend that already failed, so `stop` would have nothing
        # to kill -- but it would still set `status` to STOPPED, which reads as "somebody
        # turned this off" rather than "this is broken and waiting to be retried".
        if self._refused_for_backoff():
            return False
        self.status = BackendStatus.RESTARTING
        await self.stop()
        self.restart_count += 1
        return await self.start()

    @property
    def pid(self) -> int | None:
        """The backend's process id, or `None` -- including always, for a `url` backend,
        whose process is somebody else's."""
        proc = getattr(self.client, "_proc", None)
        return None if proc is None else proc.pid

    async def ping(self, timeout: float = 2.0) -> float | None:
        """Round-trip a `ping`, in milliseconds. `None` if the backend is not answering.

        What makes `gateway__backend_health` a health check rather than a status dump: a
        subprocess can be alive and wedged, and only a round trip tells the two apart.
        """
        if self.client is None:
            return None
        started = time.monotonic()
        try:
            await asyncio.wait_for(self.client.request("ping"), timeout=timeout)
        except (MCPProtocolError, asyncio.TimeoutError, OSError):
            return None
        return (time.monotonic() - started) * 1000.0
