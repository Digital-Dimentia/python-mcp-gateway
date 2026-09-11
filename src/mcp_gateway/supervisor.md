# `supervisor.py`

The shared backend pool: start them all, keep them, stop them all.

One `Supervisor` per process, not per connection. Three clients attached to one daemon use
**one** copy of each backend — one `npx` cold start total, one live state that everybody
including the admin UI observes identically. That is the difference between a gateway and a
launcher.

## Eager, concurrent, before the socket binds

Every enabled backend starts at daemon start, all at once, each under its own
`startup_timeout`. Concurrently because twelve backends that each take two seconds should
cost two seconds, not twenty-four.

A backend that fails is **skipped and logged**, and the sweep continues — unless it is marked
`required`, which raises `RequiredBackendFailed` and becomes an exit 2. One expired token
must not cost the operator every other tool. See [`config.md`](config.md) for the
`required` decision and [`backend.md`](backend.md) for why a failure is a status rather than
an exception.

A `Backend` is constructed for **every** catalogue entry, disabled ones included, so
`gateway__list_backends` can say why each is not serving. An object that existed only while
healthy could not.

## The readiness event

`ready` is set when the first sweep finishes. Listing methods wait on it, briefly.

The reason is specific: an empty `tools/list` is indistinguishable to a model from "this
gateway has nothing", and a model caches that conclusion for the rest of the turn. A client
that attaches mid-sweep should get the whole catalogue, not a truthful-looking partial one.

The wait is bounded by `READY_CEILING_SECONDS`. Past it, we answer with whatever is up,
because a client blocked forever is worse than a client told less than the whole truth.

## Capabilities are a callable, not a value

`client_capabilities` is a function the supervisor calls at the moment it builds a backend.
The union changes as clients connect and disconnect, and a backend started later must be
told what is true *then* — see [`gateway.md`](gateway.md). `restart` recomputes it for the
same reason: a restart is the only moment a backend can be told something new, because MCP
has no renegotiation.

## `close_all` tolerates failure

`return_exceptions=True`, because one backend that will not die must not prevent the other
eleven from being reaped. The shutdown ladder in [`mcp_stdio.md`](mcp_stdio.md) already
escalates to SIGKILL, so anything raising past it is exceptional twice over.

## Reload

`plan_reload` computes the difference; `reload` applies it. The plan is pure and touches
nothing, which is what makes `dry_run` free rather than a second code path.

### The diff is on *resolved* spawn identity

`spawn_identity` hashes command, args, cwd, env_mode, both timeouts, `enabled`, and the
**resolved** environment — not the `${VAR}` templates.

That distinction is the headline feature. Hashing the templates would make rotating a token
in `gateway.env` look like no change at all, and reloading after a rotation is the main
reason an operator reloads.

A digest rather than the values, so nothing that might be logged, diffed, or returned over
`/admin` ever holds a credential. Only "changed" or "unchanged" is ever reported — and the
digest itself is never emitted either, because a digest of a low-entropy secret is a secret.

### An unchanged backend is left completely alone

Not restarted. In a daemon with agents mid-turn, reloading to add one server must not drop
in-flight work on the other eleven. That property is the entire reason for comparing
identities rather than simply restarting everything, which would be four lines of code.

An unchanged backend keeps its process, its uptime and its restart count — but its `spec` is
swapped for the new config's equivalent object. They are identical by definition; holding the
stale one would make `gateway__list_backends` report from a config that is no longer loaded.

### A changed backend is stopped and respawned

There is no in-place update: a running process cannot be handed a new environment.

`_drain` gives its in-flight calls `DRAIN_TIMEOUT_SECONDS` to finish first, then replaces it
regardless — a call that will never return must not be able to pin a reload open. A client
whose call is cut off gets the `restarting` message, which is deliberately a *different*
sentence from the other unavailable cases: this one resolves itself on a retry, where
"missing secret GITHUB_TOKEN" needs a human.
