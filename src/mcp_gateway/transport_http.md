# `transport_http.py`

MCP's Streamable HTTP transport, on the same port and socket as the WebSocket one.

A client that speaks Streamable HTTP — Claude Code among them — attaches to the gateway
directly, with no `mcp-gateway-connect` in between. The bridge exists only because there was
nothing here.

## No framework, and why that is not stubbornness

[`pyproject.toml`](../../pyproject.toml) rules out an ASGI stack, and the reasoning is
recorded there: this process serves one protocol on one port, and the obvious dependency
tree would put two Rust-compiled per-architecture extensions into a daemon that proxies raw
dicts. What is here instead is a request reader, a response writer and an SSE writer — three
small, exactly-scoped things, because that is the whole of what the transport needs.

The cost is honest: no keep-alive, no chunked request bodies, no content negotiation beyond
one `Accept` check. Each of those is absent because nothing asks for it, not because it was
forgotten, and `response_bytes` says so where a reader would wonder.

## Sharing the port

`websockets` owns the listening socket and hands every connection to a `ServerConnection`,
which is an `asyncio.Protocol`. [`transport_ws.py`](transport_ws.md)'s `DivertingConnection`
subclasses it, reads the first bytes, and decides:

```mermaid
flowchart TD
    A[bytes arrive] --> B{head complete?}
    B -- no --> A
    B -- yes --> C{Upgrade: websocket?}
    C -- yes --> D[super().data_received<br/>library owns the connection]
    C -- no --> E{path == /mcp?}
    E -- no --> D
    E -- yes --> F[set_protocol → HttpProtocol<br/>replay the buffered bytes]
```

`/ui`, `/admin`, an unknown path and every WebSocket handshake take the `D` branch, so the
access-key check, the origin check and the UI's static assets keep the single implementation
they already had.

**Why not `process_request`.** That hook is the natural place and cannot do the job. It
returns one complete `Response`, so it can neither stream an SSE body nor read a request
body — and `websockets` has no reason to read one, because a WebSocket handshake never has
one. A POST would arrive with its JSON still in the socket buffer and nowhere to put it.

**Releasing the library.** A diverted connection leaves `handshake()` waiting on a future
nothing will resolve, so `open_timeout` fires ten seconds later and calls
`connection.transport.abort()` — our socket. An SSE stream meant to last hours would have
been cut at ten seconds. `_release_the_library` resolves the future *and* swaps in a
transport whose `abort` does nothing. Both halves are needed: the first alone still aborts,
the second alone leaks a task per request.

## The conversation

One path, `/mcp`, three methods:

| Method | Carries | Answer |
|---|---|---|
| `POST` | one JSON-RPC request | `200`, the response as `application/json` |
| `POST` | a notification, or a response to something we asked | `202`, no body |
| `GET` | nothing | an SSE stream of everything the server says unprompted |
| `DELETE` | nothing | `204`, and the session is gone |

**Responses come back on the POST, not on an SSE stream.** The spec permits either. This way
the only streaming body in the process is the `GET`, which is also the only place it buys
anything: a notification has nowhere else to go, while a response has a request still waiting
to carry it.

**A server-to-client request goes out on the GET stream and its answer arrives as a POST.**
That is how `roots/list`, `sampling/createMessage` and `elicitation/create` work here. A
client that never opens the stream can still call tools but cannot be asked anything —
the same position as a WebSocket client whose `initialize` declared no capabilities.

**A parse error is answered `200` with a JSON-RPC error inside.** It *is* the answer to a
well-formed HTTP request; a `400` would put it somewhere a JSON-RPC client is not looking.

## Sessions

`Mcp-Session-Id` is minted on the `initialize` response and required on every request after
it. It exists because HTTP has no connection to hang state on, and this transport's state is
not optional: the negotiated protocol version, the client's capabilities, its log level and
its resource subscriptions belong to *a client*, not to a socket.

The id is `secrets.token_urlsafe`, because it is a bearer token — whoever holds it can drive
the session, read what it subscribed to and answer what it was asked.

An unknown id is `404`, the spec's way of saying "start again with `initialize`". That is the
honest answer after a daemon restart, and inventing a session for an id we never issued would
let a client believe a subscription survived that did not.

A session outlives the GET stream carrying its notifications by `SESSION_GRACE_SECONDS`, so a
laptop sleeping does not cost a client its subscriptions, and a client that leaves without a
`DELETE` does not leak one. `GatewayServer` sweeps on a timer; see `reap`.

## `HttpLink` is the seam

It has the same shape as [`transport_ws.py`](transport_ws.md)'s `ClientLink` — `send`,
`notify`, `path`, `remote` — which is the entire interface `Session`, `Gateway` and the
notifier reach for. Neither learns that a client arrived over HTTP, and that is what stops
the transport split from spreading upward into code that has no business knowing.

The one difference is where an outbound message goes. A WebSocket link always has a socket;
this one has one only while the GET stream is open. Anything sent before or between streams
is buffered, bounded, dropping its **oldest** entry: a client returning after a gap is better
served by the most recent state than by a backlog it will discard anyway.
