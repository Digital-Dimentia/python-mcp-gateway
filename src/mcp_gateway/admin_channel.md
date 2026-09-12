# `admin_channel.py`

The `/admin` connection: a plain JSON-RPC method table for the UI.

## Why it is not MCP

A UI should not have to perform an MCP handshake, negotiate a protocol revision, and wrap
every question in a `tools/call` envelope just to ask which backends are up.

The inverse matters more: **admin verbs must not appear in the model's tool list.** A model
that can see `admin.secrets.set` is a model that can be talked into calling it. Two paths on
one port buys both properties for the cost of one `urlsplit`.

The implementations are shared with the `gateway__*` meta-tools — [`admin.md`](admin.md)
holds them once — so the two surfaces cannot drift into disagreeing about what "running"
means.

## No handshake

There is no `initialize` here. A method may be called immediately, because there is nothing
to negotiate: the method table is fixed, and a client that knows the URL knows the protocol.
`ping` exists so a UI can check liveness the way anything else does.

Authentication is the same as `/mcp`: the same key, checked in the same handshake hook. See
[`transport_ws.md`](transport_ws.md).

## Read-only in v1

| Method | |
|---|---|
| `admin.status` | uptime, bind address, every connection with its client info and version |
| `admin.backends` | every configured backend, running or not |
| `admin.health` | the above plus uptime, restarts, last error, live ping |
| `admin.config.get` | the catalogue as parsed, `${VAR}` references **unresolved** |
| `admin.secrets.keys` | key names only |
| `admin.logs.tail` / `.stop` | stream redacted log records as `admin.logs` notifications |
| `admin.reload` | re-read both files and apply the difference |
| `admin.backend.restart` | stop and respawn one backend |

The last two ship because they are the same code the meta-tools already expose to the model.
Withholding them from the UI while the model can call them would be theatre.

**Not implemented:** `admin.config.set`, `admin.secrets.set`, `admin.backend.add`/`remove`.
When the UI epic lands they go here and only here — a surface the model cannot reach.

## `admin.config.get` returns references, not values

A UI editing the config must see and write back `"${GITHUB_TOKEN}"`, not the token. Rendering
resolved secrets into an editor is how they end up pasted somewhere else, and it would make
the config surface a second way to read the credential store.

## `admin.config.get` reports `defaults:` as well as the servers

The servers come back with the block already folded into them, which is what they run with
— and is exactly why the block itself is reported too. Only it can say what *omitting* a
key would mean, and that is the question an editor adding a server has to answer before
there is a server to inspect. See [`config.md`](config.md).

## The log stream is installed *after* the redaction filter

`LogStream` formats records and sends them over a socket, so it must never be the thing that
reaches a credential first.

A `logging.Filter` on the root logger's *handlers* does not apply to a handler added later,
so the filter is copied onto this handler explicitly — and re-copied on reload, since the
stream's own copy would otherwise keep scrubbing the rotated-away value and stop scrubbing
the new one. See [`logging_redaction.md`](logging_redaction.md).

Subscriber queues are **bounded**, and a full one drops the record. A UI that stops reading
must not be able to make the daemon buffer without limit, and losing a log line for a stalled
client is the right failure.
