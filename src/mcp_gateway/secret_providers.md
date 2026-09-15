# `secret_providers.py`

The seam in front of [`secrets.py`](secrets.md). `gateway.env` stays the default and the
last word; a deployment that keeps credentials somewhere else plugs in a Python file of its
own and never forks this project.

## The block

```yaml
# servers.yaml
secrets:
  providers:
    - provider: "./providers/vault.py:VaultProvider"
      options:
        addr: "https://vault.internal:8200"
        mount: "kv/mcp-gateway"
    - "acme_secrets.aws:SecretsManager"      # the short form, when there are no options

servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}
```

Providers are asked **in order, and the first answer for a key wins**. `gateway.env` is
always the last link and is never named in the block — there is no way to turn it off, and
that is the point of the ordering: `GITHUB_TOKEN` comes from Vault, `WS_ACCESS_KEY` goes on
living in the file it is already in, and nobody has to move a working credential to adopt a
provider.

The file being *last* rather than first is the load-bearing half. Put it first and a stale
local copy of a rotated credential silently beats the live one — the same failure the
duplicate-key rule in [`secrets.md`](secrets.md) refuses one file down, reintroduced one
file up.

## The interface

```python
class SecretProvider(Protocol):
    def load(self, request: SecretRequest) -> Mapping[str, str]: ...
```

`SecretRequest` carries three things: `keys`, every `${NAME}` the catalogue references plus
the daemon's own `WS_ACCESS_KEY`; `options`, this entry's mapping verbatim; and
`config_path`, so a provider resolving a relative path of its own resolves it against the
catalogue rather than against whatever directory launchd or a container entrypoint happened
to start the daemon in.

A whole provider, which is the shortest useful demonstration of the shape:

```python
# ./providers/vault.py
import hvac

class VaultProvider:
    def __init__(self, addr, mount):
        self._client = hvac.Client(url=addr)
        self._mount = mount

    def load(self, request):
        out = {}
        for key in request.keys:
            try:
                read = self._client.secrets.kv.v2.read_secret_version(
                    path=f"{self._mount}/{key}"
                )
            except hvac.exceptions.InvalidPath:
                continue          # not ours; a later link may have it
            out[key] = read["data"]["data"]["value"]
        return out
```

Options arrive as keyword arguments, so a provider taking none needs no `__init__` at all.
The attribute may also be a factory, or an object that already has a `load` — a
module-level singleton works.

[`examples/vault_provider.py`](../../examples/vault_provider.py) is a runnable one with no
third-party dependency, used by the tests.

### Omit a key you do not have; raise when you are broken

The two failures look identical from the outside and must not be reported the same way.
**Returning fewer keys** means "not mine" and is ordinary — the chain continues, and a key
nobody answers is reported by the existing missing-secret path, which already names the
config site that referenced it. **Raising** means "I could not reach my backing store", and
it is fatal: startup refuses, and a reload refuses without changing anything.

Returning `{}` on an outage would hand every backend an empty token and turn one clear
error into N confusing 401s from third parties hours later.

## Why a snapshot, and not `get(key)`

Lazy per-key resolution is the obvious design and it is wrong here, for three concrete
reasons rather than a stylistic one:

**Redaction.** [`logging_redaction.py`](logging_redaction.md) installs a filter holding
every value onto the **root** logger, once. A value fetched after that moment is a value
the filter has never heard of, and it is unredacted in every log line, every traceback and
`admin.logs.tail` for the life of the process. Lazy fetching makes that the normal case.

**Atomic reload.** [`gateway.py`](gateway.md) reloads by building a new config and a new
store and only then replacing the running ones, which is why a typo costs an operator an
error message instead of a daemon with six backends missing. A lazy store cannot fail at
reload — it has fetched nothing yet — so it fails afterwards, one key at a time, half
applied.

**Answering before starting.** `--check` and `admin.secrets.missing` say what a deployment
is missing without spawning anything. Against a lazy store there is nothing to inspect.

The snapshot's cost — a provider outage is fatal rather than deferred — is the behaviour
you want anyway, and it lands at startup where somebody is watching.

## Why the request names the keys

So a remote store never needs an enumeration API. Listing a Vault mount and reading a path
inside it are different permissions, and a gateway should need the smaller one. The keys
come from scanning the catalogue for `${NAME}`.

**Disabled servers are included**, which is the one place this deliberately differs from
`secrets.missing_for`. That function answers "what is broken right now", where a parked
backend is not the operator's problem. This answers "what might this catalogue want", and
fetching a key for a backend somebody is about to enable costs one lookup — while not
fetching it costs a reload that fails on the first line of the block they just uncommented.

A provider may return keys nobody asked for. They are kept.

## Why sync

Provider files in the wild reach for `boto3`, `hvac` and `requests`, all synchronous. An
`async def load` would tax every third-party file to spare this module one
`asyncio.to_thread` — and would make `--check`, a sync path, spin up an event loop to
validate a file. The daemon calls providers through `to_thread` on reload so a slow fetch
does not stall attached sessions.

## No `${VAR}` in this block, and the error says so

There is nothing to interpolate against: the block decides how the store is built, so it
necessarily parses before any store exists. A `${VAULT_TOKEN}` here could only resolve from
`os.environ`, which is precisely the fallback [`secrets.md`](secrets.md) exists to refuse.

`_validate_ref` catches it explicitly rather than letting it fail later as a missing
module, because `no module named '${VAULT_DIR}/vault'` reads as a path bug and sends the
operator looking in the wrong place. A provider that needs a credential of its own reads it
itself, in code the deployment owns.

## Loading a provider runs the operator's code

`provider: "./providers/vault.py:VaultProvider"` imports a file and calls into it. That is
arbitrary code execution and it is **not a new trust boundary**: `servers.yaml` already
names `command:` lines this daemon spawns as itself, so anyone who can edit the catalogue
can already run code. What is new is only that the code runs in-process.

The attribute half of a ref is always required — `:VaultProvider` rather than an implied
default name — because one file may reasonably hold two providers, and an implicit
convention is something to remember rather than a line to read.

A file-backed provider module is cached in `sys.modules` under a name derived from its
resolved path, so a reload does not rebuild a provider that is holding a pooled connection.
Editing a provider file therefore needs a daemon restart, not a reload; that is the right
trade, because silently re-executing third-party code on every SIGHUP is worse.

## Parsing never imports anything

`config.load` validates the block into `ProviderSpec`s and stops. The import and the
instantiation happen in `build_store`, so checking a catalogue's shape cannot run anybody's
Python, and a reader looking for "where does this execute code" finds one function.

## What did not change

[`bridge.py`](bridge.md) still reads `gateway.env` and only `gateway.env`. It is a client
process a user's MCP client spawns from an arbitrary directory, not the daemon, and handing
it the daemon's provider credentials so it could fetch one access key would widen the blast
radius of every bridge on every laptop. A deployment putting `WS_ACCESS_KEY` behind a
provider passes it to the bridge through `MCP_GATEWAY_WS_ACCESS_KEY` instead.

`SecretStore` gained `origins` — a key-name-to-source map, reported per key by
`admin.secrets.list` — and nothing else. Every existing consumer of a store, the redaction
filter and `interpolate` included, sees the same object it always did, because the chain's
whole output is an ordinary store.
