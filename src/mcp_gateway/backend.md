# `backend.py`

One backend's lifetime: spec + credentials → environment → subprocess → handshake.

`config.py` decides what a launch *would* look like; this module resolves credentials into
it and performs it. `supervisor.py` owns the set of these.

## The curated environment is the product

A `Backend` builds its child's environment **from nothing** and passes it with
`env_replace=True`. It does not overlay the daemon's own environment.

The template's `MCPStdioClient` does overlay, and defends it well: a server command almost
always needs `PATH` and `HOME`, and it is not a sandbox boundary either way, because
whoever supplies `env` also supplies `command`.

**That last clause is exactly what differs here, and it is the whole product.** The
operator writes one file holding every credential for every backend, and the claim is that
each backend receives only its own. Inherit the parent environment and the Slack backend
gets `GITHUB_TOKEN` the moment the operator exported it in the shell that started the
daemon — and the consolidation claim is false in the most embarrassing possible way.

A *daemon* makes this sharper than it would be for a launcher. Its environment is whatever
launchd or a login shell handed it — routinely `ANTHROPIC_API_KEY`, AWS credentials,
`SSH_AUTH_SOCK` — and it persists for weeks.

## The three layers

1. **`BASE_ALLOWLIST`** — process-shaping variables, nothing secret, and a backend
   genuinely cannot run without most of them: `PATH HOME USER LOGNAME SHELL TMPDIR TMP
   TEMP LANG LC_ALL LC_CTYPE TZ TERM`, the Windows set (CPython will not start without
   `SYSTEMROOT`), and the TLS/proxy set.
2. **`env_passthrough`** — the names that server lists, taken from `os.environ` when
   present. The one deliberate door to a parent variable, opened per server, per name.
3. **the server's own `env`**, `${VAR}`-resolved from the credential store. This layer
   wins.

### What is deliberately excluded

`PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, `NODE_PATH`, `NODE_OPTIONS` are named in
`DELIBERATELY_EXCLUDED` so the omission reads as a decision rather than an oversight. Each
changes *which code* a backend executes, which is a supply-chain surface rather than a
convenience. A backend that needs a particular interpreter names an absolute `command`.

### The one accepted exception

Proxy URLs are allowlisted and a proxy URL can embed `user:pass`. A backend behind a
corporate TLS-intercepting proxy cannot work without them, so they stay; the redaction
filter covers the logging side.

### `env_mode: inherit`

The escape hatch, per server. It logs a WARNING naming the server **every time it is
used**, and `gateway__list_backends` reports the mode, so a deployment cannot come to rely
on it quietly.

## Failure is a status, not an exception

`Backend.start()` never raises for an expected failure — a missing secret, a command that
is not installed, a server that does not answer `initialize`. Each becomes
`status=FAILED`, `error` set, and a `False` return.

That is what lets the daemon start with the backends it can and report the rest, per the
decision in [`config.md`](config.md). A `Backend` object exists for **every** catalogue
entry, including disabled and failed ones, precisely so `gateway__list_backends` can say
*why* a backend is not serving; an object that only existed while healthy could not.

## A failed start puts a floor under the next one

`consecutive_failures` was counted long before anything read it, which left nothing between
a caller and a spawn-per-request loop. The caller is not hypothetical: a model holding
`gateway__restart_backend` is exactly the sort of thing that retries a failing tool as fast
as it can, and each turn buys a process spawn plus a whole `startup_timeout` at a backend
that was never going to come up.

So `restart_backoff_seconds` — **0, 0, 1, 2, 4, 8, … capped at a minute.**

- **The first retry is free.** A failed start is usually followed by a human fixing what
  broke — a missing token, a command not on `PATH` — and then asking for a restart. Making
  *that* attempt wait punishes the one caller who already knows the answer. Repetition is
  what is being damped, so the delay starts on the second attempt.
- **It stops doubling.** A ceiling, not an ever-growing delay: the point is to make a loop
  cost nothing measurable, not to eventually give up on a backend. A minute is also short
  enough that an operator who fixed the problem is not left staring at a backend that
  refuses to come up.
- **A refusal is not an attempt.** Nothing about the backend changes: `status` and `error`
  keep saying why it is actually down rather than being overwritten with a complaint about
  timing, the failure count does not grow, and the floor does not move out — otherwise a
  caller in a tight loop would extend its own punishment indefinitely.
- **`restart` checks before it tears anything down**, rather than leaving it to `start`.
  A cooling-off backend has already failed, so `stop` has nothing to kill — but it would
  still set `status` to `STOPPED`, which reads as "somebody turned this off" rather than
  "this is broken and waiting to be retried".

`retry_after_seconds` is reported by `admin.health` and `gateway__backend_health`, because
down-and-not-being-retried-yet is a different situation from simply down, and an operator
watching a restart button do nothing deserves to see which. It is `0` whenever an attempt is
allowed, which is the ordinary case.

A config change clears all of this for free: `spawn_identity` changes, so
[`supervisor.md`](supervisor.md)'s reload replaces the `Backend` object outright and the new
one starts from zero failures.

## `startup_timeout` covers spawn and handshake together

Not two budgets. A backend that spawns instantly and then never answers `initialize` is as
unusable as one that never spawns, and separate budgets would let it consume both.

## `ping` is what makes health a health check

A subprocess can be alive and wedged, and only a round trip tells the two apart.
`gateway__backend_health` reports the round-trip time; a status dump that only read
`returncode` would call a hung backend healthy.

## `cwd`

Applied through the subprocess's own `cwd` (divergence 4 of 4 in
[`mcp_stdio.md`](mcp_stdio.md)). Chdir'ing the daemon is not available — it is shared by
every backend and by the socket it serves. The directory is **not** created: a `cwd` that
does not exist is a configuration error worth a clear failure at startup.


## `serving` and `origin_session`

A backend records which sessions have a call in flight on it, because that is how its own
`roots/list` or `elicitation/create` finds the client to ask.

**Explicitly tracked rather than read from a `contextvars.ContextVar`.** The contextvar
version looks correct and is not: a backend's server-to-client request is dispatched from
`MCPStdioClient`'s *read loop* task, which was created in `start()` — long before any call.
A task copies its context at creation, so the read loop's context can never contain a value
set by a later call, and every forwarded request would find nothing.

`origin_session()` returns `None` when no call is in flight, and also when **two different
sessions** have calls in flight at once — MCP gives no correlation between a
server-to-client request and the call that provoked it, so the honest answer there is that
we cannot tell. Two concurrent calls from one session are not ambiguous.
