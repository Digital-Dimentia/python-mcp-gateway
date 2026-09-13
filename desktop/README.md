# The desktop shell

A Tauri app that is the gateway and its admin UI in one window: a Rust host that mints the
access key, supervises a bundled-CPython gateway as a child process, and opens that
gateway's two WebSockets itself.

This file is how the shell is built and why it is shaped this way. For what to do once it
opens — configuring backends, attaching a client, working the admin UI — see
[GET_STARTED.md](../GET_STARTED.md); everything there applies unchanged, minus the terminal
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
make tauri-bundle     # the installable bundle, under desktop/src-tauri/target/release/bundle/
make tauri-artifacts  # what tauri-bundle produced, renamed for this platform, in artifacts/
make tauri-check      # cargo fmt --check, clippy -D warnings, cargo test
```

`cargo tauri` comes from `cargo install tauri-cli --version "^2" --locked`.

**`make tauri-python` comes first, once, and `make tauri-check` needs it too.** `tauri-build`
validates every path in `bundle.resources` at *compile* time, so the crate does not build at
all until the interpreter is on disk — and what it says when it is not is `resource path
\`resources/python\` doesn't exist`, which names the symptom rather than the fix. The
Makefile checks for it and says so instead. The UI staging, being a file copy and not a
download, is handled for you.

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

`icons/` carries Tauri's default icon list, and two of the five entries are there for the
build rather than for the eye: `icon.ico` is compiled into a **Windows Resource file by
`tauri_build::build()`**, so a missing one is not an app with a blank icon, it is a crate
that does not compile — on Windows only, where no developer here builds. `icon.icns` is the
macOS half of the same list. Both are generated from `icon.png` with `cargo tauri icon`;
regenerate them from that file rather than adding a differently-drawn one beside it.

## Not done yet

The bundle is ad-hoc signed and un-notarised, and the icons are a generated placeholder.
`dmg` is not a bundle target because `bundle_dmg.sh` drives Finder through AppleScript; it
belongs with the signing work, which has to sign every Mach-O inside the bundled
interpreter. The Windows installer is unsigned too, which means SmartScreen warns on it.
See `python-mcp-gateway-c1q`.
