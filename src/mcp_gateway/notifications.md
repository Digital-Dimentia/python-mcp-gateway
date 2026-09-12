# `notifications.py`

Relays what a backend says unprompted, to the clients that should hear it.

A backend speaks up in four ways that matter, and each gets a different answer. Getting this
table wrong is how a daemon becomes noisy or stale — and neither symptom points at its cause.

| Backend sends | Gateway does |
|---|---|
| `notifications/{tools,prompts,resources}/list_changed` | invalidate that backend's cache; emit one upward, **debounced** |
| `notifications/progress` | relay **to the originating connection only**, verbatim |
| `notifications/message` | log locally with the backend's name; do not re-emit |
| `notifications/resources/updated` | rewrite the URI; relay **to the sessions subscribed to it** |
| anything else | drop, with a debug line |

## The gateway announces its own catalogue changes too

Not everything that changes a listing starts at a backend. Three things here do:

| Gateway does | Clients hear |
|---|---|
| a reload that adds, removes or restarts backends | one `list_changed` per kind, after the whole sweep |
| `admin.backend.restart` / `gateway__restart_backend` | one `list_changed` per kind |
| a backend's own `list_changed` | that kind, relayed |

The restart row was missing until it was noticed from the outside: the catalogue was
invalidated and nobody was told, so an operator who edited a backend and restarted it
watched an open UI go on listing what that backend used to publish, with no way to know it
was looking at the past. All three kinds, because a restarted backend may have changed any
of them, and the debounce below means saying three things costs no more than saying one.

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

## Why `resources/updated` goes to subscribers, not everyone

It is the `progress` argument reached from the other side. A backend saying
`file:///README.md` changed is answering a question *some* client asked, and the URI it names
is its own — meaningless to a client that only ever saw `mcpgw://docs/file%3A%2F%2F%2F…`. So
two things happen before it can be relayed: `naming.encode_resource_uri` rewrites the URI,
and the notification goes only to the sessions subscribed to that exact public URI.

Broadcasting would be wrong twice over. A client that never subscribed is entitled to
silence — that is what `subscribe` *means* — and a client watching a *different* backend's
identically-named resource would be woken by a change that did not happen to it. Two
filesystem backends both publishing `file:///README.md` is the same collision that made
`encode_resource_uri` necessary in the first place, arriving on the notification path.

## Why a subscription survives a restart

`Subscriptions` is keyed on the public URI, which outlives the process behind it. A restarted
backend knows nothing about what anyone subscribed to, and the client has no way to find that
out — its subscription would simply stop producing, which is indistinguishable from a
resource that stopped changing. So `gateway.resubscribe` replays them against the new
process, and drops any the new process refuses rather than leaving the registry claiming a
subscription that does not exist.

A backend a reload *removed* is the other case: its subscriptions are dropped, because there
is nothing left to replay them against.

## Where the state lives

`Subscriptions` keeps two indexes over one fact — sessions by URI, URIs by session — because
both directions are hot: an update needs the sessions, a closing connection needs the URIs.
Nothing outside the class touches either dict, which is the only way they stay in step.
