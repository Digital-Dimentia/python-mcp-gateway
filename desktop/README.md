# The desktop shell

A Tauri app that is the gateway and its admin UI in one window: a Rust host that mints the
access key, supervises a bundled-CPython gateway as a child process, and opens that
gateway's two WebSockets itself.

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
[`python-mcp-gateway-isb`](../src/mcp_gateway/transport_ws.md) sat deferred rather than
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

## Why the gateway is a bundled interpreter and not a frozen binary

Both of the gateway's runtime dependencies are pure Python *by decision* — `ruamel.yaml`
taken plain rather than with the `libyaml` extras, `websockets` chosen over the `mcp` SDK
partly to keep compiled extensions out of the process. So `site-packages` is
architecture-independent and the interpreter is the only per-target artifact in the bundle,
which Tauri builds per target anyway.

A freezer (PyInstaller, Nuitka) would buy one file and cost the thing `webui.py` depends on:
`importlib.resources` finding `mcp_gateway/ui/*.js` inside the bundle exactly as it finds
them in a checkout. It also keeps the app debuggable — `cli.py` in a shipped `.app` is still
a file you can open when a user reports something.

`scripts/bundle_python.py` builds it: a python-build-standalone interpreter via `uv`, the
wheel installed into it, the developer tooling and Tk stripped, everything pre-compiled, and
then a set of checks that *fail the build* if the result cannot import the gateway, find the
UI assets, or validate a config. Roughly 58 MB, which makes the `.app` about 96 MB.

## Why there is only one copy of the admin UI

The assets live in `src/mcp_gateway/ui/` and nowhere else. `desktop/.staging/ui` is a view of
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
make tauri-bundle     # MCP-Gateway.app under desktop/src-tauri/target/release/bundle/macos/
make tauri-check      # cargo fmt --check, clippy -D warnings, cargo test
```

`cargo tauri` comes from `cargo install tauri-cli --version "^2" --locked`.

Neither `make test` nor `make build` depends on any of this. The daemon is the product and it
ships without the app; a checkout with no Rust toolchain runs the entire Python suite.

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

A gateway that outlives its window still holds every credential in `gateway.env`. macOS has
no `PDEATHSIG`, so there are three layers: the child gets its own process group and teardown
signals the *group* (taking the `npx` backends with it even if the daemon is wedged);
`kill_on_drop` covers the panic path; and a pidfile, checked at startup against the running
process's executable, covers the app itself being Force Quit.

## What the tests cover, and what they cannot

`make tauri-check` runs 43 tests. Three of them are the real thing: they spawn the bundled
interpreter, scrape its port, open `/admin` through the proxy with a Bearer header, round-trip
`admin.status`, confirm a wrong key gets a 401 and a non-allowlisted path is refused, and
confirm the port is free again afterwards. They skip rather than fail when
`make tauri-python` has not been run.

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

## Not done yet

The bundle is ad-hoc signed and un-notarised, and the icons are a generated placeholder. `dmg` is
not a bundle target because `bundle_dmg.sh` drives Finder through AppleScript; it belongs with
the signing work, which has to sign every Mach-O inside the bundled interpreter. Linux and
Windows are a separate piece of work — Windows has no `setpgid` and wants a Job Object, Linux
gets `PR_SET_PDEATHSIG`, which is better than any of this.
