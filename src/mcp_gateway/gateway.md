# `gateway.py`

The composition root. Owns everything shared; builds a `Session` per connection.

Nothing else constructs a supervisor, a catalogue, or a store. That concentration is what
lets `cli.py` stay a command line, and what lets a test drive the whole daemon in-process
with two clients attached and no subprocess of its own.

## Shared versus per-connection

| Per connection | Process-wide |
|---|---|
| negotiated protocol version | the backend pool |
| the client's declared capabilities | the credential store |
| that client's in-flight requests | the parsed config |

A daemon with three clients attached runs **one** copy of each backend, and all three see
the same live state. That is the difference between a gateway and a launcher: one `npx` cold
start total, and a UI that observes exactly what the agent is using.

## Backends come up before the socket binds

`start()` starts backends, *then* binds. In that order deliberately:

- The first client to attach should find a warm pool.
- A daemon with no clients still has to answer `/admin` about what is running — the UI needs
  that without an agent attached.
- Binding first would let a client connect into a half-populated catalogue and cache an
  answer that was true for a second.

`initialize` itself never waits on this; see [`session.md`](session.md).

## Reverse passthrough and the origin-connection rule

A backend may ask `roots/list`, `sampling/createMessage`, or `elicitation/create`. Those must
reach *a* client, and with several attached there is no general answer to "which one".

**The rule: the connection whose call is currently in flight on that backend.** `router`
wraps every forwarded call in `Backend.serving(session)`, and `Backend.origin_session()`
reports it. That covers every real case, because a backend asks these questions *in response
to* a call it is servicing.

The origin is **explicitly tracked, not carried in a `contextvars.ContextVar`** — and this
is worth writing down, because the contextvar version looks obviously correct and is not. A
backend's server-to-client request is dispatched from `MCPStdioClient`'s **read loop** task,
created in `start()`, long before any call exists. A task copies its context at creation, so
the read loop's context can never contain a value set by a later call: every forwarded
request finds nothing. The tests caught this; nothing else would have.

Two cases are refused with `-32601`, which `mcp_stdio.py` already produces from
`UnsupportedServerRequest`:

- **A spontaneous request** — no call in flight. There is nobody to ask.
- **Two different clients mid-call on one backend.** MCP provides no correlation between a
  server-to-client request and the call that provoked it, so guessing has a fifty percent
  chance of putting one client's question in front of another client's user.

Two concurrent calls from the *same* client are not ambiguous, and are not refused.

A client that never declared the capability is also not asked, even if it is unambiguously
the origin: a capability block is a promise in both directions.

## What the gateway declares downward

The **union** of what the currently-attached clients declared, recomputed on connect and
disconnect.

A backend cannot renegotiate after `initialize`, so the union is the only honest answer at
the moment a backend starts. It also means a capability newly declared by an arriving client
only reaches backends started *after* it — which is exactly why the origin rule above has to
be able to refuse, rather than assuming any accepted request can be placed.

A gateway with no clients attached declares nothing, which is correct: there is nobody to
ask.

## Shutdown

`cli` awaits `shutdown_requested`, then calls `stop()` in a `finally`. A `finally` rather
than a context manager because the one thing that must happen on every exit path is that the
backend subprocesses are reaped — and a daemon exits by signal far more often than by
falling off the end of a block.

## Not yet wired

`reload` is phase 8; everything else is in place.
