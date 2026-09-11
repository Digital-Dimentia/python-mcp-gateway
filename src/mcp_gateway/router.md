# `router.py`

Forwards one call to the backend that owns it, and translates what comes back.

Three methods: `tools/call`, `prompts/get`, `resources/read`.

## A dead backend answers differently per method, deliberately

This is the most user-visible decision in the project.

**`tools/call` on a known-but-down backend returns a successful result carrying
`isError: true`** — not a protocol error.

MCP's own contract is that tool-level failure is a successful result with `isError`, *so the
model can read it and adapt*. A model that gets

> Backend 'github' is not running: missing secret GITHUB_TOKEN. Call gateway__backend_health
> for details.

can tell the human that GitHub is not configured. A model that gets `-32603` gets a turn that
died on a protocol error it has no way to act on, and a human who sees "an error occurred".

That matters more in a daemon than it would in a launcher: a backend can die at any point in
a week of uptime, and the client that notices is mid-turn.

**`prompts/get` and `resources/read` have no such escape hatch** — neither result type has an
`isError` — so they answer `-32603` and `-32002` (MCP's `RESOURCE_NOT_FOUND`) respectively.

**An unknown backend prefix or an unparseable name is `-32602`**, whatever the method, with
`knownBackends` in `data`. That is a caller mistake rather than a runtime condition, and
telling those two apart is the entire value of the distinction.

| | backend down | name unknown |
|---|---|---|
| `tools/call` | result, `isError: true` | `-32602` |
| `prompts/get` | `-32603` | `-32602` |
| `resources/read` | `-32002` | `-32002` |

## Results are forwarded verbatim

`content`, `annotations`, `structuredContent`, `_meta`, and whatever the next revision adds
all pass through untouched. A proxy that reshaped them would be lossy against every revision
it had not been taught — the same reason the daemon parses no models at all.

## The backend name is stamped on the way out

`MCPProtocolError.backend` is set as the exception passes through, so
[`errors.md`](errors.md) can put it in `data["backend"]` without every call site having to
remember to pass it. The `source` tagging, and the rule that a codeless failure is *ours*
rather than the backend's, live there.


## `serving` wraps every forwarded call

`Backend.serving(session)` is what lets a `roots/list` or `elicitation/create` raised *by
this call* find its way back to *this* client. It is the router's job because the router is
where the backend is resolved. See [`backend.md`](backend.md) and [`gateway.md`](gateway.md).
