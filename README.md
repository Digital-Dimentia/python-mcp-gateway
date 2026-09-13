# python-mcp-gateway

An MCP gateway daemon: **one WebSocket MCP server, many stdio backends, one credential
store.**

Every MCP server you use today needs its own token wired into its own client config, and
every one of them inherits whatever happened to be in the shell that launched your client.
This daemon puts every credential in one gitignored `gateway.env`, injects each secret into
only the subprocess that needs it, and presents the union of all their tools, prompts and
resources as a single MCP server.

Adding an integration is one entry in `servers.yaml` and one line in `gateway.env`. Your
client config never changes again.

```
Claude Desktop / Claude Code        your browser
   │ stdio                             │ http://127.0.0.1:8765/ui
   ▼                                   ▼
mcp-gateway-connect ─────────┐    admin UI ──► /admin  and  /mcp
   │ ws://127.0.0.1:8765/mcp │         │
   ▼                         │         │
mcp-gateway daemon ◄─────────┴─────────┘
   │ stdio     │ stdio     │ stdio
   ▼           ▼           ▼
 github      slack      filesystem
```

## Desktop app

`desktop/` builds **MCP-Gateway.app**: the daemon, its backends and the admin UI in one
window, with no terminal and no URL to copy. The app generates the access key itself at every
launch, hands it to the daemon through the environment, and opens the sockets from Rust — so
there is no key to paste, and none written down anywhere.

```bash
make tauri-python     # the bundled interpreter, once
make tauri-bundle     # desktop/src-tauri/target/release/bundle/macos/
```

macOS, unsigned, and a first cut. See [`desktop/README.md`](desktop/README.md).

## Why a daemon

Backends are a **single shared pool**. Three clients attached means one copy of each backend
— one `npx` cold start total, and one live state that every client and the admin UI observe
identically. Rotating a credential restarts only the backend that uses it, while everything
else keeps its process and its in-flight work.

## Setup

```bash
make venv
cp gateway.env.example gateway.env && chmod 600 gateway.env
$EDITOR gateway.env servers.yaml
make check          # validates both files; binds nothing, spawns nothing
```

`servers.yaml` is committed and holds **no secret values** — only `${NAME}` references:

```yaml
servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
```

`gateway.env` is gitignored and holds the values:

```bash
GITHUB_TOKEN=ghp_...
WS_ACCESS_KEY=...
```

To see exactly what will be launched, without opening the credential store:

```bash
mcp-gateway --list    # commands, cwd, and env KEY NAMES. Values are never printed.
```

## Running

```bash
make run
```

The banner prints the `ws://` URL and the matching `claude mcp add` line, to stderr. Then:

```bash
claude mcp add gateway -- mcp-gateway-connect --url ws://127.0.0.1:8765/mcp
```

`mcp-gateway-connect` is a thin stdio↔WebSocket pump: it lets today's stdio-only clients
attach to the daemon unchanged. It reconnects on its own, so restarting the daemon does not
make your client mark the server dead.

### Or attach over HTTP, with no bridge

The same port also speaks MCP's Streamable HTTP transport, so a client that supports it
natively needs nothing in between:

```bash
claude mcp add --transport http gateway http://127.0.0.1:8765/mcp
```

Same endpoint, same access key — `?key=…` or `Authorization: Bearer …`, whichever your
client can send. `POST` carries requests and gets its answers back directly; `GET` opens the
stream the daemon pushes notifications down, which is how `resources/updated`, log messages
and `list_changed` reach you. Both transports can be attached at once, and a session is
identified by the `Mcp-Session-Id` the daemon returns from `initialize`.

See [`transport_http.md`](src/mcp_gateway/transport_http.md) for the shape of it, and for
why there is no web framework underneath.

## Using it

Tools, prompts and resources are namespaced by backend:

```
github__create_issue        filesystem__read_file
github__list_prs            mcpgw://docs/file%3A%2F%2F%2FREADME.md
```

The gateway also exposes four tools of its own, so a model can explain itself:

| | |
|---|---|
| `gateway__list_backends` | every configured backend, running or not, and why not |
| `gateway__backend_health` | uptime, restarts, last error, and a live ping round-trip |
| `gateway__restart_backend` | stop and respawn one, re-reading its credentials |
| `gateway__reload_config` | re-read both files and apply the difference |

A call to a backend that is down comes back as readable content, not a protocol error — so
the model can tell you "GitHub is not configured" instead of failing the turn.

## The UI

The daemon serves its own admin and test console at **`http://127.0.0.1:8765/ui`**. `make
run` prints the URL, with the access key attached when there is one.

```
┌ Backends ────────┬ Tools · Prompts · Resources ─┬ Results ─────────┐
│ ● github         │ create_issue                 │ what came back,  │
│ ● filesystem     │   title    [            ]    │ rendered, with   │
│ ○ slack          │   body     [            ]    │ the raw payload  │
│   SLACK_BOT_TOKEN│   labels   [+ Add item ]     │ one click away   │
│   missing        │            [ Call tool ]     │                  │
└──────────────────┴──────────────────────────────┴──────────────────┘
```

Three columns: what is configured and running, what the selected backend publishes, and what
came back from invoking it.

- **The forms are generated from each tool's own `inputSchema`** — typed inputs, enum
  dropdowns, required markers, bounds and array editors. A schema using `if`/`then`/`else`,
  `allOf`, `dependentSchemas` or a discriminated `oneOf` gets a raw JSON box and a reason
  instead: a form that is confidently wrong is worse than the text box it should have fallen
  back to.
- **The middle and right columns speak MCP**, through a real handshake on `/mcp`. What you
  exercise in the UI is byte-identical to what the model gets.
- **Editing `servers.yaml` happens on `/admin`**, which the model cannot reach. Comments in
  the file survive the edit, an invalid edit writes nothing at all, and a `.bak` is left
  beside it.
- **Nothing in the UI can read or write a credential.** When a backend is missing one, the UI
  names the key — `SLACK_BOT_TOKEN`, say — and you add the line to `gateway.env` yourself.

To try it without configuring a real integration first:

```bash
make run-dev        # starts examples/zoo_server.py, then open the URL it prints
```

That is a local MCP server whose only purpose is to be rendered: thirteen tools covering
every JSON Schema construct a form can meet, five prompts, eight resources and four URI
templates. One of those prompts, `zoo-prompt-animal`, takes an `id` from the `zoo://animals`
listing and expands to a brief filled with that animal — which is what the UI's Variables
column is for.

It also publishes a **cascade**, for the case where one parameter decides what the next may
be: `zoo://continents` narrows to the countries of one continent, which narrow to the animals
of one country, which read through the same `zoo://animals/{id}`. The zoo answers that both
ways — as chained listing resources the Variables column walks, and as `completion/complete`
with `context.arguments`, which the gateway forwards and the UI offers as suggestions in the
field you are typing in.

## Rotating a credential

```bash
$EDITOR gateway.env
kill -HUP $(pgrep -f mcp-gateway)      # or call gateway__reload_config
```

Only the backend whose resolved environment actually changed is restarted. Everything else
keeps its process, and every attached client stays connected.

If either file fails to parse, **nothing changes at all** — you get the parse error and the
daemon carries on as it was.

## Security

- Each backend's environment is built **from nothing**: a small allowlist of
  process-shaping variables, plus that server's own `env_passthrough` and `env`. No backend
  sees another's credential, or yours.
- `${VAR}` resolves from `gateway.env` only, never from the ambient environment.
- Interpolation is refused in `command` and `args`: argv is world-readable through `ps`.
- Every known secret value is scrubbed from every log record, at the root logger — so a
  backend that prints its own token to stderr is covered too.
- Binding anything but loopback without an access key is refused.
- A WebSocket whose `Origin` header names anywhere but this server is refused. WebSocket has
  no same-origin policy, so any page you visit could otherwise open a socket to
  `127.0.0.1:8765`; non-browser clients send no `Origin` and are unaffected.
- No admin method returns a credential value, and nothing anywhere can write one. The UI
  edits `servers.yaml`, never `gateway.env`.

## Running it in the background

`make run` is foreground. For a persistent daemon, adapt the shipped launchd job:

```bash
cp scripts/com.dbuschman7.mcp-gateway.plist ~/Library/LaunchAgents/
$EDITOR ~/Library/LaunchAgents/com.dbuschman7.mcp-gateway.plist   # fix the paths
launchctl load ~/Library/LaunchAgents/com.dbuschman7.mcp-gateway.plist
```

launchd owns restart and log rotation; the daemon does not daemonize itself.

## Make targets

| | |
|---|---|
| `make venv` / `sync` | provision the repo-local venv (stamped: re-running is free) |
| `make lint` / `docs-check` / `test` / `build` | the CI gate, in that order |
| `make check` | validate `servers.yaml` + `gateway.env`, bind nothing |
| `make run` | start the daemon |
| `make run-dev` | start it against `servers.dev.yaml`: the schema zoo, for the UI |
| `make connect` | run the bridge in the foreground |
| `make container-image` / `package` | build and export the container image |
| `make clean` | build outputs and caches — **leaves the venv alone** |

Behind a TLS-intercepting proxy, pass `PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"`.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the map. Every module has a sibling `.md` beside
it carrying the reasoning, and `make docs-check` fails if one goes missing.

## License

Apache-2.0. See [LICENSE](LICENSE).
