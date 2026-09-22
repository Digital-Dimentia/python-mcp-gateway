# `admin.py`

The gateway's own meta-tools, and the `/admin` method table they share an implementation
with.

## One implementation, two surfaces

- **`gateway__*` MCP tools on `/mcp`** — so the model can diagnose its own missing tools.
  "GitHub is not configured" is an answer a model can give a human; a silently absent tool
  is not.
- **`admin.*` JSON-RPC methods on `/admin`** — so the future UI never has to speak MCP to
  ask which backends are up.

Both are always available, even with zero live backends. That is why
[`protocol.md`](protocol.md) can advertise `tools` unconditionally: there is always at least
this much to list.

## The five tools

| Tool | Answers |
|---|---|
| `gateway__list_backends` | every configured backend, running or not, and why not |
| `gateway__backend_health` | the above plus uptime, restarts, last error, and a **live ping round-trip** |
| `gateway__restart_backend` | stop and respawn one, re-reading its credentials |
| `gateway__reload_config` | re-read both files and apply the difference |
| `gateway__clipboard` | the admin UI's bench session as one document — see [`clipboard.md`](clipboard.md) |

The live ping is what makes health a health check rather than a status dump: a subprocess
can be alive and wedged, and only a round trip tells the two apart. `ping_ms: null` means
"did not answer", which is not the same fact as "not running".

Every schema sets `additionalProperties: false`, so a model's invented argument fails loudly
instead of being ignored.

## Two of the schemas depend on the configuration

`backend_health` and `restart_backend` take a backend name, and that property carries an
`enum` of the configured servers rather than being a bare string. For a person the admin UI
renders it as a dropdown with no UI code at all — [`schema_form.js`](../mcp_gateway_ui/schema_form.js)
already turns an `enum` into a `<select>`. For a model it is the difference between a name
it has to have learned from `list_backends` and remembered, and one it can simply choose;
a typo stops being a runtime failure and becomes something that cannot be expressed.

**A tool definition that varies is a change in kind, so it is worth saying why it is safe.**
A client may cache a listing, and this one goes stale when the catalogue changes — but that
is exactly the case MCP has a signal for, and the gateway already sends it: `reload` and
`restart_backend` each emit `notifications/tools/list_changed`. A client that holds a
listing is told to re-read it, whether the change was a backend appearing or this enum
following it.

Two smaller calls, both deliberate:

- **Every configured backend, not just the running ones.** A disabled or failed backend is
  the one you most want to ask about or restart, and omitting it would have the dropdown
  answer "no such server" for a server the operator can see in the header.
- **No `enumNames`.** A backend's name is what the header, the log lines and `servers.yaml`
  all call it. A dropdown showing its description instead would be the single place in the
  UI naming it something else.

With no backends configured the `enum` is omitted entirely rather than published empty: an
empty `enum` matches nothing, so it would be a field no value could satisfy.

## `admin.status` carries the branding

The one non-status thing in that payload, and deliberately: the page needs the deployment's
title and icon at exactly the moments it needs the rest of `admin.status` — on connect, and
again after every reload — so a method of its own would be a second round trip that is
always made beside the first. The icon travels inline as a `data:` URI because the desktop
shell cannot fetch a URL for it; [`branding.md`](branding.md) has the whole argument, and
the 128 KiB cap that keeps this payload a status payload.

## Credential values never appear in either payload

`describe_backend` reports `env_keys` — the *names* of the variables from the server's `env`
block. There is no way for a value to reach the payload even by accident, because a
`ServerSpec` holds the `${VAR}` templates rather than resolved values; that is the point of
the split described in [`config.md`](config.md).

`gateway__clipboard` quotes backend *output* — payloads the same client could have fetched itself through `/mcp` — and reads the credential store not at all.

`admin.secrets.keys` likewise returns key names. There is no method anywhere that returns a
value.

`tests/test_admin_tools.py` asserts that a known secret appears nowhere in either payload —
and also that the *store-side* key name (`ALPHA_TOKEN`) does not, since only the child-side
name (`MY_TOKEN`) is the backend's business.

## `hidden_tools` is runtime, not spec

`describe_backend` reports every field from the backend's `ServerSpec`, plus one that is not:
`hidden_tools`, the names `tools/list` is currently leaving out. It rides on
`admin.backends` rather than behind a read method of its own, so the admin UI learns it in
the refresh it already makes and there is one source of truth rather than two to keep in
step — and so a backend that is down, and lists nothing, still tells the UI what to offer
back.

It is passed in rather than read off `Backend`, because `skipped_tools` beside it in the
health payload is the catalogue's *observation* about a backend while this is an operator's
*policy* about it, and a process object carrying both is how the two get confused across a
restart. The key is not `tools`, which would read like something `admin.backend.add`
accepts. Names only. See [`visibility.md`](visibility.md).

## Errors: which kind, and why

- **An unknown backend name** → a tool-level failure (`isError: true`), so a model can read
  it and say which backends *do* exist.
- **An unknown `gateway__*` tool** → `-32602` with `knownTools`. That is a caller mistake,
  not a runtime condition, and a protocol error is the honest answer.

The same distinction governs backend tool calls; see [`errors.md`](errors.md).

## What is deliberately not here

There is **no `gateway__set_secret`, and no `admin.secrets.set` in v1.**

Writing a credential from a tool call means a model can be talked into writing one, and the
blast radius of this particular process is every credential on the machine. When the UI epic
adds secret writing it goes on `/admin` only — a surface the model cannot reach — never on
`/mcp`. Both halves of that are recorded here because the second half is the one that gets
forgotten.
