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

## Lazy spawn and idle teardown

Two opt-in keys, both per-backend and both settable in `defaults:`. See
[`config.md`](config.md) for the schema.

`lazy: true` means the backend is built at startup but not spawned. It is not skipped or
forgotten — it reports as `idle`, and the first listing or call wakes it.

`idle_ttl: <seconds>` means a running backend with nothing to do for that long is put to
sleep. Omitting it means never; a `0` is refused, because "tear it down the instant it goes
idle" is a thrash rather than a policy.

**The property the whole thing rests on: a listing does not change by the process going
away.** The same command with the same config publishes the same tools, so
[`catalogue.py`](catalogue.md) keeps answering from what the backend last said and a slept
backend goes on advertising. Without that, every teardown would show up to a client as tools
vanishing — the exact symptom the feature exists to avoid.

The honest limit is the other side of the same coin: a sleeping backend cannot tell us its
listing changed. So a `tools/call` can arrive for a tool the woken process no longer has.
That call fails as an unknown tool and the backend's own `list_changed` corrects the listing
moments later — the same self-correcting path a live backend's change takes, just entered
later.

### What is never slept

- A backend with a **live resource subscription**. A sleeping process cannot send
  `notifications/resources/updated`, and a client watching a resource cannot tell silence
  from nothing having changed. Reclaiming a process at the cost of quietly breaking a
  feature the client asked for is not a trade the daemon makes on its own. `Gateway`
  computes the exempt set; see [`notifications.md`](notifications.md).
- A backend **with a call in flight**. A `tools/call` waiting on a human is the
  longest-running thing this daemon does, and it is not idle.
- A backend with **no `idle_ttl`**, which is every backend by default.

### Sleeping is not failing

`sleep()` leaves `consecutive_failures` and the restart backoff alone. Feeding them would
make a quiet backend progressively slower to wake, which is the opposite of the point.
`IDLE` is its own status for the same reason: an operator reading
`gateway__list_backends` has to be able to tell "the daemon reclaimed this" from "somebody
turned it off" (`stopped`) and "this is broken" (`failed`), because only one of the three is
a reason to go and look at something.

### Waking is serialised per backend

Six calls landing on one sleeping backend must spawn one process, not six, and the five that
did not win have to *wait* rather than conclude the backend is unavailable. `wake` takes its
lock before it decides, and `Backend.wakeable` counts `STARTING` as well as `IDLE` — a
caller that checked `asleep` alone would see the winner's `STARTING` and give up on a start
that was milliseconds from finishing.

The idle clock is `time.monotonic`, kept beside the wall-clock `last_call_at` that
`admin.status` reports to a human. A wall clock an NTP step can move backwards would have
the sweeper tear down a backend that was busy a second ago.

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
