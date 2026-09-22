# `session.py`

One `/mcp` connection's state and method table.

## Per connection, not per process

A `Session` holds the negotiated protocol version, the client's declared capabilities, and
that client's in-flight requests. Everything below — the supervisor, the catalogue, the
credential store — is shared by the whole process.

This mirrors `python-acp`'s "one agent per socket, one registry per process", and it is not
a style choice: `initialize` records *that* client's capabilities and *that* connection's
version, so a shared object would have the second client silently answered with the first
one's gates. Two clients on one daemon can legitimately negotiate different revisions, and
[`test_ws_lifecycle.py`](../../tests/test_ws_lifecycle.py) asserts exactly that.

## The method table

`initialize`, `ping`, the three listings, `tools/call`, `prompts/get`,
`resources/templates/list`, `resources/read`, `resources/subscribe`,
`resources/unsubscribe`, `completion/complete`, and `logging/setLevel`. It is closed:
anything else is `-32601` before a backend is consulted, which is what makes the advertised
capability block a promise rather than a guess. `completion/complete` resolves its `ref` the
way `prompts/get` and `resources/read` resolve their name and URI; see
[`router.md`](router.md).

Two of these leave state on the session rather than only forwarding. `resources/subscribe`
registers the connection against a URI, and `logging/setLevel` records the severity this
client wants — both are per-connection, and both are what a shared backend's notification is
filtered against on the way back up. See [`notifications.md`](notifications.md).

## `tools/list` is the one listing that is handed the session

Every other listing is a property of the backend pool and answers the same whoever asks.
`tools/list` leaves out the tools an operator has hidden, for everyone **except** the admin
UI's own bench connection — and the only thing it reads off the session to decide is
`client_info`, whose `name` the page sends as `mcp-gateway-ui`.

Keying on a name any client can claim is honest here because nothing is being protected. A
hidden tool is still callable by anyone who knows its name, so a client that lies about who
it is buys a longer list and nothing else. The bench needs the exemption for a reason of its
own: the header's checkbox list is built from this listing, and a filtered one could never
offer a hidden tool back. See [`visibility.md`](visibility.md).

## What may be called before `initialize`

`initialize` and `ping`, and nothing else.

A client that jumps to `tools/list` is answered `-32600` naming the problem rather than
served, because its capabilities are unknown at that point and answering would mean guessing
them.

`ping` is exempt because a load balancer or a supervisor may well ping before any client has
spoken, and refusing that would make a healthy daemon look down.

## `initialize` answers from configuration, immediately

Never from live backend state. Clients time `initialize` out, and a daemon may hold a dozen
backends that each take an `npx` cold start; blocking the handshake behind them is how a
gateway becomes unusable on a cold cache.

Backends are started when the *daemon* starts, not when a client arrives — see
[`gateway.md`](gateway.md). A client that lists before the first sweep finishes waits on the
supervisor's readiness event instead, which is a bounded wait it can see the result of.

## Cancellation

`_dispatch` registers the **current** task — obtained from `asyncio.current_task()`, not a
newly created one. The transport already gave every inbound message a task; wrapping again
would make `notifications/cancelled` land on a wrapper while the real work carried on.

From there the cancellation propagates on its own: the handler is awaiting a backend call,
and `MCPStdioClient.request`'s `except asyncio.CancelledError` branch abandons the request
and sends `notifications/cancelled` down the backend's wire. Nothing in this module has to
know that, which is why it composes at all.

## Responses from the client

A message classified as a *response* is an answer to something the gateway asked this client
— a forwarded `roots/list`, `sampling/createMessage` or `elicitation/create`. It is handed to
`gateway.resolve_client_response`, never to a handler. See [`gateway.md`](gateway.md) for the
origin-connection rule that decided which client was asked.
