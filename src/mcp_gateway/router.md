# `router.py`

Forwards one call to the backend that owns it, and translates what comes back.

Five methods: `tools/call`, `prompts/get`, `resources/read`, `completion/complete`, and
`resources/subscribe` — whose two halves share one function, since they differ only in which
call goes down the wire.

## Subscribing anticipates the refusal rather than relaying it

A backend without `resources.subscribe` would answer `-32601`, and passing that up would
name a method the client *did* call and the gateway *does* implement — which reads as a
gateway bug. The capability is checked first and the answer is `-32002`: this URI is not one
you can watch. Unsubscribing is forgiving in one direction: the gateway forgets the
subscription whatever the backend says, because a client that asked to stop has stopped, and
anything the backend keeps sending is filtered out on the way up.

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
| `completion/complete` | `-32603` | `-32602` (prompt) / `-32002` (resource) |

## Completion is a hint, and a hint has its own error budget

`completion/complete` is asked while somebody is still typing, so the standard by which it
is judged is different: it must never turn a half-filled form into a failed call.

**The `ref` is the routing key**, and the only thing rewritten. A `ref/prompt` carries a
namespaced name and a `ref/resource` a `mcpgw://` URI — resolved by the same
`find_prompt`/`find_resource` that `prompts/get` and `resources/read` use, and answered the
same way when they miss: `-32602` for a name, `-32002` for a URI. A `ref` of a type we do
not know is `-32602` rather than a guess, because there is no way to tell which backend it
meant. A known-but-down backend is `-32603`, the `prompts/get` rule, since a completion
result has no `isError` to carry the sentence instead.

**A backend that never declared `completions` gets an empty result, not an error.**
`{"values": [], "total": 0, "hasMore": false}` — which is what a completions-capable server
returns for an argument it cannot suggest for. A `-32601` would be the gateway denying a
method it advertises; a `-32602` would claim something was wrong with the params. Nothing
reaches the backend in this case, so nothing is stamped on it either.

**`context` is stripped for a backend that negotiated `2024-11-05`.** The member postdates
that revision, and sending it is a field the server never agreed to read: ignored by a
lenient one, refused by a strict one. The request is perfectly well-formed without it — a
cascade simply degrades into an unfiltered list, which beats a hint that fails the call. The
decision is here rather than in `mcp_stdio.py` because this is where the backend, and so its
negotiated version, is known.

**Nothing in the answer is rewritten.** `values` are *argument values* in the backend's own
vocabulary — an animal id, a country name — never names or URIs in the gateway's address
space. Rewriting one would corrupt the exact string the client is about to send back.

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
