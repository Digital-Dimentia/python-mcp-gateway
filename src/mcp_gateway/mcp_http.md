# `mcp_http.py`

The client side of MCP's Streamable HTTP transport: how the gateway talks to a backend that
is a URL rather than a process it spawns. The session itself (ids, pending futures, the
handshake, pagination, cancellation, server requests) is `MCPClient` in
[`mcp_stdio.py`](mcp_stdio.md). This module only supplies `start`, `stop` and `_write`, and
passes whatever it reads to `_handle_message`, the same way the stdio read loop does.

## Why a backend can be a URL

The gateway was built for backends it owns: stdio children, their environment built from
nothing, their lifetime tied to its own. That model breaks in a container. There each MCP
server is usually a container of its own, listening on a port, and the gateway can neither
spawn it nor hand it an environment. So a `servers.yaml` entry can name a `url` instead of a
`command`, and to everything above this module the difference does not exist. It has the
same tool names, the same catalogue and the same status, and the same failure shows up as
readable content.

## The conversation

| Direction | Sent as | Answered with |
|---|---|---|
| our request | `POST`, one JSON-RPC message | `200` as `application/json`, or an SSE stream ending in our response |
| our notification, our answer to a server request | `POST` | `202` (any 2xx is accepted) |
| the server, unprompted | our `GET` stream, opened after the handshake | SSE, for as long as the server holds it |
| end of session | `DELETE` | whatever it likes; `405` is fine |

Every request after `initialize` carries `Mcp-Session-Id` (when the server minted one) and
`MCP-Protocol-Version` (the revision `initialize` settled on). The server may not reuse a
session or guess a version, and sending both makes a server that checks strictly behave
the same as one that does not.

**A request is sent from a task, not from `_write`.** `MCPClient.request` holds the write
lock around `_write`, and a tool call can take minutes. If `_write` waited for the response,
the second call would queue behind the first. So a request's POST and its response belong
to a task keyed by the request id, which resolves the pending future like any other inbound
message. That task also settles the future on every failure path: connection refused, a
`500`, or a stream that ends before our reply. The caller gets an `MCPProtocolError` at
once instead of waiting out `request_timeout`.

**Everything else waits for its `202`.** `notifications/initialized` has to land before the
first request after the handshake. A server may refuse requests until it has been told
initialization is over, and a race here would be the client's fault.

**Server requests work on both streams.** A server can send `roots/list` on the SSE stream
of the POST that provoked it or on the GET stream. Either way it reaches
`_handle_message`, and the answer goes back as a POST. `tests/test_http_backends.py` runs
the roots round trip both ways.

## A session that expires

A `404` on a request that carried a session id means the server does not know us any more.
Usually it restarted, which in a container is ordinary. MCP's answer is a new `initialize`,
so the client runs one, only once however many requests hit the 404 together, and then
re-sends the request that noticed. The caller sees the call succeed.

A renewed session could belong to a different build of the server, publishing different
tools. So after a renewal the client raises `list_changed` for each capability the server
declares, as if the server had sent it, and the gateway re-lists.

Nor does the new session carry what the gateway had set on the old one: the resource
subscriptions and the `logging/setLevel`. This module cannot replay those -- it knows a
session was renewed and nothing else, not the backend's name and not who subscribed to
what. So it raises one more notification, `protocol.SESSION_RENEWED`, on the channel a
backend's notifications already travel, and `Gateway._session_renewed` does the repair it
already does for a restarted process: `resubscribe` and `apply_log_level(only=...)`. The
alternative was a callback wired from `Supervisor` down into the transport, which would
have been a second path from a backend to the gateway saying the same kind of thing.

If the renewal itself fails, the next request tries again before it is sent. Nothing is
sent into a session that no longer exists.

## The old transport, and why it is not here

MCP 2024-11-05 had a different HTTP transport: `GET /sse` opens a stream whose first event
announces a POST endpoint, every message the client sends goes there, and every reply comes
back down the GET stream. `2025-03-26` replaced it with the one this module speaks, and
`url` backends speak only the replacement.

Supporting both would mean a second message-routing shape in here, not a second header. In
Streamable HTTP a request's reply arrives on its own POST response, which is what
`_exchange` is built around -- send, read the answer, settle the future. In HTTP+SSE the
POST is acknowledged `202` and the reply arrives somewhere else entirely, on a stream that
is also carrying everything else. That is a parallel `_exchange`, a parallel `_listen`, a
`transport:` key in the schema and a second mock server, kept alive permanently for a
transport the spec deprecated in March 2025. A server that still speaks only the old one
can be fronted with a Streamable HTTP proxy -- one more container beside it, in a
deployment that is already containers.

What the gateway does do is say so. A handshake that fails is followed by one bounded GET,
and a stream that opens with an `event: endpoint` is a server speaking the old transport
rather than a dead one -- so `gateway__list_backends` reports that, with the shim as the
fix, instead of the `HTTP 405` an operator would otherwise have to interpret. See
`_legacy_sse_hint`.

## Why there is an HTTP client here instead of a dependency

The same reason [`transport_http.py`](transport_http.md) has a hand-written server side,
and `pyproject.toml` has the long form. Every HTTP client on PyPI adds either a
dependency tree or an ASGI-era stack to a daemon that proxies dicts. What this transport
needs from HTTP/1.1 is small and fixed: one request per connection, `Connection: close`,
a body delimited by `Content-Length`, by chunked encoding, or by the connection closing,
and an SSE parser. With one connection per request there is no pool to get wrong and no
keep-alive state to leak between calls.

What that choice leaves out, on purpose:

- **Proxies.** `HTTP_PROXY` is not honoured. The deployment this exists for is backends on
  the same host or the same container network, where a proxy would be a misconfiguration.
- **Redirects.** A `3xx` fails the call. Following one would send the backend's
  credential headers to a host the operator never named.
- **HTTP/2.** Nothing in MCP needs it.

`https` uses `ssl.create_default_context()`, so the system trust store and `SSL_CERT_FILE`
apply the way they do for any OpenSSL client. A private CA goes in `SSL_CERT_FILE` in the
daemon's own environment.

## Headers are the credential, so they get `env`'s rules

`headers:` in `servers.yaml` holds templates such as `Bearer ${TOKEN}`, resolved from the
credential store at start by `backend.resolve_headers`. A missing reference fails that one
backend with the same message as a missing `env` reference, and
`secrets.missing_for` reports it the same way to `--check` and to the UI. Only the header
**names** are ever reported. The values live on the client and nowhere else, and the
reload digest in `supervisor.spawn_identity` hashes them so that rotating a token restarts
the session.

The headers the transport sets itself (`Host`, the framing headers, `Accept`, the MCP
session and version headers, `Last-Event-ID`) are refused in config (`RESERVED_HEADERS`).
A config line could otherwise split a request or impersonate a session. A resolved value
containing a line break is refused at start, because `config.py` checks the template but
cannot see a value that comes from `gateway.env`.

## Related

- [`mcp_stdio.py`](mcp_stdio.md): `MCPClient`, the session this is a transport for
- [`backend.py`](backend.md): which client a backend gets, and `resolve_headers`
- [`config.py`](config.md): `url` and `headers`, and what an HTTP entry refuses
- [`transport_http.py`](transport_http.md): the same transport, server side
