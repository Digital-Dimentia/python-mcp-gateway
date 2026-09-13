"""Relay what a backend says unprompted, to the clients that should hear it.

A backend speaks up in four ways that matter, and each gets a different answer. Getting this
table wrong is how a daemon becomes noisy or stale, and neither symptom points at its cause.

| Backend sends | Gateway does |
|---|---|
| `notifications/{tools,prompts,resources}/list_changed` | invalidate that backend's cache; emit one upward, **debounced** |
| `notifications/progress` | relay **to the originating connection only**, verbatim |
| `notifications/message` (logging) | log locally, **and** relay to each client whose level admits it |
| `notifications/resources/updated` | rewrite the URI and relay **to the sessions subscribed to it** |
| anything else | drop, with a debug line |

## Why `list_changed` is debounced

A reload that restarts eight backends produces eight notifications within a few
milliseconds. A client that refetches on each one does eight `tools/list` round trips to
reach the same answer, and each of those fans out across every backend. Coalescing them into
one costs a quarter of a second of staleness and saves a burst that grows as the square of
the deployment.

The debounce is per *kind*, not per backend: the client's reaction is a single
`tools/list` covering everything, so there is nothing to be gained by telling it twice which
backend changed -- and the notification carries no backend field anyway.

## Why progress goes to one connection

The `progressToken` in a `notifications/progress` is the **client's own**, forwarded down
unchanged on the call that started the work. Broadcasting it would hand every other client a
token it never issued: noise at best, and a token collision at worst, since two clients can
independently pick the same one.

## Why `notifications/message` is relayed, and how it is filtered

This was a drop for as long as the gateway did not advertise `logging` -- MCP says a server
must not use a capability the client did not declare, so re-emitting would have been a
violation. Advertising it changed the condition, not the rule.

It is still logged locally first: an operator reading the daemon's log should not have to be
a connected client to see a backend complain, and `/admin`'s log stream is how the UI gets
it. What the relay adds is the client's copy.

Two levels are in play and they are not the same one. What goes *down* to the backends is
the most verbose level any live session asked for, because one backend process serves every
client and MCP has no per-subscriber level on its wire. What goes *up* is filtered per
session against the level that session set. So a client asking for `debug` makes the daemon
noisier for itself alone.

A client that never called `logging/setLevel` is sent nothing. The spec leaves the default
to the server, and the alternative -- picking `info` for everyone -- would start a stream
every client that predates this feature never asked for.

The backend's name goes in `logger`, the field MCP already has for it, ahead of whatever the
backend put there (`talker/db.pool`). Three backends' logs in one stream are useless if you
cannot tell them apart, and flattening them would be lossy in a way nothing downstream could
undo.

Nobody is told to be quiet again when the last interested client leaves: the union simply
stops being pushed down, and a backend already at `debug` stays there. Its messages reach
our own level-filtered log and no client, which costs a little stderr and no correctness.

## Why `resources/updated` goes to subscribers, not everyone

It is the same argument as `progress`, reached from the other side. A backend saying
`file:///README.md` changed is answering a question *some* client asked, and the URI it
names is its own -- meaningless to a client that only ever saw `mcpgw://docs/file%3A///...`.
So two things have to happen before it can be relayed: the URI is rewritten into the
gateway's address space by `naming.encode_resource_uri`, and the notification goes only to
the sessions that subscribed to that exact public URI.

Broadcasting instead would be wrong twice over. A client that never subscribed is entitled
to assume it will not be told -- that is what `subscribe` *means* -- and a client that
subscribed to a different backend's identically-named resource would be woken by a change
that did not happen to it. Two filesystem backends both publishing `file:///README.md` is
the same collision that made `encode_resource_uri` necessary in the first place.

## Why a subscription survives a restart

`Subscriptions` is keyed on the public URI, which outlives the process behind it. A backend
that restarts comes back knowing nothing about what anyone had subscribed to, so the gateway
replays them -- see `gateway.resubscribe`. The alternative is a subscription that silently
stops working after a crash-and-respawn nobody watched, which is indistinguishable from a
resource that stopped changing.

A subscription to a backend that is *removed* by a reload is dropped instead. There is
nothing to replay it against, and the resource it names no longer exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from mcp_gateway import naming, protocol

logger = logging.getLogger(__name__)

#: How long `list_changed` notifications of one kind are coalesced. Long enough to swallow a
#: reload's burst, short enough that a human clicking "refresh" does not notice it.
DEBOUNCE_SECONDS = 0.25

#: MCP log levels to Python's, for the messages we log rather than relay.
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "alert": logging.CRITICAL,
    "emergency": logging.CRITICAL,
}

_LIST_CHANGED = {
    protocol.TOOLS_LIST_CHANGED: "tools",
    protocol.PROMPTS_LIST_CHANGED: "prompts",
    protocol.RESOURCES_LIST_CHANGED: "resources",
}

Broadcaster = Callable[[str, dict | None], Awaitable[Any]]


class Notifier:
    """Coalesces and routes backend notifications."""

    def __init__(self, broadcast: Broadcaster, *, debounce: float = DEBOUNCE_SECONDS) -> None:
        self._broadcast = broadcast
        self._debounce = debounce
        self._pending: dict[str, asyncio.Task[None]] = {}

    def kind_of(self, method: str) -> str | None:
        return _LIST_CHANGED.get(method)

    async def list_changed(self, method: str) -> None:
        """Schedule one upward `list_changed` for this kind, coalescing a burst.

        An already-scheduled emission is left alone rather than restarted. Restarting it
        would make a steady trickle of backend changes postpone the notification forever --
        the classic debounce failure, where the busiest case is the one that never fires.
        """
        if method in self._pending and not self._pending[method].done():
            return
        self._pending[method] = asyncio.create_task(self._emit_after_delay(method))

    async def _emit_after_delay(self, method: str) -> None:
        try:
            await asyncio.sleep(self._debounce)
            await self._broadcast(method, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - a broadcast that fails is not fatal
            logger.debug("failed to emit %s: %s", method, exc)
        finally:
            self._pending.pop(method, None)

    async def flush(self) -> None:
        """Emit anything pending now. For shutdown, and for tests that cannot wait."""
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()

    def log_backend_message(self, name: str, params: dict) -> None:
        """Put a backend's `notifications/message` in our log, not on the client's wire."""
        level = _LEVELS.get(str(params.get("level", "info")).lower(), logging.INFO)
        data = params.get("data")
        logger.log(level, "[%s] %s", name, data if isinstance(data, str) else repr(data))

    async def cancel_pending(self) -> None:
        await self.flush()


class Subscriptions:
    """Who is listening to which resource, in the gateway's own address space.

    Two indexes over one fact, because both directions are hot: a
    `notifications/resources/updated` needs the sessions for a URI, and a closing session
    needs its URIs. Keeping them in step is this class's whole job -- nothing outside it
    touches either dict.

    Public URIs throughout (`mcpgw://server/...`). A backend's own URI is never stored: it
    is derivable, it is ambiguous across backends, and storing the public form is what lets
    a replay after a restart work without re-deriving anything.
    """

    def __init__(self) -> None:
        self._by_uri: dict[str, set[Any]] = {}
        self._by_session: dict[Any, set[str]] = {}

    def add(self, session: Any, public_uri: str) -> bool:
        """Record one subscription. False if this session already had it.

        The caller uses the answer to skip a redundant `resources/subscribe` on the wire:
        MCP does not say what a duplicate does, and the honest reading of a set is that
        subscribing twice is subscribing once.
        """
        uris = self._by_session.setdefault(session, set())
        if public_uri in uris:
            return False
        uris.add(public_uri)
        self._by_uri.setdefault(public_uri, set()).add(session)
        return True

    def remove(self, session: Any, public_uri: str) -> bool:
        """Forget one subscription. False if this session did not have it."""
        uris = self._by_session.get(session)
        if uris is None or public_uri not in uris:
            return False
        uris.discard(public_uri)
        if not uris:
            self._by_session.pop(session, None)
        self._discard_uri(session, public_uri)
        return True

    def drop_session(self, session: Any) -> None:
        """Forget everything one session subscribed to. For a closing connection."""
        for public_uri in self._by_session.pop(session, set()):
            self._discard_uri(session, public_uri)

    def drop_backend(self, name: str) -> None:
        """Forget every subscription against one backend. For a backend a reload removed."""
        for public_uri in self.uris_for(name):
            self.drop_uri(public_uri)

    def _discard_uri(self, session: Any, public_uri: str) -> None:
        sessions = self._by_uri.get(public_uri)
        if sessions is None:
            return
        sessions.discard(session)
        if not sessions:
            self._by_uri.pop(public_uri, None)

    def sessions_for(self, public_uri: str) -> list[Any]:
        """Who to tell about a change to this URI. A list, so the caller can await freely
        without iterating a set that a closing session may mutate underneath it."""
        return list(self._by_uri.get(public_uri, ()))

    def uris_of(self, session: Any) -> frozenset[str]:
        """What one session is subscribed to, for the duplicate check on `subscribe`."""
        return frozenset(self._by_session.get(session, ()))

    def drop_uri(self, public_uri: str) -> None:
        """Forget one URI for everyone. For a subscription that could not be replayed."""
        for session in self._by_uri.pop(public_uri, set()):
            uris = self._by_session.get(session)
            if uris is None:
                continue
            uris.discard(public_uri)
            if not uris:
                self._by_session.pop(session, None)

    def watched_uris(self) -> frozenset[str]:
        """Every URI somebody is subscribed to. The idle sweep reads this to know which
        backends it must leave running -- see `gateway._subscribed_backends`."""
        return frozenset(self._by_uri)

    def uris_for(self, name: str) -> list[str]:
        """Every public URI subscribed against one backend, for a replay after a restart."""
        prefix = f"{naming.RESOURCE_SCHEME}://{name}/"
        return [uri for uri in self._by_uri if uri.startswith(prefix)]

    def __len__(self) -> int:
        return sum(len(sessions) for sessions in self._by_uri.values())
