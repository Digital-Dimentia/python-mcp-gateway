# `jsonrpc.py`

JSON-RPC 2.0 message classification and framing. No MCP knowledge at all.

Used by all three transports — the daemon's WebSocket server, the backend stdio client, and
the bridge. Kept protocol-agnostic so the one thing every direction must agree on is
decided in one place.

## Classification is by shape

JSON-RPC has no discriminator field. A message is:

| | `method` | `id` | |
|---|---|---|---|
| request | yes | yes | must be answered exactly once |
| notification | yes | no | must **never** be answered |
| response | no | yes | carries `result` or `error` |

Getting this wrong in a proxy is how a notification ends up answered, which a conforming
peer treats as a protocol violation because it never asked — and the symptom is an
unsolicited response the peer cannot correlate, which most clients log and ignore, so the
bug survives for a long time.

## `id: null` is not a request

The spec reserves `null` for a response to a request whose id could not be determined.
Treating it as an id would let a caller open a correlation nothing can ever answer, and
would make `failure(None, ...)` — the only correct reply to a parse error — ambiguous with
a real response.

## `encode` adds no newline

The stdio framing is newline-delimited and adds one; the WebSocket framing is
message-oriented and must not. Putting the newline in `encode` would make the WS path emit
a trailing byte inside every frame, which is legal JSON and therefore invisible until
something downstream does a byte comparison.

`ensure_ascii=False` because both wires are UTF-8, and escaping every non-ASCII character
would inflate a tool result full of ordinary prose for nothing.
