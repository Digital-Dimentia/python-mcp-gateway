# `notifications.py`

Relays what a backend says unprompted, to the clients that should hear it.

A backend speaks up in four ways that matter, and each gets a different answer. Getting this
table wrong is how a daemon becomes noisy or stale — and neither symptom points at its cause.

| Backend sends | Gateway does |
|---|---|
| `notifications/{tools,prompts,resources}/list_changed` | invalidate that backend's cache; emit one upward, **debounced** |
| `notifications/progress` | relay **to the originating connection only**, verbatim |
| `notifications/message` | log locally with the backend's name; do not re-emit |
| `notifications/resources/updated` | drop, with a debug line |
| anything else | drop, with a debug line |

## Why `list_changed` is debounced

A reload that restarts eight backends produces eight notifications within a few
milliseconds. A client that refetches on each does eight `tools/list` round trips to reach
the same answer — and each of *those* fans out across every backend, so the cost grows as
the square of the deployment.

Coalescing costs a quarter second of staleness and removes the burst entirely.

The debounce is per **kind**, not per backend. The client's reaction is a single
`tools/list` covering everything, so there is nothing to gain by telling it twice which
backend changed — and the notification carries no backend field anyway.

An already-scheduled emission is **left alone rather than restarted**. Restarting it is the
classic debounce bug: a steady trickle of changes postpones the notification forever, so the
busiest deployment is the one that never hears about anything.

## Why progress goes to one connection

The `progressToken` in a `notifications/progress` is the **client's own**, forwarded down
unchanged on the call that started the work.

Broadcasting it would hand every other client a token it never issued: noise at best, and a
collision at worst, since two clients can independently pick the same token. The originating connection is found through
`Backend.origin_session()` — the same mechanism the passthrough rule uses, and for the same
reason it cannot be a contextvar. See [`gateway.md`](gateway.md).

## Why `notifications/message` is not re-emitted

The gateway does not advertise `logging`, and MCP says a server must not use a capability the
client did not declare.

Nothing is lost: the content goes to the daemon's own log with the backend's name on it,
which is where an operator looks, and `/admin`'s log stream is how the UI will surface it.

## Why `resources/updated` is dropped

`subscribe: false` is advertised, so nobody has subscribed and nobody is expecting these.
Forwarding one would also require rewriting its URI into the `mcpgw://` space — which is the
work the follow-up issue covers, and doing it half-way would produce a notification naming a
URI no client could read.
