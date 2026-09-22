# The desktop shell

A Tauri app that is the gateway and its admin UI in one window: a Rust host that mints the
access key, supervises a bundled-CPython gateway as a child process, and opens that
gateway's two WebSockets itself.

This file is how the shell is built and why it is shaped this way. For what to do once it
opens — configuring backends, attaching a client, working the admin UI — see
[GET_STARTED.md](../../GET_STARTED.md); everything there applies unchanged, minus the terminal
and the access key.

```
MCP-Gateway.app
├── the window ── the admin UI, the same files webui.py serves
│        │ ipc
│        ▼
├── the host ──── mints the key · supervises the child · owns both sockets
│        │ stdio (stderr)          │ ws + Authorization: Bearer
│        ▼                         ▼
└── Contents/Resources/python ── standalone CPython + the gateway wheel
             │ stdio
             ▼
          npx backends
```

## Why the host owns the sockets

A browser's `WebSocket` constructor takes a URL and a subprotocol list. There is no header
parameter, so a page cannot send `Authorization: Bearer` — which is why the browser build of
the admin UI carries its key in a query string, and why
[`python-mcp-gateway-isb`](../mcp_gateway/transport_ws.md) sat deferred rather than
choosing between a cookie, a subprotocol smuggle and a single-use ticket.

Here the question does not arise. The key is generated in Rust, goes into the child's
environment, and goes onto the socket as a real header. It is never serialised into a
command's arguments, never put in an event payload, and never written to disk. The window
has no secret to leak because it has never been given one.

Two things follow, and both are *absences* in the Python:

- **`Origin` needed no new rule.** `tokio-tungstenite` sends no `Origin` header, and
  `transport_ws.origin_permitted(None, …)` already returns true — the branch that exists for
  `mcp-gateway-connect`, for Claude Desktop, and for every non-browser client. A Tauri page
  dialling out directly would have needed `tauri://localhost` added to the allowlist.
- **No new key carrier.** `_offered_keys` already accepted `Authorization: Bearer`.

`transport_ws.py` is unmodified by this whole feature. That was the test of whether the
design was right, and both assumptions are pinned from the Python side in
`tests/test_desktop_contract.py` so that tightening either one fails a Python test rather
than someone's window.

## The one document this window loads that we did not write

A backend can ship a panel as HTML — SEP-1865's `text/html;profile=mcp-app` — and the daemon
serves it at `/panel/<token>` under `Content-Security-Policy: sandbox allow-scripts`, which
is what puts it in an opaque origin. See [`panels.md`](../mcp_gateway/panels.md) for why that
header, and not the `<iframe>` attribute, is the boundary.

This window cannot frame that URL, and the reason is the section above working as intended:
the page is served from the bundle at `tauri://localhost` and its policy names `ipc:` and no
network at all. A relative `/panel/<token>` resolves against `tauri://localhost` and finds
nothing; an absolute `http://127.0.0.1:<port>/…` is refused before a request exists.

So the host answers a scheme of its own. `panelframe.rs` registers `panel:`, fetches that
path from the daemon over loopback — a hand-written GET, since the one address this process
dials is 127.0.0.1 — and hands the webview the bytes **under the daemon's own
`Content-Security-Policy`, copied verbatim**. It builds no policy and parses none. Same rule
as `proxy.rs`: this layer carries what it is given and does not understand it, because a
layer that reshaped it would quietly make the browser and the window disagree about what a
panel may do.

Two consequences worth stating:

- **`frame-src panel: http://panel.localhost` is the window's only framing grant**, and it is
  not a hole in "the page reaches no network": it names this process's scheme, not the
  daemon's origin. Both spellings because WebView2 serves a custom scheme from
  `http://<scheme>.localhost/` and every other platform from `<scheme>://localhost/`.
- **The token is checked against the alphabet Python mints before it is written into a
  request line.** This module composes HTTP by hand from a string that arrived over IPC, so
  that check is what stands between a panel URL and request splitting.

## Two connection modes

The gateway does not have to be a child of this app. The **Connection** screen chooses:

- **Local** — the child described above. Unchanged, and what you get when nothing is
  configured: there is no settings file until somebody writes one, and its absence *is* the
  default.
- **Remote** — a daemon somebody else is running on another machine, reached through an SSH
  port forward this host opens and supervises. The backends and every credential stay over
  there. The app never starts, stops or restarts that daemon; it manages the tunnel and
  nothing else.

```
MCP-Gateway.app                      build-box
├── the window                       ┌──────────────────────────────┐
│        │ ipc                       │ mcp-gateway, bound to        │
├── the host ── ssh -N -L ───────────┼─→ 127.0.0.1:8765, no key     │
│        │ ws 127.0.0.1:8765         └──────────────────────────────┘
│        ▼   (the near end of the forward)
└── same two sockets, same admin methods
```

The forward is what makes this small: a forwarded loopback port is indistinguishable from a
local one at the socket layer, so `proxy.rs` still dials `ws://127.0.0.1:<port>` and nothing
downstream of it knows which machine answered.

```
ssh -N -T
    -o ExitOnForwardFailure=yes      # a forward that cannot bind must fail, not "connect"
    -o BatchMode=yes                 # stdin is null; a prompt would be a hang
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3   # notice a sleeping laptop
    -o ConnectTimeout=10
    -o ControlMaster=no -o ControlPath=none              # this child's life is the tunnel's
    -L 127.0.0.1:<local>:127.0.0.1:<remote>
    <destination>
```

`StrictHostKeyChecking` is deliberately not set. `accept-new` would be the app vouching for
a host key on the user's behalf, on the one connection that is also its entire
authentication story; first contact with a host is made once, by hand, in a terminal.

### Why remote mode carries no key

The same argument as the local one, arrived at from the other end. Locally the key exists
and the window never sees it. Remotely there *is* no key: the daemon over there binds
loopback, and only someone SSH already trusts can reach it. So `session::authorization`
returns `None`, `proxy::open` omits the header — omitted, not empty, which a keyed daemon
would answer with a 401 that reads like a broken tunnel — and nothing secret is stored on
this machine. `connection.json` in the app data directory holds a destination and two port
numbers, and is the first file in there this app owns rather than seeds.

`tests/test_desktop_contract.py` pins both halves from the Python side: a keyless loopback
daemon accepts a client with neither `Origin` nor `Authorization`, and `session.rs` still
says `Mode::Remote => None`.

### Save, Connect, Disconnect

Three buttons, because they are three decisions, and keeping them apart is what lets the
window be honest about each one. **Save** writes `connection.json` and starts nothing — you
can retype a destination while a tunnel is up without the tunnel noticing. **Connect** tears
down whatever is running and starts again from what was saved; it is the only way to change
mode while the app is open. **Disconnect** stops what is running and leaves it stopped.

That third one was missing, and its absence was not neutral: a tunnel opened in this window
could only be closed by quitting the app, and saving a switch back to local mode left it
open — so "I went back to local" looked from the screen like nothing had happened at all.

It needs both halves to work. `Inner::park` sets a flag *and* bumps the reconnect watch: the
flag alone stops nothing, because the loop is asleep on a child's stderr and will not read
it until something wakes it, and the signal alone would stop the tunnel and open another one
a moment later, since every other way out of that loop is a reason to retry. The loop reads
the flag at the top of every start, in the same place it reads the settings, and `Connect`
clears it — otherwise Disconnect would be a one-way door out of the app's own point.

Connect and Disconnect are **one button**, never both: only ever one of them does anything,
and a pair where the second is reachable only through the first reads as two equals. The
exception is a save that has not been applied — the window is connected, but not to what was
just saved — where Connect wins, because needing a disconnect first to change a port number
would be a step for nothing.

Disconnect is offered in both modes rather than only remote. "This stops whatever the window
is driving" is a rule somebody can hold; a button that appears and disappears with the mode
radio is one they have to relearn.

A stop is its own phase, `Phase::Stopped`, and it exists for the window rather than for the
host — the coarse `state` it reports is still `idle`. The page has to tell it from the
*other* idle, the moment between opening and the first spawn: that one is a splash you wait
through, and this one lasts until somebody acts. Painted as the splash, a disconnect sealed
the window shut — the gate covers the header, it shows no Retry while it believes something
is coming, and the way to the Connection screen was offered only to a failed *remote* gate.
The stopped gate offers both, and it never appears over the Connection screen itself, which
is where the stop was asked for and where the next gateway is chosen.

### When it will not open

`ssh`'s stderr is classified — never parsed for a value — into the kinds that want different
things done about them: authentication, a host key, an unresolvable name, a busy local port,
a far side that refused the channel, and the transient network. The first four stop at once,
because retrying a wrong host key five times is five identical failures and a worse message.
The last two keep trying: a closed laptop lid is not a broken configuration, and a daemon
that somebody has not started yet is not this app's failure at all — the tunnel stays up and
the window says so.

Readiness is a probe rather than a guess. `ssh -L` accepts locally the moment it has
authenticated and only *then* asks the far side to connect, so a bare TCP connect proves
nothing; `tunnel::probe` sends an HTTP request through the forward, which needs no key, and
tells a gateway from an empty tunnel from an unbound port.

## Why the gateway is a bundled interpreter and not a frozen binary

Both of the gateway's runtime dependencies are pure Python *by decision* — `ruamel.yaml`
taken plain rather than with the `libyaml` extras, `websockets` chosen over the `mcp` SDK
partly to keep compiled extensions out of the process. So `site-packages` is
architecture-independent and the interpreter is the only per-target artifact in the bundle,
which Tauri builds per target anyway.

A freezer (PyInstaller, Nuitka) would buy one file and cost the thing `webui.py` depends on:
`importlib.resources` finding `mcp_gateway_ui/*.js` inside the bundle exactly as it finds
them in a checkout. It also keeps the app debuggable — `cli.py` in a shipped `.app` is still
a file you can open when a user reports something.

`scripts/bundle_python.py` builds it: a python-build-standalone interpreter via `uv` — or,
on a machine without uv, downloaded by the script itself against a pinned hash — the
wheel installed into it, the developer tooling and Tk stripped, everything pre-compiled, and
then a set of checks that *fail the build* if the result cannot import the gateway, find the
UI assets, or validate a config. Roughly 58 MB, which makes the `.app` about 96 MB.

## Why there is only one copy of the admin UI

The assets live in `src/mcp_gateway_ui/` and nowhere else. `src/desktop/.staging/ui` is a view of
that directory, rebuilt by `scripts/stage_ui.py` and gitignored — a symlink for
`make tauri-dev`, so editing `app.js` in its real home is one Cmd+R away, and a copy for
`make tauri-bundle`, because the bundler would follow a symlink to a path that does not exist
inside the `.app`.

The copy takes `webui.ASSETS` as its manifest rather than globbing the folder, so the app can
never ship a file the HTTP server would not serve. `tests/test_desktop_layout.py` asserts the
staged files are byte-identical to their sources and that nothing else is there.

One file is new: `tauri-transport.js`. It is loaded by `index.html` in both hosts, and in a
browser it looks for Tauri, does not find it, and returns having done nothing.

## Building and running

```bash
make tauri-python     # the bundled interpreter — needs the network, takes a minute
make tauri-dev        # run from source, UI symlinked, edits live
make tauri-bundle     # the installable bundle, under src/desktop/src-tauri/target/release/bundle/
make tauri-artifacts  # what tauri-bundle produced, renamed for this platform, in artifacts/
make tauri-check      # cargo fmt --check, clippy -D warnings, cargo test
```

`cargo tauri` comes from `cargo install tauri-cli --version "^2" --locked`.

`make tauri-python` is the one step here that downloads anything other than a Python
package, and the one a corporate network breaks: see *Building behind a firewall* below.

**Re-stage the interpreter with the app stopped.** `cargo tauri dev` treats every file under
`src-tauri/` as a source file, and `resources/python` is a whole CPython living there — so a
re-stage underneath a running `tauri dev` restarts the app once per file `pip` writes, which
on screen is a window that says the gateway could not start and retries forever. `.taurignore`
keeps the watcher off that tree; the app still has to be restarted by hand afterwards, since
the daemon it would start is the one that just changed.

**And a second failure with the same symptom, which is why `tauri-python` deletes the copies
Cargo made.** Cargo stages `resources/` into `target/<profile>/`, overwriting files *in
place*. macOS caches a binary's code-signing identity per inode, so a `python3` whose bytes
changed under an inode the kernel has already seen is `SIGKILL`ed on exec — immediately, with
no output, and `codesign -v` still reports the file as valid, because the file is. What a
person sees is an app whose gateway will not start and a `Killed: 9` with nothing to search
for. The same bytes copied to a fresh path run perfectly, which is the whole diagnosis and
the whole fix: delete `target/<profile>/python` and let the next build write new inodes.

**`make tauri-python` comes first, once, and `make tauri-check` needs it too.** `tauri-build`
validates every path in `bundle.resources` at *compile* time, so the crate does not build at
all until the interpreter is on disk — and what it says when it is not is `resource path
\`resources/python\` doesn't exist`, which names the symptom rather than the fix. The
Makefile checks for it and says so instead. The UI staging, being a file copy and not a
download, is handled for you.

Neither `make test` nor `make build` depends on any of this. The daemon is the product and it
ships without the app; a checkout with no Rust toolchain runs the entire Python suite.

## Building without uv

uv is the default, not a requirement. When `uv` is not on `PATH`, `make tauri-python` fetches
the interpreter itself — the `install_only_stripped` tarball of the release pinned as
`PBS_RELEASE` / `PBS_PYTHON` in `scripts/bundle_python.py`, checked against a SHA-256 pinned
beside it before anything is unpacked — and installs the wheel with that interpreter's own
pip, which the strip step then removes. Nothing else changes: same layout, same
verification, and `BUNDLE.json` records `"source": "builtin"`.

```bash
make tauri-python                                           # no uv: the built-in fetcher
.venv/bin/python scripts/bundle_python.py --fetcher builtin # the same, with uv installed
```

The cost is the pin. uv floats to the newest patch of `DEFAULT_VERSION`; the built-in fetcher
ships exactly `PBS_PYTHON` until someone bumps it, which means copying the six
`install_only_stripped` lines out of the new release's `SHA256SUMS`. It takes the standard
Python variables rather than uv's: `SSL_CERT_FILE` for an intercepting proxy, `HTTPS_PROXY`,
and `PIP_INDEX_URL` / `PIP_TRUSTED_HOST` for the wheel's two dependencies.
`UV_PYTHON_INSTALL_MIRROR` works for both fetchers, so the mirror recipe below needs no uv
either.

## Building behind a firewall

One step of this build talks to the internet for something other than a Python package:
`make tauri-python` runs `uv python install` (or, without uv, the built-in fetcher above),
which downloads a python-build-standalone
interpreter. Everything after it — the wheel, the strip, the byte-compile, the verification —
is local. So when a corporate network breaks the desktop build, it breaks it there, and the
fixes below are in the order worth trying.

**First, assume TLS interception rather than a block.** uv ships its own certificate bundle
and does not see a corporate CA, which fails as a certificate error that reads like an outage:

```bash
export UV_NATIVE_TLS=1                  # trust the OS store, where the corporate CA lives
# or:  export SSL_CERT_FILE=/path/to/corp-ca-bundle.pem
export HTTPS_PROXY=http://proxy.corp:8080
make tauri-python
```

This is the same failure `PIP_TRUSTED_HOST` works around for `make venv`, arriving one layer
down. `scripts/bundle_python.py` passes the environment through to uv untouched, so every uv
variable works from your shell without a flag.

**Then, a mirror.** `UV_PYTHON_INSTALL_MIRROR` replaces
`https://github.com/astral-sh/python-build-standalone/releases/download` in the URL uv builds
and keeps the `/<release>/<asset>` tail, so an internal Artifactory or Nexus needs nothing but
the variable. It also accepts `file://`, which is how a machine with no egress at all still
gets a bundle — fetch the tarball anywhere, carry it in, and build offline:

```bash
# Somewhere with network. uv names the exact asset it wants for this platform:
uv python list --all-versions --output-format json \
  | jq -r '.[] | select(.key == "cpython-3.13.15-macos-aarch64-none") | .url'

# Lay it out as <mirror>/<release>/<asset>, keeping the filename's literal `+`:
mkdir -p ~/pbs-mirror/20260901
curl -Lo ~/pbs-mirror/20260901/'cpython-3.13.15+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz' "$URL"

# On the build machine:
UV_PYTHON_INSTALL_MIRROR=file://$HOME/pbs-mirror make tauri-python
```

**Last, skip the download entirely.** `--interpreter` takes an interpreter that is already on
the disk — an unpacked tree, or the `.tar.gz` as downloaded — and adopts it instead of fetching
one. It is for the machine where nothing can be made to reach the release host:

```bash
make build                                            # the wheel this installs
.venv/bin/python scripts/bundle_python.py \
    --interpreter ~/Downloads/cpython-3.13.15+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz
```

What you hand over is copied, not moved, so a download that was difficult survives a build
that fails. Take an `install_only` asset matching this machine's platform and architecture —
nothing here cross-builds — and note that `.tar.zst` is refused: unpack that one yourself and
pass the directory. Everything downstream is identical to a fetched interpreter, verification
included, and `BUNDLE.json` records `"source"` so a bundle that misbehaves can be traced back
to where its Python came from.

The interpreter is not the only fetch in `make tauri-python` — the wheel's two dependencies
come from PyPI — but that one is an ordinary package install and an internal index handles it.
Note that with uv the step runs `uv pip install`, which reads `UV_DEFAULT_INDEX` (or
`UV_INDEX_URL`) and **not** `PIP_INDEX_URL` — without uv it is plain pip, which reads
`PIP_INDEX_URL` as usual; `PIP_TRUSTED_HOST` is the exception, which
`scripts/bundle_python.py` translates into uv's `--allow-insecure-host` so there is one
variable to set rather than two.

## First run

The app creates `~/Library/Application Support/com.dbuschman7.mcp-gateway/` — the same
identifier as `scripts/com.dbuschman7.mcp-gateway.plist`, deliberately, so the launchd job
and the app are one deployment and not two.

Into it go `servers.yaml` (seeded with `servers: {}`, so a first launch starts nothing and
fails nothing) and `gateway.env` (mode `0600`). Both are then the user's. The app never
rewrites either: the admin UI owns `servers.yaml` through `config_writer.py`, which replaces
it atomically and keeps a `.bak`, and a second writer would be a data-loss bug. The one thing
re-checked on every launch is `gateway.env`'s mode — `secrets.py` only warns about a
group-readable credential store, and the app that created the directory can do better.

The seeded `gateway.env` has no `WS_ACCESS_KEY` line. The environment wins over the file, so
one written there would be read, ignored, and quietly confusing.

## The PATH problem

An app launched from Finder inherits launchd's `PATH`: `/usr/bin:/bin:/usr/sbin:/sbin`. Every
`npx` backend fails to spawn, and the user — who has `npx` in every terminal they have ever
opened — concludes the app is broken.

`pathenv.rs` asks the login shell once at startup (`$SHELL -ilc 'command printf %s "$PATH"'`,
three-second timeout, stdin closed) and falls back to the same list the launchd plist
hard-codes. `backend.py` forwards the daemon's `PATH` to every backend, so fixing it once
here fixes it everywhere.

**This is invisible in development.** `cargo tauri dev` inherits the terminal's `PATH` and the
bug cannot reproduce, which is why the checklist below insists on Finder.

The child's environment is *added to* rather than cleared, because `backend.py`'s
`env_passthrough` reads the daemon's own environment and clearing it would silently narrow a
documented feature of the daemon.

## Not leaving processes behind

A gateway that outlives its window still holds every credential in `gateway.env`. Every
platform has to answer the same two questions — kill the whole tree rather than just the
daemon, and survive the app itself being killed — and each answers them differently.

| | the group kill | the app is killed outright |
| --- | --- | --- |
| macOS | `setpgid` + `kill(-pid)` | pidfile, checked at startup |
| Linux | the same | `PR_SET_PDEATHSIG`, in the kernel |
| Windows | `TerminateJobObject` | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` |

macOS is the platform with the least help, which is why it has three layers: the child gets
its own process group and teardown signals the *group* (taking the `npx` backends with it
even if the daemon is wedged); `kill_on_drop` covers the panic path; and a pidfile, checked
at startup against the running process's executable, covers Force Quit.

**The first layer was missing for a while, and the way it failed is worth keeping.** The
exit handler cleared the two pidfiles and left the killing to `kill_on_drop` — which never
runs, because the process exits without unwinding and this crate aborts on panic. So
quitting signalled nothing, and then deleted the only record that would have let the next
launch find what was left. It accumulated silently: seventeen orphaned daemons on one
machine, the oldest eight days old, each still holding the backends it had spawned, and
each still holding every credential in `gateway.env` — which is the sentence at the top of
this section, arriving eight days late.

Two rules came out of it. **A pidfile is cleared only once its process is confirmed gone**;
a record that outlives a failed kill is exactly what the third layer is for, and one deleted
beside a live child is worse than never having written it. And **a signal is a quit**:
`SIGTERM` and `Ctrl-C` are routed into the app's own exit, because otherwise the same app
stops its gateway or strands it depending on whether somebody used the menu.

Linux keeps all three and adds `prctl(PR_SET_PDEATHSIG, SIGTERM)`, which is better than any
of them: the kernel signals the daemon when this process goes, including by the `SIGKILL` no
handler of ours survives. `SIGTERM` rather than `SIGKILL`, so the daemon's own handler still
takes its backends down cleanly. The `prctl` is not atomic with the fork, so the child also
checks that it has not already been reparented — a gateway whose death signal was missed is
exactly the orphan holding credentials this section is about.

Windows has neither process groups nor `PDEATHSIG`, and gets both properties from one Job
Object: every gateway is assigned to it, `TerminateJobObject` is the group kill, and the
`KILL_ON_JOB_CLOSE` limit means the kernel reaps the whole job when this process's last
handle to it closes — however this process died. That is the pidfile layer done by the OS,
which is why `appdata::reap_previous` is a no-op there.

`is_ours`, the check that stops a reused pid getting signalled, differs too: macOS reads the
full executable path out of `ps -o comm=`, and Linux reads `/proc/<pid>/exe`, because `comm`
on Linux is a truncated *name* and comparing it against a path would never match — turning
the reaper silently off rather than loudly wrong.

## What the tests cover, and what they cannot

`make tauri-check` runs 43 tests. Three of them are the real thing: they spawn the bundled
interpreter, wait for the port file it writes, open `/admin` through the proxy with a Bearer
header, round-trip `admin.status`, confirm a wrong key gets a 401 and a non-allowlisted path
is refused, and confirm the port is free again afterwards. They skip rather than fail when
the interpreter tree is there but incomplete.

Both CI workflows build the interpreter before checking, so those three run on all three
platforms rather than only on whichever machine happened to have a bundle lying around. On
Linux and Windows that is a stronger statement than `clippy` passing: it is the new orphan
control reaping a real process tree.

What no automated test here reaches is **the window**. So this stays a manual checklist:

1. Build, move `MCP-Gateway.app` to `/Applications`, and launch it **from Finder** — not from
   a terminal, which would mask the PATH case entirely.
2. All three columns populate, and no key is ever asked for.
3. Add an `npx` backend and confirm it starts. This is the PATH fix under the one condition
   that breaks it.
4. `kill -9` the gateway child with the window open: the pills go red, then green, and the
   listings come back without a page reload.
5. Quit, then check nothing is left: `pgrep -f mcp_gateway` is silent.
6. `ps eww <pid> | tr ' ' '\n' | grep MCP_GATEWAY_WS_KEY` shows the key; `ps ww` does not, and
   grepping the app data directory for it finds nothing.

### Remote mode

Everything under the SSH handshake — argv, the fault classification, the three-way probe,
the header decision — is covered by `cargo test`, and everything above the tunnel can be
driven from a terminal with `ssh -N -L` and `curl`. What is left needs the window, and a
second machine:

7. With a gateway running on the far box, set Remote in the Connection screen and watch it
   come up. Launch **from Finder**, for the reason step 1 gives: a tunnel that works under
   `make tauri-dev` and not in the bundle is the agent-socket case below, not a bug in the
   destination.
8. Take the line the screen hands you, `claude mcp add` it, and call a tool through it.
9. Edit a server from the window, and confirm the **far** `servers.yaml` changed and the
   local one did not.
10. Break each half in turn — `pkill ssh`, then stop the remote daemon — and confirm the
    window names *which* of the two broke, and recovers on its own when it comes back. From
    a terminal the two are already distinguishable: `curl` exits 7 when the tunnel is gone
    and 56 when the tunnel is up and nothing is listening on the far end.

**The agent socket is the thing most likely to bite**, and it is the same family as the
`PATH` case. Compare `echo $SSH_AUTH_SOCK` in Terminal with `launchctl getenv SSH_AUTH_SOCK`.
If they match — the usual case on macOS, where launchd runs the agent and passes it to GUI
apps alike — a Finder launch reaches the same keys your shell does and there is nothing to
do. If they differ, your keys are in an agent your shell starts (1Password, gpg-agent, a
`keychain` line in an rc file) and the app will see the other one, with no keys in it. The
`AuthRequired` hint says so, because "it works in Terminal" is exactly what you will have
just proved.

Windows remains entirely unexercised.

## Launching it

Double-click it in Finder, or `open` it from a normal Terminal window.

The bundle is `MCP-Gateway.app`, hyphenated. A space is perfectly legal in a bundle name and
half of Apple's own apps have one, but this is a developer tool whose path gets typed, pasted
into scripts and handed to `open` — and an unquoted path with a space in it fails in a way
that looks like the app is broken rather than like the command is. The window still says
"MCP Gateway": that is prose, and nothing has to quote it.

**It cannot be launched from an agent or CI shell.** Those sandboxes block the XPC
connection to `launchservicesd`, and `open` fails with a LaunchServices error whose code
names the symptom and not the cause — `-10822 kLSServerCommunicationErr` if you are lucky,
`-600 procNotFound` or `-10827 kLSNoExecutableErr` if you are not. None of the three is about
the bundle. Check the bundle itself with `codesign --verify --deep` (below) before believing
any of them.

The bundle is **ad-hoc signed** (`bundle.macOS.signingIdentity: "-"`), which needs no
certificate. That is not optional cosmetics: without it Tauri writes no `_CodeSignature` at
all, the main binary carries only the linker's ad-hoc signature, and

```console
$ codesign --verify --deep "MCP-Gateway.app"
… code has no resources but signature indicates they must be present
```

which on Apple Silicon is a bundle macOS may simply refuse. A correct one says `valid on
disk` and reports `Sealed Resources version=2`.

Signing turns the hardened runtime on, and a bundled CPython is exactly the thing that
usually breaks under it — ctypes wants `allow-unsigned-executable-memory`, and library
validation wants one Team ID. It does not break here, and the reason is worth knowing: the
gateway is a **subprocess**, so it is governed by its own signature rather than by the
host's. Verified by running the in-bundle interpreter's `ctypes` and `ssl` imports directly.

## Three platforms

`publish-artifacts.yml` builds the shell on four runners — Apple Silicon, Intel Mac, Linux
and Windows — and every leg runs `make tauri-check` before `make tauri-bundle`, so the
Windows Job Object and the Linux `PDEATHSIG` are compiled and tested somewhere. A macOS
developer cannot reach either of them locally, and code no machine ever builds is code that
does not work.

The same workflow runs by hand (**Actions → Publish artifacts → Run workflow**) as a
rehearsal: the four desktop legs build exactly what a release would and upload the bundles
as workflow artifacts, and the wheel-and-image job sits it out. A release is the wrong place
to learn that one platform does not build.

Nothing cross-compiles. The bundle carries a python-build-standalone interpreter *and* a
Tauri binary; each is happier built natively than cross-built against a webview SDK that is
not the host's, and `uv python install` already knows every triple. So each leg builds for
the machine it is on, and the platform differences in `bundle_python.py` are all *shape* —
Unix is `bin/python3` beside `lib/python3.13/`, Windows is `python.exe` beside `Lib/` and
`DLLs/`. `interpreter_path` there is twinned with `Layout::interpreter` in `supervisor.rs`,
and both are asserted from both sides, because a disagreement is an app that starts nothing
with a dialog that says nothing.

Each platform picks its bundle formats in an overlay Tauri merges over `tauri.conf.json`:

| | targets | why |
| --- | --- | --- |
| macOS | `app` | `tauri.conf.json` |
| Linux | `deb`, `appimage` | `tauri.linux.conf.json` |
| Windows | `nsis` | `tauri.windows.conf.json`, `installMode: currentUser` |

The overlays carry bundle targets and nothing else. The base config is where the design is
written down — the CSP that keeps the window off the network, the resources, the frontend
path — and a platform file that restated any of it would be a second copy able to disagree
with the first, on one platform only. `tests/test_collect_desktop_bundle.py` holds them to
that, and to the other half of the deal: every target built is one
`scripts/collect_desktop_bundle.py` knows how to publish, under a name that says which
platform it is for.

The Linux leg is pinned to the oldest supported runner image rather than `ubuntu-latest`:
the `.deb` and the AppImage carry whatever glibc they were linked against, and a bundle
built on the newest runner will not start on a two-year-old desktop.

**The Windows NSIS installer and the Linux packages have not been installed and launched by
a person.** Everything under the window is covered by `make tauri-check` on each platform in
CI, but the same manual checklist above is unwalked on both — see
`python-mcp-gateway-e6o`.

`icons/` carries Tauri's default icon list, and `bundle.icon` in `tauri.conf.json` has to
name it: the bundler reads that list rather than the folder, and without it the `.deb`
ships iconless and the AppImage refuses to build. Two of the five entries are there for the
build rather than for the eye: `icon.ico` is compiled into a **Windows Resource file by
`tauri_build::build()`**, so a missing one is not an app with a blank icon, it is a crate
that does not compile — on Windows only, where no developer here builds. `icon.icns` is the
macOS half of the same list. All of them are rendered from `icons/app-icon.svg` by
`make tauri-icons`: the admin UI's favicon, `src/mcp_gateway_ui/logo.svg`, drawn on the
rounded tile macOS gives every Dock icon, so the app and the browser tab wear one mark.
A test holds the two SVGs' drawing equal; change the mark in both, then re-run the target.
`make tauri-brand` still overrides the set per deployment, beside it in `icons/brand/`.

## Not done yet

The bundle is ad-hoc signed and un-notarised.
`dmg` is not a bundle target because `bundle_dmg.sh` drives Finder through AppleScript; it
belongs with the signing work, which has to sign every Mach-O inside the bundled
interpreter. The Windows installer is unsigned too, which means SmartScreen warns on it.
See `python-mcp-gateway-c1q`.
