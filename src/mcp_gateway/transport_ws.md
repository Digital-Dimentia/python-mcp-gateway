# `transport_ws.py`

Binds the gateway to a WebSocket, and decides who is allowed to speak to it.

Lifted from `python-acp`'s module of the same name — the access-key hook, the loopback bind
refusal, the message-size cap and the keepalive settings are that module's, comments
included. What is **not** lifted is everything above the frame: no ACP SDK, no `run_agent`,
no `Transport` conformance. A decoded message goes to `Session.handle`, which is our own
method table.

## Two paths on one port

`/mcp` speaks MCP to any number of clients. `/admin` speaks a separate JSON-RPC method
table to the future UI.

The split is not cosmetic. Admin verbs must not appear in the model's tool list, and a UI
should not have to speak MCP to ask which backends are up. Anything else gets a 404 during
the handshake, before a connection object exists.

## The bind guard

`refuse_unauthenticated_bind` refuses a non-loopback bind with no key, unless
`MCP_GATEWAY_WS_ALLOW_UNAUTHENTICATED` is `1`/`true`/`yes`.

The threat is specific and not hypothetical: this daemon spawns the commands named in its
config and holds every credential on the machine in one file. A socket anyone can open is
both arbitrary code execution and a credential oracle, as whoever runs the daemon. On
loopback that is the design — the client is the user. On any other interface it is a remote
shell with no password.

`is_loopback` **fails closed**. `None` and `""` mean every interface to `serve()`, and a
name we cannot parse as an address is not resolved here — a DNS lookup at startup is a side
effect this function has no business having, and one that could answer differently later.

The opt-out is read **strictly**: a permissive reading would turn
`MCP_GATEWAY_WS_ALLOW_UNAUTHENTICATED=0`, which says the opposite, into consent.

The guard lives here rather than in `cli.py` so a caller embedding `GatewayServer` in its
own program inherits it instead of having to remember it. It runs in the **constructor**,
not in `start()`: the point is that the misconfiguration never gets as far as a listening
port.

## The access key

`MCP_GATEWAY_WS_KEY` first, then `WS_ACCESS_KEY` in `gateway.env`. The environment wins so a
container or a systemd unit can override a checked-out credential file without editing it.
The credential store is the natural second home: it is already gitignored, mode-checked and
covered by redaction.

An **environment variable and not a CLI flag**, deliberately: argv is world-readable through
`ps`, so a `--ws-key` flag would publish the secret to every other account on the machine at
the moment it is used to protect it. An empty value reads as unset, because
`MCP_GATEWAY_WS_KEY=` is how someone spells "I turned this off" — and treating it as a key
that matches only the empty string would refuse every client that sent nothing while
admitting one that sent `?key=`.

Checked during the opening handshake, so a rejected client never reaches `initialize`.
Exactly one offered key is accepted, so `?key=wrong&key=right` cannot be smuggled past a
check that scanned for any match, and `compare_digest` keeps it constant-time. The 401 body
carries no detail: a rejected client is unauthenticated by definition and has no claim on
knowing whether the key was absent, wrong, or duplicated.

### The carrier

`?key=` is accepted because it is the one carrier every WebSocket client library can send
without custom header support, and a key nobody can present protects nothing. It is the
wrong place for a secret on principle — URLs are what get written down, in proxy and server
access logs.

**And that is not hypothetical here**: `websockets` itself logs the full HTTP request line
at DEBUG, query string included. So the resolved access key is passed to
`install_redaction` as an extra value regardless of which source it came from — see
[`logging_redaction.md`](logging_redaction.md). `Authorization: Bearer` is accepted too and
is what the bridge sends; retiring the query form is filed as its own issue.

`_report_access_key` logs whether authentication is on and **never what the key is**. It
exists for the deploy, not for debugging: an unset variable in a unit file, an interpolation
that expanded to nothing, a secret mounted after the process started — every one of those
produces a daemon that runs perfectly and accepts anybody, and the only other evidence is a
connection that should have been rejected and was not, which nobody is watching for. The
*length* is included because it separates "the deploy passed nothing" from "the deploy
passed something truncated or quoted".

## Keepalive and size caps

`ping_interval` and `ping_timeout` are restated at 20 s rather than inherited from
`websockets`, because they are client-facing contract. MCP defines a `ping` method, but a
client that correctly sends nothing of its own on an idle connection depends on *these*
frames to hold a NAT or proxy mapping open — and a daemon makes an hours-idle connection
the normal case rather than an edge one. Inheriting them would leave that dependency written
down nowhere and free to change under a pin bump with no test failing.

`MAX_MESSAGE_BYTES` is 50 MiB, matching the backend client's stream limit. `websockets`
defaults to 1 MiB, which a single multimodal tool result can exceed, and a client that hits
the cap gets its connection *closed* rather than an error — so the two directions must not
disagree about the size of a message they will both be asked to carry.

## One task per inbound message

A request is dispatched into its own task, so a slow `tools/call` — which may be sitting
behind a backend that is waiting on a human — does not stop the loop from reading the
`notifications/cancelled` that would un-ask it. That is the same discipline `mcp_stdio.py`
applies in the other direction, for the same reason.

On disconnect, this connection's in-flight tasks are cancelled **before** the session is
told it is gone. That cancellation propagates down through `MCPStdioClient.request`, which
turns it into a `notifications/cancelled` on the backend's own wire: a client that hung up
must not leave a backend working on a reply nobody will read.

## Framing is ours; dispatch is not

Everything below JSON — a parse error, a payload that is not an object, a message that is
none of request/notification/response — is answered here, because there is no id to carry it
upward and no handler to route it to. A parse error is answered with `id: null`, which is
what the spec reserves that for.

`_serve_one` distinguishes a `GatewayErrorObject` (mapped deliberately by
`@as_error_object`) from anything else (something above forgot the decorator). The second
is logged as the bug it is — and still answered, because a client waiting forever is worse
than a generic code.


## The startup line is a contract now

`listening on ws://host:port/mcp` is how the desktop shell learns which port `--port 0`
landed on: it spawns this daemon, reads its stderr, and parses that line. So the format is no
longer only a log line. `tests/test_desktop_contract.py` pins it, and
`desktop/src-tauri/src/supervisor.rs` carries the twin of the parser — each names the other.

The same shell is why the `Origin` rule's "a request with no `Origin` proceeds" branch is now
load-bearing rather than merely convenient: the shell's own client is `tokio-tungstenite`,
which sends none. That, plus `Authorization: Bearer` already being accepted, is the whole
reason this module needed no change to support a desktop app. See `desktop/README.md`.
