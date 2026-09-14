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
