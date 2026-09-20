# Standing up the gateway in a container on a Linux box

This runbook sets up the far end of remote mode in Docker. The gateway and every MCP
server run on the Linux box. The desktop app, or any MCP client, stays on your laptop and
reaches the gateway through an SSH port forward. [GET_STARTED.md](../../GET_STARTED.md#the-far-end-in-a-container)
explains why it has this shape. This page covers only the order of the steps.

```
laptop                          Linux box
------                          ---------
desktop app / claude  --ssh-->  127.0.0.1:8765  gateway container (host network, no key)
                                     |  url: http://127.0.0.1:<port>/mcp
                                127.0.0.1:<port> one container per MCP server
```

Nothing on the laptop holds a secret. Every credential is in `gateway.env` on the box, and
SSH does the authenticating.

## 0. Before you start

On the **Linux box**:

- Docker Engine with the compose plugin (`docker compose version` prints a version).
- Port `8765` free on loopback (`ss -ltn | grep 8765` prints nothing).

On the **laptop**:

- `ssh you@box` works in a terminal **with no prompt**: the key is in the agent and the
  host key is accepted. The desktop app runs `ssh` in batch mode and never answers a
  prompt for you.
- Port `8765` is free too. Use the same number at both ends: a browser at a *different*
  forwarded port is refused by the gateway's `Origin` check.

## 1. Get the image onto the box

Either way, the image ends up tagged `python-mcp-gateway:local`, which is the name
`compose.yaml` expects.

**Build it on the box** (simplest, needs a checkout there):

```bash
git clone git@github.com:Digital-Dimentia/python-mcp-gateway.git
cd python-mcp-gateway
docker build -f Containerfile -t python-mcp-gateway:local .
```

**Or build it on the laptop and load it onto the box in one step:**

```bash
make container-image TARGET=you@box
```

This asks the box for its architecture (`ssh you@box uname -m`), builds the image for that
platform, and streams it into the box's `docker load` (or `podman load` if the box has
podman). The laptop's own architecture can't be used: an Apple-silicon Mac is `arm64` and
most Linux servers are `amd64`. When the two differ, the build runs under emulation, which
is slow but works. The same `ssh` rule as step 0 applies: no prompts. If the load fails,
the tar stays in `dist/` for copying by hand.

To do the same thing by hand, name the platform yourself and copy the tar:

```bash
make container-image PLATFORMS=linux/amd64      # linux/arm64 for a Pi or an ARM server
scp dist/python-mcp-gateway-container.tar you@box:
ssh you@box docker load -i python-mcp-gateway-container.tar
```

Check it on the box:

```bash
docker run --rm python-mcp-gateway:local --version
```

## 2. Lay out the config directory

The compose file mounts `./config` at `/config`. The gateway reads `servers.yaml` and
`gateway.env` from there, and the admin UI saves `servers.yaml` back into it, so the
directory must be writable.

```bash
mkdir -p ~/mcp-gateway && cd ~/mcp-gateway
cp -r /path/to/python-mcp-gateway/examples/remote-compose/. .   # compose.yaml + config/
cp config/gateway.env.example config/gateway.env
chmod 600 config/gateway.env
printf 'MCPGW_UID=%s\nMCPGW_GID=%s\n' "$(id -u)" "$(id -g)" > .env   # who the container writes as
```

That last line is worth one sentence. The container runs unprivileged, and `.env` tells it
*which* unprivileged account, so that a save from the admin UI leaves `config/servers.yaml`
owned by you and still editable by hand. Skip it and the container falls back to uid 1000,
which is right only if that happens to be you.

`gateway.env` holds values and `servers.yaml` holds `${NAME}` references to them. **Do not
set `WS_ACCESS_KEY`.** Remote mode is keyless, and a key makes the desktop app's connection
fail with a 401.

## 3. Add your MCP servers

Each server is a container in `compose.yaml` that **publishes its port on 127.0.0.1
only**, with a matching `url:` entry in `config/servers.yaml`:

```yaml
# compose.yaml
  search:
    image: your-registry/search-mcp:latest
    ports:
      - "127.0.0.1:9001:8000"    # box port : container port
    restart: unless-stopped
```

```yaml
# config/servers.yaml
servers:
  search:
    url: http://127.0.0.1:9001/mcp     # the box port, and the server's MCP path
    headers:
      Authorization: "Bearer ${SEARCH_TOKEN}"
```

```bash
# config/gateway.env
SEARCH_TOKEN=...
```

- Use a different box port for each server.
- Take the path (`/mcp` is common) from the server's own documentation.
- A server that does not need a credential gets no `headers:` block.
- Never publish a server on `0.0.0.0` or leave out `127.0.0.1:`. That puts it on the LAN,
  where anyone can reach it without going through the gateway, and all the credential
  handling in `gateway.env` is bypassed.

The example `search` entry ships with `enabled: false`. Remove that line once the
container exists.

## 4. Check, then start

```bash
docker compose run --rm gateway --check     # validates both files; binds and connects nothing
docker compose up -d
docker compose logs -f gateway              # each backend should log "running (http://...)"
```

`--check` names any `${NAME}` that `gateway.env` is missing. Fix those before starting.
Otherwise the backend that needs the value will not start.

Then, on the box:

```bash
curl -fsS -o /dev/null http://127.0.0.1:8765/ui/ && echo up
```

## 4a. Check it end to end, before you have a server of your own

Step 4 proves the gateway is up. It does not prove a `url:` backend works, because at this
point there is nothing to reach. The schema zoo fills that gap: 14 tools, 6 prompts, 8
resources and every JSON Schema construct, served over Streamable HTTP by
[`../zoo_http.py`](../zoo_http.py) — a worked example of putting an stdio MCP server behind
a URL, which is what most of them still are.

It runs from the same image, so there is nothing extra to build. Copy the two files it
needs and start it under the `verify` profile:

```bash
mkdir -p zoo
cp /path/to/python-mcp-gateway/examples/zoo_http.py zoo/
cp /path/to/python-mcp-gateway/examples/zoo_server.py zoo/
cp /path/to/python-mcp-gateway/examples/panel_server.py zoo/
$EDITOR config/servers.yaml                  # delete `enabled: false` under `zoo` and `panel`
docker compose --profile verify up -d
docker compose logs gateway | tail -4        # both backends running (http://127.0.0.1:900…)
```

The line to wait for is `backend 'zoo' running (http://127.0.0.1:9001/mcp, MCP 2025-06-18)`,
and another like it for `panel`. One wrapper serves both: `zoo_http.py --server` names the
stdio program to put behind the URL, which is the general shape of the problem — most MCP
servers still speak stdio, and a container deployment reaches them by URL.

Then, in the admin UI:

- Call `zoo__zoo-types` with a number, and watch it come back as a number rather than a
  string. That round trip is where a transport that stringifies everything would show itself.
- Open `panel__board` and press **Call and open panel**. The card draws the backend's own
  control surface instead of the generic form. `panel__describe` returns the same data with
  no panel, which is the comparison: one server, two renderings, one `_meta` line between
  them.

When you are done, `docker compose --profile verify down` stops both and leaves the gateway
running. Set `zoo` and `panel` back to `enabled: false` so the gateway stops dialling ports
with nothing on them.

## 5. Connect from the laptop

**Desktop app:** open **Connection**, choose *On another machine, over SSH*, and enter
`you@box` (or a `Host` alias from `~/.ssh/config`) with `8765` for both ports. The app opens
the tunnel and reopens it if it drops. The pills turning green means the gateway answered
through it.

**Any other client**, with the tunnel opened by hand:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@box
claude mcp add gateway -- mcp-gateway-connect --url ws://127.0.0.1:8765/mcp
```

The admin UI is then at `http://127.0.0.1:8765/ui/` in a browser on the laptop.

## Day two

| To | Do |
|---|---|
| Pick up a hand edit to `servers.yaml` or `gateway.env` | `docker compose kill -s HUP gateway`. SIGHUP reloads and restarts only the backends whose config or credential changed. Saving from the admin UI reloads by itself. |
| Rotate a credential | Edit `config/gateway.env`, then send SIGHUP as above. |
| Add a server | Add its container and entry (step 3), run `docker compose up -d`, then send SIGHUP. |
| Upgrade the gateway | Rebuild or reload the image (step 1), then run `docker compose up -d gateway`. |
| Restart a backend container | Nothing to do on the gateway's side: it starts a new session and re-lists that server's tools. |
| See what it is doing | `docker compose logs -f gateway`. For every MCP message as well, add `command: ["--debug"]` to the service and recreate it. |

## When it does not work

| Symptom | Usually |
|---|---|
| Container exits with `refusing to bind … without an access key` | `MCP_GATEWAY_HOST` is not `127.0.0.1`, so the image's default `0.0.0.0` is in force. Check the `environment:` block. |
| `no such catalogue` | `./config/servers.yaml` is missing, or compose ran from a directory without `./config`. |
| Desktop app: tunnel open, pills stay red | The gateway is not running or not on loopback. Check `docker compose ps` and the `curl` in step 4. |
| Desktop app gets a 401 | A `WS_ACCESS_KEY` is set in `gateway.env`, or `MCP_GATEWAY_WS_KEY` in the environment. Remove it. |
| A backend fails with `cannot reach http://127.0.0.1:…` | Its container is down, or publishes a different port, or publishes on a bridge IP instead of `127.0.0.1`. Check `docker compose ps` and the `ports:` line. |
| A backend fails with `HTTP 401` or `HTTP 403` | The header is wrong or its value is. `--check` says whether the `${NAME}` resolves; the server decides whether the value is right. |
| A backend fails with `HTTP 404` on every call | The url's path is wrong. That is not a lost session: the gateway only renews a session it was actually given. |
| Saving in the admin UI fails with `cannot write catalogue`, or `./config` files end up owned by someone else | The container is writing as the wrong account: `.env` is missing or holds the wrong numbers. `cat .env` and compare with `id -u` and `id -g`, then `docker compose up -d`. Failing that, `./config` is not writable, or a single file was mounted instead of the directory. |
| Works with `docker run -p 127.0.0.1:8765:8765` but the app cannot connect | A published port on a bridge network is not the box's loopback. Use `network_mode: host`, as the compose file does. |
