# `errors.py`

Exceptions to JSON-RPC error objects, and the one rule that keeps a proxy honest.

A message is a concise sentence; structured detail goes in `data`.

## The `source` discriminator

A gateway sits between two peers that both speak JSON-RPC, and the codes are not
namespaced. Forwarding a backend's `-32601` unchanged leaves a client unable to tell *this
gateway has no such method* from *the backend behind it has no such method* — two problems
with completely different fixes, and only one of them the operator's.

So:

- An error **forwarded from a backend** always carries `data["source"] == "mcp"`, plus
  `data["backend"]`, `data["mcpCode"]`, and `data["mcpData"]` when the backend sent one.
- An error the gateway **originates never sets `source`**.

One key, checked one way, and the ambiguity is gone.

The corollary is the part that is easy to get wrong. A backend exception with **no code** —
a timeout, a dead transport, a malformed result — is *ours*, not the backend's. It becomes
`-32603` with `data = {backend, reason}` and **no `source`**, because tagging it would
claim the backend produced a code it never sent. That is the exact fidelity loss this
module exists to prevent, arriving by the back door.

## Messages are forwarded verbatim

A backend that failed has already written a sentence describing what failed, and it knows
things we do not. Replacing it with "backend error" destroys the only useful description in
the exchange. Context is added in `data`; the message is never rewritten.

## `data` is omitted when empty

Not `null`, not `{}`. Its presence is meaningful — a client checks `data.get("source")` —
so an always-present empty object would make "the gateway said nothing extra"
indistinguishable from "there is something here to inspect".

## Cancellation is not an error

`asyncio.CancelledError` is re-raised, never mapped. Swallowing it turns a cancelled task
into a completed one, which breaks cancellation everywhere above — including the `_abandon`
path that tells a backend to stop working on a reply nobody will read. The `as_error_object`
decorator re-raises it explicitly, ahead of the general `except Exception`.

## A marker, not an import

`to_error_object` recognises a backend exception by `getattr(exc, "mcp_error", False)`
rather than by `isinstance(exc, MCPProtocolError)`.

This module is imported by `config.py` and `naming.py`, which have no business dragging in
a subprocess client. An import-local `from mcp_gateway.mcp_stdio import ...` inside the
mapping function would move that cost onto every error instead — and would make the two
modules import each other in a cycle the moment `mcp_stdio` wants to build an error object
of its own. A one-line class attribute on the exception costs nothing and inverts the
dependency.

## `maps_errors`

`as_error_object` stamps `maps_errors = True` on the wrapper, so a test can assert every
registered handler carries the decorator rather than trusting that nobody forgot one. An
undecorated handler does not fail loudly — it fails by letting an exception escape into the
transport's generic catch, which answers `-32603` with a stack-trace-ish message and loses
the code the caller needed.

## `GatewayErrorObject`

Carries an already-built error object up to the transport that will write it. A distinct
type so the transport can tell *this was mapped deliberately, send it* from *this escaped
unmapped, and something above forgot the decorator* — the second deserves a log line and
the first does not.
