# Get started

A walkthrough, from an empty checkout to a gateway your client and your model are both
attached to — and then to making **your own** MCP server light up the admin UI's
*Injectable values* column.

[README.md](README.md) is the pitch and the reference; [ARCHITECTURE.md](ARCHITECTURE.md)
is the map. This is the path through them.

1. [Pick a path](#pick-a-path)
2. [Install and first run](#install-and-first-run)
3. [Take the tour, with no credentials](#take-the-tour-with-no-credentials)
4. [Configure your own MCP servers](#configure-your-own-mcp-servers)
5. [Attach a client](#attach-a-client)
6. [Working in the admin UI](#working-in-the-admin-ui)
7. [Injectable values, in your own server](#injectable-values-in-your-own-server)
8. [Day two: rotating, reloading, running in the background](#day-two)
9. [When something is wrong](#when-something-is-wrong)

## Pick a path

| | |
|---|---|
| **The desktop app** | Everything in one window: no terminal, no URL, no access key to paste. Download a bundle, or build one — [`src/desktop/README.md`](src/desktop/README.md). Start here if you only want to *use* the gateway. |
| **The daemon, from a terminal** | `make run`, a browser for the UI, and a client attached over WebSocket or HTTP. This is what the rest of this document walks through, and it is also what the desktop app runs inside itself. |

Either way the daemon, the configuration files and the UI are the same, and both mint an
access key rather than asking you for one. The difference is only what carries it: the app
hands the key to a socket it opens itself, and the terminal prints it in the banner.

## Install and first run

You need Python 3.11+ and `make`. Node is needed only by the backends that happen to be
`npx` packages, and by the optional JavaScript test suite.

```bash
git clone <this repo> && cd python-mcp-gateway
make venv                                          # repo-local .venv, stamped: re-running is free
cp gateway.env.example gateway.env && chmod 600 gateway.env
```

Two files run everything, and the split between them is the point of the project:

| file | committed? | holds |
|---|---|---|
| `servers.yaml` | **yes** | what to launch, and `${NAME}` *references* to credentials |
| `gateway.env` | **no** — gitignored | the credential values, and the daemon's own access key |

The daemon wants an access key, even on loopback: without one, any other account on the
machine can open a socket and reach every backend you have configured. There are two ways it
gets one, and knowing which is which saves an afternoon:

| how you start it | the key |
|---|---|
| `make run` | **minted fresh for that run** and exported as `MCP_GATEWAY_WS_KEY`. The banner prints every URL with it already attached, so there is nothing to set up and nothing to paste. `make run NO_KEY=1` turns it off — and `make run-dev` does exactly that, since the zoo holds no credential. |
| `mcp-gateway` directly — launchd, a container, the desktop app | `MCP_GATEWAY_WS_KEY` from the environment if set, else `WS_ACCESS_KEY` from `gateway.env`. This is the one to fill in for anything long-lived. |

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
$EDITOR gateway.env        # WS_ACCESS_KEY=<that>
```

Then check your work without binding a port or spawning anything:

```bash
make check      # validates servers.yaml + gateway.env; reports every problem, not the first
```

Behind a TLS-intercepting proxy, `make venv` and `make build` need
`PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"`.

## Take the tour, with no credentials

Before wiring up a real integration, run the gateway against the schema zoo — a local MCP
server whose only job is to be rendered:

```bash
make run-dev
```

It prints an admin UI URL, with no key on it: `run-dev` defaults to `NO_KEY=1`, because
there is nothing behind this socket worth locking. Open it, and you have one backend, `zoo`,
publishing thirteen tools covering every JSON Schema construct a form can meet — including
the four it is *meant* to decline — plus five prompts, eight resources and four URI
templates. It touches no network and holds no credential, so nothing you click there can
cost you anything.

The zoo is also the reference implementation for the last section of this document: it is
what a server looks like when it publishes its own vocabularies. See
[`examples/zoo_server.py`](examples/zoo_server.py).

## Configure your own MCP servers

### The file

`servers.yaml` is a catalogue of launch specs. A minimal entry is a command and its
arguments:

```yaml
version: 1

defaults:
  timeout: 30.0            # per-request seconds
  startup_timeout: 20.0    # spawn + initialize + first listing
  env_mode: curated        # build each child's environment from nothing

servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/src"]
    description: "Read and write files under ~/src"
```

A server that needs a credential names it, and never inlines it:

```yaml
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
```

```bash
# gateway.env
GITHUB_TOKEN=ghp_...
```

Three rules are worth knowing before you hit them:

- **`${VAR}` resolves from `gateway.env` only** — never from the ambient environment. A
  variable that happens to be exported in your shell is not visible to a backend.
- **`${VAR}` is refused in `command` and `args`**, with an error saying why: argv is
  world-readable through `ps`, so a secret on a command line publishes itself at the moment
  it is protecting something. Credentials go in `env`, which is private to the child.
- **`env_mode: curated` (the default) means a backend inherits nothing** but a small
  allowlist of process-shaping variables plus its own `env` and `env_passthrough`. If your
  server needs `PATH`, `HOME`, `HTTPS_PROXY` or `NODE_EXTRA_CA_CERTS`, name them:

  ```yaml
      env_passthrough: ["PATH", "HTTPS_PROXY", "NODE_EXTRA_CA_CERTS"]
  ```

Other per-server keys: `enabled` (spawn it at all), `required` (a failure here is fatal to
the daemon rather than skipped), `cwd`, `lazy`, `idle_ttl`, `timeout`, `startup_timeout`.
The full schema is in [`config.md`](src/mcp_gateway/config.md).

### A server that is already running

Not every MCP server is a process for the gateway to start. One that serves MCP over HTTP,
such as a container on its own port or a service somebody else runs, gets a `url` instead
of a `command`:

```yaml
  search:
    url: http://127.0.0.1:9001/mcp
    headers:
      Authorization: "Bearer ${SEARCH_TOKEN}"
```

The gateway speaks MCP's Streamable HTTP to it, and to a client it looks like any other
backend: `search__*` tools, the same status, the same failure message. `headers` does for
a URL what `env` does for a process. Values come from `gateway.env`, only the header
names are ever shown, and each header goes to this server and no other. The keys that
describe a process (`args`, `env`, `env_passthrough`, `env_mode`, `cwd`) are refused on a
`url` entry, and so is a `${VAR}` or a `user:password@` in the URL itself, because the URL
is shown by `--list`, the admin UI and every log line about the connection.

If the server restarts and forgets the gateway's session, the gateway starts a new one and
re-lists its tools. No restart is needed on this side.

Already have a Claude Desktop config? YAML 1.2 is a superset of JSON, so
`mcp-gateway --config claude_desktop_config.json` goes through the identical loader, with no
second parser and no conversion step.

### Or from the UI

`+ Add` in the header of the admin UI opens the same spec as a form — process, environment,
behaviour — and writes `servers.yaml` for you. Comments in the file survive the edit, an
invalid edit writes nothing at all, and a `.bak` is left beside it. The dialog can read and
write `${NAME}` references and nothing else: **nothing in the UI can read or write a
credential value.** That editing lives on `/admin`, which the model cannot reach.

### Check before you run

```bash
make check                 # both files, every problem, binds nothing
mcp-gateway --list         # the resolved spawn plan: commands, cwd, and env KEY NAMES only
```

`--list` never prints a value. It is the safe way to answer "what would actually launch, and
with which variables set" on a machine whose config you did not write.

## Attach a client

Start the daemon:

```bash
make run
```

The banner prints, to stderr, the `ws://` URL, the admin UI URL, and the `claude mcp add`
line — each with that run's key already attached. Copy them from there rather than
reconstructing them: the key is minted per run, so yesterday's pasted URL will not work
today. For a stable key, set `WS_ACCESS_KEY` in `gateway.env` and run `mcp-gateway`
directly, or export `MCP_GATEWAY_WS_KEY` before `make run`.

**Claude Code, over the stdio bridge:**

```bash
claude mcp add gateway -- mcp-gateway-connect --url 'ws://127.0.0.1:8765/mcp?key=…'
```

`mcp-gateway-connect` is a thin stdin↔WebSocket pump that lets a stdio-only client attach
to the daemon unchanged. It reconnects on its own, so restarting the daemon does not make
your client mark the server dead.

**Any client that speaks Streamable HTTP, with nothing in between:**

```bash
claude mcp add --transport http gateway 'http://127.0.0.1:8765/mcp?key=…'
```

Same endpoint, same key — `?key=…` or `Authorization: Bearer …`, whichever your client can
send. Both transports can be attached at once. See
[`transport_http.md`](src/mcp_gateway/transport_http.md).

**Claude Desktop**, whose config is JSON and whose transport is stdio:

```json
{
  "mcpServers": {
    "gateway": {
      "command": "mcp-gateway-connect",
      "args": ["--url", "ws://127.0.0.1:8765/mcp?key=…"]
    }
  }
}
```

Once attached, everything is namespaced by backend — `github__create_issue`,
`filesystem__read_file` — and the gateway adds five tools of its own so a model can explain
itself: `gateway__list_backends`, `gateway__backend_health`, `gateway__restart_backend`,
`gateway__reload_config` and `gateway__clipboard`.

## Working in the admin UI

`http://127.0.0.1:8765/ui`, on the same port as the sockets. The configured servers are
pills in the header — the indicator, the name, and a menu holding the description, the last
error, the credentials it wants, and Restart. Under them, three columns:

```
  ● github   ● filesystem   ○ slack (SLACK_BOT_TOKEN missing)      + Add
┌ Tools · Prompts · Resources · Templates ─┬ Results ────────┬ Injectable values ┐
│ create_issue                             │ what came back, │ id                │
│   title    [                    ]        │ rendered, with  │  ● axolotl        │
│   body     [                    ]        │ the raw payload │  ○ capybara       │
│   labels   [ + Add item ]                │ one click away  │  ○ pangolin       │
│            [ Call tool ]                 │                 │  [Clear]          │
└──────────────────────────────────────────┴─────────────────┴───────────────────┘
```

- **The forms are generated from each tool's own `inputSchema`** — typed inputs, enum
  dropdowns, required markers, bounds, array editors. A schema using `if`/`then`/`else`,
  `allOf`, `dependentSchemas` or a discriminated `oneOf` gets a raw JSON box and a stated
  reason instead: a form that is confidently wrong is worse than the text box it should have
  fallen back to.
- **All three columns speak MCP**, through a real handshake on `/mcp` — the listings, the
  calls, and the reads behind Injectable values alike. What you exercise here is
  byte-identical to what the model gets; that is the whole value of a test bench, and it is
  why the columns do not ask `/admin` for a listing. `/admin` answers what is *configured*,
  which is the header and the footer.
- **Clipboard**, in the Results header, renders the whole bench session — every call with
  its arguments and its answer, then every value the right-hand column is offering and which
  are picked — as one Markdown document you can trim and copy. The daemon renders it, not the
  page, and `gateway__clipboard` on `/mcp` serves the same text, so a model can read what you
  just did without anyone pasting anything. See [`clipboard.md`](src/mcp_gateway/clipboard.md).
- **The log drawer** at the foot carries the daemon's own records, at a level you choose.
  Every known secret value is scrubbed from every record at the root logger — so a backend
  that prints its own token to stderr is covered too.

## Injectable values, in your own server

The right-hand column is empty for most servers, and that is not a bug in them: it activates
only when a server publishes the pair it needs. Getting your own server to fill it is a
small, purely additive change — no gateway-specific protocol, no extension to negotiate.

### What the column is

A server that publishes a **template** like `zoo://animals/{id}` usually publishes a
**listing** beside it at `zoo://animals`: the set of values `id` may take, small enough to
send whole. That pair is a *vocabulary*, and the column is those vocabularies — one group
per variable, every value a button that fills the field it belongs in.

The values are not decoration. In `many` mode a group sends the open form **once per value**,
so six animals is six real tool calls; two groups picked at once multiply.

### The pairing rule

**A template's fixed prefix, up to its first `{`, must be the listing's URI.**

| template | pairs with | why |
|---|---|---|
| `zoo://animals/{id}` | `zoo://animals` | prefix, trailing separator trimmed |
| `zoo://echo/{word}` | — nothing | no resource is published at `zoo://echo` |

The comparison is on your server's own spelling, after the gateway's
`mcpgw://<server>/<percent-encoded>` wrapper is unwound — so you write ordinary URIs and
think about nothing else.

This rule is also a guarantee to your users: **a resource that pairs with no template is
never read.** `resources/read` is a live call that may move state, and the column will not
go fishing through your resource space looking for something that might be a set.

### Step 1 — publish the listing and the template

From `resources/list`:

```json
{ "uri": "zoo://animals", "name": "zoo-animals",
  "description": "The known animals", "mimeType": "application/json" }
```

From `resources/templates/list`:

```json
{ "uriTemplate": "zoo://animals/{id}", "name": "zoo-animal-template",
  "description": "Details for one animal; `id` comes from zoo://animals",
  "mimeType": "application/json" }
```

### Step 2 — make the listing's body machine-readable

**JSON only.** A vocabulary has to be machine-readable to be one, and splitting prose on
newlines would turn every text resource into a list of garbage values. Three body shapes are
understood, and any of the three is enough to activate the column:

| body | values are | labels come from | may also name |
|---|---|---|---|
| a JSON Schema enum fragment | `enum` | `enumNames` | `readOne`, `narrows` |
| an array of scalars, or of records | the scalar, or `id` / `value` / `uri` / `name` | `title` / `name` | |
| an object keyed by identifier | the keys | each record's `name` / `title` | |

The first is the one worth publishing, because it is the only one that carries labels *and*
names its own template. `resources/read` on `zoo://animals` returns one text content whose
body is:

```json
{
  "type": "string",
  "enum": ["axolotl", "capybara", "pangolin"],
  "enumNames": ["Axolotl", "Capybara", "Pangolin"],
  "description": "The animals this zoo knows about.",
  "readOne": "zoo://animals/{id}"
}
```

`readOne` is **not** a JSON Schema keyword. It is the listing saying where one of its values
is spent — and where it disagrees with the pairing found by prefix, it wins, because you know
where your values go better than your URIs do. It does not replace the prefix rule: that is
still what makes the listing *discoverable* as a vocabulary in the first place. What
`readOne` settles is which template the values are spent on, and therefore which variable the
group is named for. `enumNames` is the same kind of addition: show the name, send the
value.

At this point the column works. A user selects your server, picks `id` from the menu at the
top of the column, and every value is a chip that fills any field named `id` — in a tool
form, in a prompt's arguments, or in the template's own variable.

### Step 3 (optional) — a parameter that decides the next one

Some parameters are not independent: which countries there are depends on the continent, so
there is no single `zoo://countries` to publish, and a listing that took a parameter would be
a template, which pairs with nothing.

So a listing names its own child, in the body, beside `readOne`:

| key | one of my values buys | example |
|---|---|---|
| `readOne` | a **member** | `zoo://animals/{id}` |
| `narrows` | another **listing** | `zoo://continents/{continent}/countries` |

A three-level cascade is then three listings, each naming what one of its values buys:

```
zoo://continents                                     narrows → …/{continent}/countries
zoo://continents/africa/countries                    narrows → …/{country}/animals
zoo://continents/africa/countries/congo/animals      readOne → zoo://animals/{id}
```

The invariant that makes this safe is the same one as before, all the way down: a root is
read because a template's fixed prefix named it, and **every deeper listing is read at a URI
your server itself produced**, expanded from the parent's `narrows` with everything already
picked. Nothing is ever guessed.

Two consequences for your implementation:

- **A narrowing group picks exactly one value.** Merging two continents' countries would
  invent a listing you never published, so the UI removes the option rather than papering
  over it. The leaf keeps the `one | many` fan-out switch.
- **Answer a URI you never handed over with an error, not an empty list.** The zoo returns
  `-32002` for a country that is not in the continent named in the URI. An empty listing
  would read as "this country has no animals", which is a different and wrong fact.

### Step 4 (optional) — suggestions in the field itself

`completion/complete` is the protocol's own answer to the same question, for a different
moment: the column is how a person *browses* a vocabulary; a completion is how a field gets
filled *while someone is typing in it*. Implement it and the gateway forwards it, and the UI
offers the answers as a `<datalist>` on template variables and prompt arguments.

MCP defines a ref for a prompt and for a resource template and none for a tool argument, so
tool forms get no suggestions — that looks like an oversight in the UI and is not one.

Read `context.arguments` when you answer: the other fields of the same form are sent along,
so a `country` request arrives carrying the `continent` already filled in, and you can narrow
on it. A pick in the column arrives as a real `input` event, so this cascades with no extra
wiring on either side.

### Checklist

- [ ] The template is in `resources/templates/list`, the listing in `resources/list`.
- [ ] The listing's URI is exactly the template's text before its first `{`, minus any
      trailing `/`, `#`, `?` or `&`.
- [ ] `resources/read` on the listing returns a **text** content whose body parses as JSON
      in one of the three shapes above.
- [ ] The body names `readOne` (one of my values buys a member) or `narrows` (one of my
      values buys another listing), so which template the values are spent on is stated
      rather than inferred. A cascade past the first level needs `narrows`; there is no other
      way to publish one.
- [ ] The variable in the template has the same name as the field it should fill —
      `{id}` fills an `id` argument. Matching loosens in steps (exact, then case- and
      separator-insensitive, then a field whose name *ends* in it, like `animalId`), and
      stops there.
- [ ] Reading the listing is cheap and side-effect-free. It is read on open, and again on
      Reread or a `list_changed`.

### Trying it against the zoo

`make run-dev` publishes all of the above: the flat `zoo://animals` pair, the three-level
continent cascade, `completion/complete` with `context.arguments`, and a prompt
(`zoo-prompt-animal`) whose `id` argument the same picks fill — so six picks in `many` mode
expand six briefs. [`examples/zoo_server.py`](examples/zoo_server.py) is 1,900 lines of
fixture with the reasoning written beside each part; the UI's side of the contract is in
[`webui.md`](src/mcp_gateway/webui.md).

## Day two

### Rotating a credential

```bash
$EDITOR gateway.env
kill -HUP $(pgrep -f mcp-gateway)      # or call gateway__reload_config
```

Only the backend whose **resolved** environment actually changed is restarted. Everything
else keeps its process and its in-flight work, and every attached client stays connected. If
either file fails to parse, nothing changes at all — you get the parse error and the daemon
carries on as it was.

### Keeping the credentials somewhere else

`gateway.env` is the default, not the only option. If your credentials live in Vault, in
AWS Secrets Manager, or behind something internal, write a Python file and point the
catalogue at it:

```yaml
# servers.yaml
secrets:
  providers:
    - provider: "./providers/vault.py:VaultProvider"
      options:
        addr: "https://vault.internal:8200"
        mount: "kv/mcp-gateway"
```

A provider is any object with a `load` method:

```python
# ./providers/vault.py
class VaultProvider:
    def __init__(self, addr, mount):
        self._client = hvac.Client(url=addr)
        self._mount = mount

    def load(self, request):
        # request.keys is exactly the ${VAR} names servers.yaml references.
        return {key: self._fetch(key) for key in request.keys if self._has(key)}
```

Providers are asked in order and **`gateway.env` is always asked last**, so you do not have
to move anything that already works — the usual setup is backend tokens from the provider
and `WS_ACCESS_KEY` left in the file. Return only the keys you have; a key you omit falls
through to the next provider, and then to the file. Raise if your store is unreachable, so
the daemon refuses to start rather than handing every backend an empty token.

`mcp-gateway --check` runs the chain and prints what each source supplied:

```
secrets: ./providers/vault.py:VaultProvider (4 key(s)) -> /etc/mcp-gateway/gateway.env (1 key(s))
```

There is a runnable provider with no third-party dependency in
[`examples/vault_provider.py`](examples/vault_provider.py), and the reasoning behind the
interface — in particular why it hands back everything at once instead of resolving one key
at a time — is in
[`src/mcp_gateway/secret_providers.md`](src/mcp_gateway/secret_providers.md).

Editing a provider *file* needs a restart, not a reload: a loaded provider is cached so a
reload does not drop a pooled connection.

### Adding a server while running

Edit `servers.yaml` (or use `+ Add` in the UI), then reload the same way. The catalogue
changes and a `list_changed` goes out to every attached client; the eleven backends you did
not touch never notice.

### Putting your own name on it

For an internal deployment that is not "the MCP gateway" to the people using it. The block
is already in `servers.yaml`, filled in with the stock values — edit it:

```yaml
# servers.yaml
branding:
  title: "Acme Internal Tools"
  name: acme-tools
```

Reload, and the browser tab, the page header and the desktop window all wear the new name;
MCP clients see `acme-tools` in `serverInfo` instead of `mcp-gateway`.

The mark is one file, `src/mcp_gateway_ui/logo.svg` — the header image and the favicon, in
both the browser and the desktop app. In a checkout, replace it. Anywhere else — a pip
install, a container — point the config at your own file instead, which is also the way to
keep your logo out of the package:

```yaml
branding:
  title: "Acme Internal Tools"
  name: acme-tools
  icon: ./brand/acme.svg    # relative to servers.yaml
```

That path must be an SVG, PNG, ICO, WEBP, JPEG or GIF under 128 KiB; the daemon reads it and
inlines it into the `/admin` payload, which is how the desktop window — which cannot fetch a
URL — gets the same picture the browser does. A path that does not resolve is a startup
refusal naming the file, so `make check` catches it before anything binds.

Tool names do not change: a backend is still `github__create_issue`, in every deployment.

The desktop bundle's *own* name and icon are baked in before any config file exists, so they
are a build step rather than a reload:

```bash
make tauri-brand ARGS='--icon brand/acme-1024.png'   # then: make tauri-bundle
make tauri-brand ARGS=--clear                        # back to stock
```

That writes a gitignored overlay beside `tauri.conf.json`; the committed config, and the
tests that pin it, are untouched. See [`branding.md`](src/mcp_gateway/branding.md).

### Running the gateway on another machine

The daemon does not have to be on the machine you are looking at. Run it on the Linux box
you already keep things on, and reach it from your laptop through an SSH port forward: the
backends, their processes and every credential stay over there, and what crosses is the same
two WebSockets a local gateway answers on.

On that machine, bind loopback and set no access key:

```bash
make run NO_KEY=1          # 127.0.0.1:8765, no key
curl http://127.0.0.1:8765/ui/   # from that machine, to prove it is up
```

**Do not bind anything but `127.0.0.1` for this.** SSH is what carries the connection and
SSH is what authenticates it; a LAN bind would need an access key and would be a second,
weaker door into the same credentials. The daemon refuses an unauthenticated non-loopback
bind for exactly that reason.

Then, from your laptop:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@build-box
```

That is the whole mechanism. With the forward up, everything on the laptop attaches exactly
as it would to a local gateway — `http://127.0.0.1:8765/ui/` in a browser, and

```bash
claude mcp add gateway -- mcp-gateway-connect --url ws://127.0.0.1:8765/mcp
```

for a client — with no key, because the daemon over there has none.

The desktop app runs that same forward for you, from its **Connection** screen: pick *On
another machine, over SSH*, give it the destination and the two ports, and it opens the
tunnel, reopens it when the network drops, and says which of the two ends is at fault when
it cannot. It never answers an SSH prompt on your behalf, so make sure `ssh build-box`
works in a terminal first — key in the agent, host key accepted. And it never starts or
stops the daemon over there: that is yours.

Forward to the *same* port number at both ends unless something on the laptop already has
it. A mismatched forward works for the desktop app and for `mcp-gateway-connect`, but a
**browser** at the forwarded port is refused: the daemon's `Origin` allowlist names the port
it bound, and it cannot know what a tunnel did with it. `MCP_GATEWAY_WS_ALLOWED_ORIGINS` on
the remote is the way out if you need one.

### The far end in a container

The same arrangement works with the daemon in a container on the Linux box. There, most MCP
servers will be containers too, reached by `url` rather than spawned. The one thing to get
right is the network. Remote mode means a keyless daemon on the box's own loopback, and
only **host networking** puts a container there:

```bash
docker run -d --name mcp-gateway --network host --restart unless-stopped \
  -e MCP_GATEWAY_HOST=127.0.0.1 \
  -v ~/.config/mcp-gateway:/config \
  python-mcp-gateway:local
```

`-p 127.0.0.1:8765:8765` on a bridge network looks equivalent and is not. The daemon would
have to bind `0.0.0.0` inside its container to be reachable through the published port, it
refuses to do that without an access key, and the desktop app sends no key in remote mode.
Host networking is Linux-only. Docker Desktop's version of it is not the host's network.

The image reads `/config/servers.yaml`, with `gateway.env` beside it. Mount the
**directory**, writable: saving from the admin UI renames a new file over the old one,
which a single bind-mounted file cannot take. Get the image onto the box with
`make container-image TARGET=you@box` from the laptop, which builds for the box's
architecture and loads the image there, or by running
`docker build -f Containerfile -t python-mcp-gateway:local .` in a checkout on the box.

Each backend container publishes its port on `127.0.0.1` only, and the gateway reaches it
at `url: http://127.0.0.1:<port>/mcp`. Under host networking that address is the box
itself. [`examples/remote-compose/`](examples/remote-compose/compose.yaml) is the whole
thing as a compose file, with a catalogue to start from, and its
[README](examples/remote-compose/README.md) is the step-by-step standup: getting the image
onto the box, adding server containers, checking, starting, connecting from the laptop,
and what to do on day two.

The image carries Python and nothing else. A `command:` backend that needs `npx` or `uvx`
needs an image built `FROM` this one that adds it.

### Serving the LAN directly, over TLS

SSH is the better answer when there is one person and one laptop. When there is not — a
team, or a client that cannot run a tunnel — bind the LAN with an access key **and** a
certificate, so the key does not cross the network readable:

```bash
MCP_GATEWAY_WS_KEY=… mcp-gateway --host 0.0.0.0 \
  --tls-cert /etc/mcp-gateway/cert.pem --tls-key /etc/mcp-gateway/key.pem
```

The port then speaks `wss://` and `https://` only; the UI is at `https://<host>:8765/ui/`.
`mcp-gateway --check --tls-cert … --tls-key …` loads the pair without binding anything, and
catches the key that no longer matches its renewed certificate. Both paths can come from
`MCP_GATEWAY_TLS_CERT` and `MCP_GATEWAY_TLS_KEY` instead, for a container or unit file.

A client verifies the certificate as it would any other. For a self-signed one or a private
CA, give the bridge the bundle:

```bash
claude mcp add gateway -- mcp-gateway-connect --url wss://build-box:8765/mcp --ca-file ca.pem
```

A plaintext bind off loopback still starts — TLS terminated by a proxy in front is a normal
arrangement — but it logs a warning saying the key is crossing the network in the clear.

### Running in the background

`make run` is foreground and does not daemonize. For a persistent daemon on macOS, adapt the
shipped launchd job:

```bash
cp scripts/com.dbuschman7.mcp-gateway.plist ~/Library/LaunchAgents/
$EDITOR ~/Library/LaunchAgents/com.dbuschman7.mcp-gateway.plist   # fix the paths
launchctl load ~/Library/LaunchAgents/com.dbuschman7.mcp-gateway.plist
```

launchd owns restart and log rotation. There is also a `Containerfile` and
`make container-image` / `make package`; for the container as remote mode's far end, see
[above](#the-far-end-in-a-container).

On the Linux box, a **user** unit rather than a system one — the daemon holds every
credential in `gateway.env` and spawns the commands in `servers.yaml`, and running it as
yourself is the whole security model:

```ini
# ~/.config/systemd/user/mcp-gateway.service
[Unit]
Description=MCP Gateway
After=network.target

[Service]
ExecStart=%h/src/python-mcp-gateway/.venv/bin/mcp-gateway --config %h/.config/mcp-gateway/servers.yaml --host 127.0.0.1 --port 8765
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now mcp-gateway
loginctl enable-linger "$USER"     # or it stops with your last session
```

`loginctl enable-linger` is the line everyone forgets, and without it the tunnel from your
laptop connects to nothing the moment you log out.

### The gate you should not open casually

Binding anything but loopback with no access key is refused. If you bind a LAN address, set
`WS_ACCESS_KEY` and understand that every credential in `gateway.env` is reachable by
anything that has it.

## When something is wrong

| symptom | what it usually is |
|---|---|
| A server's pill is hollow and its menu names a missing credential | `gateway.env` has no line for the `${NAME}` that server's `env` block references. The UI names the key; you add the line. Nothing in the UI writes `gateway.env`. |
| `command not found` for an `npx`/`python3` backend | `env_mode: curated` gave the child no `PATH`. Add `env_passthrough: ["PATH"]`. |
| A tool call answers "GitHub is not configured" instead of failing | Working as designed: a call to a down backend comes back as readable content with `isError`, so the model can tell you rather than dying on a protocol error. `prompts/get`, `resources/read` and `completion/complete` have no such escape hatch and answer `-32603`, `-32002` and `-32603`. |
| The UI's gate says "Starting…" for a while | The daemon binds its socket only after every backend is up, so the first client finds a warm pool. A cold `npx` is the usual reason. |
| The browser cannot connect but the daemon is running | The access key. The UI URL needs `?key=…`; `make run` prints the whole URL with it attached. |
| The tunnel is open and both pills stay red | Nothing is listening on the far side. Start the daemon on that machine — `systemctl --user status mcp-gateway`, or `make run` in a checkout. The desktop app says this in so many words. |
| `ssh` in the desktop app fails with "Permission denied" | It runs `ssh` in batch mode and will never prompt you: no password, no passphrase, no host-key question. Run `ssh <destination>` once in a terminal, get it working there — key in the agent, host key accepted — and the app will work afterwards. |
| A browser at a forwarded port is refused but the desktop app is fine | The remote's `Origin` allowlist names the port *it* bound, not the one your forward landed on. Forward to the same number, or set `MCP_GATEWAY_WS_ALLOWED_ORIGINS` on the remote. |
| A WebSocket from a page is refused | Its `Origin` names somewhere other than this server. WebSocket has no same-origin policy, so any page you visit could otherwise dial `127.0.0.1:8765`. Non-browser clients send no `Origin` and are unaffected. |
| The Injectable values column says the server publishes no pairing | No template's fixed prefix matches a published resource URI. See the [checklist](#checklist). |
| A vocabulary group says the listing is not JSON | The listing's body must parse as JSON in one of three shapes. Prose is refused deliberately. |
| An edit through `+ Add` did nothing | An invalid edit writes nothing at all, by design. The dialog reports why, and a `.bak` of the last good file is beside `servers.yaml`. |

## Where to go next

- [README.md](README.md) — the pitch, and the reference for flags and make targets
- [ARCHITECTURE.md](ARCHITECTURE.md) — the module map, startup order, and the two things the
  design turns on
- [`src/desktop/README.md`](src/desktop/README.md) — the Tauri shell, and why its host owns the
  sockets
- [`webui.md`](src/mcp_gateway/webui.md) — the admin UI in full, including every rule the
  Injectable values column follows
- [`config.md`](src/mcp_gateway/config.md) and [`secrets.md`](src/mcp_gateway/secrets.md) —
  the two file grammars
- [CHANGELOG.md](CHANGELOG.md) — what changed, and what each release was *for*

Every module under `src/mcp_gateway/` has a sibling `.md` carrying its reasoning, and
`make docs-check` fails if one goes missing or a relative link stops resolving.
