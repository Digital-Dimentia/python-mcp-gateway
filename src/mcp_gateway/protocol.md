# `protocol.py`

MCP method names, protocol versions, and the capability block the gateway advertises.

## Why the constants live here and not in `mcp_stdio.py`

The template held them in its MCP client, because it spoke MCP in one direction only. The
gateway speaks it in both: `mcp_stdio.py` negotiates with each backend and
`transport_ws.py` negotiates with each client. Two copies would be two things to keep in
step, and a mismatch between what we accept *from* a backend and what we accept *from* a
client is not a crash — it is a silent interop asymmetry, where a version a backend may
speak is one a client may not.

This is divergence 3 of 4 from the lifted module; see [`mcp_stdio.md`](mcp_stdio.md).

## Versions

Proposed: `2025-06-18`. Accepted: that, plus `2024-11-05`.

`2025-06-18` because `elicitation` is a client capability the gateway forwards and it does
not exist before that revision. Nothing in the framing, the handshake, or the shutdown
sequence differs between the two, and the fields the newer revision adds
(`structuredContent`, resource links) are passed through untouched — which is what a proxy
wants regardless.

`2024-11-05` stays in the *accepted* set while no longer being what we *propose*. A backend
pinned to it must counter with it, and hanging up on that counter would drop every server
that has not moved yet.

`negotiate_version` treats a **missing** `protocolVersion` as `2024-11-05` rather than as
an error. Servers of that vintage omit it, and refusing them over a field their revision
did not require would break working backends for no protection.

## The capabilities are advertised unconditionally

`tools`, `prompts` and `resources`, always, with `listChanged` on each — not the live union
of what the backends currently offer.

This looks like a violation of the rule that you must never declare what nothing answers,
so the reasoning is worth stating plainly:

- **MCP capabilities are fixed at `initialize` and cannot change afterwards.** A backend
  that starts failed and comes up later via `gateway__reload_config` would need `prompts`
  to *appear* on an already-negotiated connection. That is structurally impossible, so
  advertising the live union is broken by construction — and a daemon holds connections
  across many reloads.
- Advertising the union of the *configured* set is no better: it would make the block
  depend on subprocess startup races, so two runs of one config could advertise
  differently.
- The rule is honoured **in substance** because the gateway can always answer. A
  `prompts/list` with no prompt-capable backend returns `{"prompts": []}` — exactly what a
  real server with no prompts returns, and a case every client already handles. Nobody is
  ever stranded on a `-32601`.

`subscribe: false` is the one live claim in the block, and it is negative: nothing
implements `resources/subscribe` yet, so saying otherwise would strand a caller.

## The manifest

`CAPABILITY_MANIFEST` records, for each advertised key, what answers it.
`tests/test_capabilities.py` refuses any key in `advertised_capabilities()` without a row.
The pattern is lifted from the template; the rows are not, because they described ACP.

## What the gateway declares *downward*

Not in this module — see `gateway.py`. The gateway mirrors to each backend the union of
what its currently-attached clients declared, recomputed on connect and disconnect. A
backend cannot renegotiate, so the union is the only honest answer, and the
origin-connection routing rule ensures a request we accept can always be placed.
`RELAYED_SERVER_REQUESTS` is the set that gets forwarded; everything else a backend asks
receives `-32601`.
