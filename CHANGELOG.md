# Changelog

Notable changes to python-mcp-gateway, newest first.

This project is pre-1.0. Until 1.0, a **minor** bump is allowed to break the client-facing
wire, the `servers.yaml` schema, or the `gateway.env` grammar; a patch bump is not. Each
release opens with what it is *for*, because a list of changes without a thesis is a list
nobody reads.

## Unreleased

**The briefing quotes payloads as TOON.** The request and response blocks in the Clipboard
document — and so in `gateway__clipboard` — are written in
[TOON](https://github.com/toon-format/spec) rather than pretty-printed JSON. It is the same
data with the punctuation left out and a uniform array's keys stated once as a header
instead of once per element, which is exactly the shape `tools/list`, `resources/list` and
`prompts/list` answer in. The new `toon.py` is an encoder only: nothing here reads TOON
back, and every wire frame, `servers.yaml` and JSON-RPC payload stays JSON. Where
compactness and strict decodability disagree it gives up the compactness, because a document
the reader mis-parses is worse than the JSON it replaced. A call with no arguments now says
`Request: no arguments.` rather than fencing an empty box, and a payload that is already
prose is still fenced as prose.

**The bench briefing is terse.** The document `gateway__clipboard` returns — and the
Clipboard modal shows — now opens at `## Results` and goes straight to the first call. The
title, the `Generated …` and captured lines, the paragraph explaining what a bench session
is, the "not instruction" warning, the call count and the note introducing the injectable
values are all gone from the text: every one of them was paid for out of the reading model's
context window, on every call, to say something that is not evidence about the bench. The
warning moved to the tool's description, which is read once at `tools/list`; when the
snapshot was captured is reported beside the document by `admin.clipboard.get` rather than
inside it. What survives is the one line the headings cannot show: a session with more
calls than the document carries still says how many it left out. The snapshot no longer
carries the selected server or the bind address either,
since nothing rendered them. The renderer now has no clock in it, so the `/admin` and `/mcp`
surfaces are byte-identical documents rather than documents agreeing below their first line.

**The Clipboard shows its document rendered.** The briefing the modal generates is Markdown,
and until now the only way to see what it would look like to whoever it was pasted to was to
paste it somewhere. It now opens rendered, with a toggle in the modal's header that switches
to the plain text and back and stays where it was put. The text is still the document: Copy
takes what is in the box, edits included, whichever view is on screen. The rendering is
built out of DOM nodes rather than markup — the briefing quotes output from backend servers,
so nothing in it is ever parsed as HTML and no link in it is ever clickable.

**The daemon starts on Windows.** `loop.add_signal_handler` is a Unix method — asyncio's
Windows loops raise `NotImplementedError` from it — and handlers are installed before the
socket is bound, so on Windows the gateway did not fail to *reload*, it failed to **start**.
The desktop shell reported that as `no port file`. Signals now install through whichever
mechanism the platform has: `add_signal_handler` where it exists, `signal.signal` plus
`call_soon_threadsafe` where it does not, and Ctrl-Break joins SIGINT and SIGTERM in
stopping the daemon. Nothing delivers SIGTERM on Windows anyway; what the desktop app relies
on there is its Job Object, which is the same guarantee by another route.

The `gateway.env` permission warning is now POSIX-only. Windows synthesises `0o666` for
every readable file whatever its ACL says, so the check fired on every start, about a file
in the user's own profile, advising a `chmod` that machine does not have.

**The desktop shell builds on Windows.** Two things stood between the Windows runner and the
Job Object code it exists to compile. `tauri_build::build()` compiles `icons/icon.ico` into a
Windows Resource file, and the icon set shipped only PNGs — so the build failed at the crate,
before anything of the app was exercised. Behind that, `windows-sys` gates *functions* on
every feature their signature reaches into, and `CreateJobObjectW` takes a
`SECURITY_ATTRIBUTES`: without `Win32_Security` the symbol is not absent-with-an-explanation,
it is absent, and the compiler suggests `CreateJobSet`. The `.ico` and the macOS `.icns` are
generated from the same placeholder `icon.png` and committed;
`scripts/bundle_python.py` joined that workflow's path filter in the same breath, because it
is what `make tauri-python` runs and a fix to its Windows half had already merged without the
workflow ever running.

**GET_STARTED.md: the walkthrough the repository did not have.**

There were two kinds of document here and neither was a path through the project. `README.md`
is a pitch and a reference; the sibling `.md` beside each module carries the reasoning behind
that module, written for somebody already inside. A newcomer had to assemble the order
themselves — and one whole feature was documented only from the inside.

[`GET_STARTED.md`](GET_STARTED.md) is that order: install, the two files and why they are
split, configuring a backend by hand and through the UI's `+ Add` dialog, attaching Claude
Code over either transport and Claude Desktop over the bridge, a tour of the admin UI, and
day-two work — rotating a credential, reloading, running under launchd.

Its longest section is the one nothing user-facing covered: **how to make your own MCP server
light up the Injectable values column.** That column activates when a server publishes a
listing resource whose URI is a URI template's fixed prefix, reads its values out of a JSON
body in one of three shapes, and follows `readOne` and `narrows` in that body to build a
cascade. Every rule for it existed in `webui.md`, written as why the UI does what it does; it
is now also written outward, as what a server has to publish, with a checklist and the
failure modes. `webui.md` links to it and keeps the reasoning.

Three stale facts fell out of writing it and are fixed. The Makefile claimed `make run`
reuses `WS_ACCESS_KEY` from `gateway.env`; it does not — it reuses `MCP_GATEWAY_WS_KEY` from
the environment, else mints a fresh key for that run, and since it always exports the result
the file's key never wins under `make run` (it is for a daemon started directly, under
launchd or in a container). `gateway.env.example` now says so where the key is set.
`ARCHITECTURE.md` and `webui.md` counted eight UI assets and six ES modules, where `ASSETS`
has held nine and seven since `clipboard.js` landed. And `README.md`'s picture of the UI
still showed backends in a left column, which they left when they became header pills, and
no Injectable values column at all.

**The Clipboard: hand a bench session to an agent.**

The admin UI is where a person works a backend out by hand — press Send, read the answer,
pick a value, press Send again. What comes out of that is evidence, and it used to be
trapped in the page. The Results column now has a **Clipboard** button: it opens a modal
holding one text document — every call with its arguments and its answer, then every value
the Injectable values column is offering and which of them are picked — in a plain editor,
so it can be trimmed or annotated, and copied.

The document is rendered by the daemon, not by the page, and that is the whole design. The
same text is a tool: **`gateway__clipboard`** on `/mcp` returns it unedited, so a model can
read what just happened at the bench without anyone pasting anything. One renderer
(`clipboard.py`), two surfaces — the alternative is two renderers that drift, which would
surface as the agent being briefed on a session that did not happen.

The page publishes a *snapshot* of its two right-hand columns over `admin.clipboard.put`
whenever either changes, so the tool answers with the current bench rather than with
whatever it looked like the last time somebody pressed a button; the document states both
when it was captured and how long ago that was. Every quoted payload is backend output, and
the preamble says so in as many words — a model reading a tool result as an instruction is
the one failure this document would otherwise invite.

**The desktop app on Linux and Windows.**

`publish-artifacts.yml` now builds the shell on four runners — Apple Silicon, Intel Mac,
Linux and Windows — and attaches a bundle from each to the release: a `.app.tar.gz`, a
`.deb` and an AppImage, and an NSIS installer that installs for the current user and needs
no administrator. Every leg runs the Rust suite before it builds, so the platform-specific
half is compiled and tested somewhere rather than only written down.

That half is orphan control, which is the one thing that cannot be written once. A gateway
that outlives its window still holds every credential in `gateway.env`, and each platform
has a different primitive for making sure it does not: macOS keeps its three layers, Linux
adds `PR_SET_PDEATHSIG` so the kernel signals the daemon even when the app is `SIGKILL`ed,
and Windows uses a Job Object whose `KILL_ON_JOB_CLOSE` limit does the pidfile's job in the
kernel. Two portability bugs came out of writing it down: `ps -o comm=` returns a truncated
*name* on Linux where it returns a path on macOS, which would have turned the reaper silently
off, and `platform.machine()` says `AMD64` on Windows, which no triple in the project knew.

Nothing cross-compiles — each runner builds for itself — so the only platform differences in
`bundle_python.py` are the shape of the interpreter tree, and the two strip lists are checked
against each other on every platform rather than only on the one that ships them.

**A desktop app: the daemon, its backends and the admin UI in one window, with no terminal
and no key to copy.**

Until now, running the gateway meant `make venv`, a terminal, and pasting a URL with a secret
in its query string out of a startup banner. `desktop/` builds `MCP-Gateway.app` instead: a
Tauri shell whose Rust host mints the access key at launch, supervises the daemon as a child
process on a bundled CPython, and opens the daemon's two WebSockets itself.

That last part is the point. A browser's `WebSocket` constructor cannot send an
`Authorization` header, which is why the page has always carried its key in the URL and why
`python-mcp-gateway-isb` sat deferred rather than picking a browser-shaped workaround. A host
process just sends the header. The window never sees a secret, dials nothing, and its content
security policy says so.

**The daemon is unchanged.** `Authorization: Bearer` was already accepted, and a client that
sends no `Origin` was already allowed — the branch that exists for `mcp-gateway-connect` and
for Claude Desktop, which a Rust WebSocket client falls straight into. Both are now pinned
from the Python side by `tests/test_desktop_contract.py`, because a convenience that becomes
load-bearing should fail a test rather than someone's window.

### Added

- **`desktop/`**: the Tauri shell. A Rust host (`supervisor.rs`, `proxy.rs`, `key.rs`,
  `pathenv.rs`, `appdata.rs`) and the configuration to bundle it. `make tauri-python`,
  `make tauri-dev`, `make tauri-bundle`, `make tauri-check`. macOS, unsigned, first cut —
  see [`desktop/README.md`](desktop/README.md).
- **`scripts/bundle_python.py`**: builds the interpreter the app ships — a
  python-build-standalone CPython with the gateway wheel installed into it, stripped of
  developer tooling and Tk, pre-compiled, and then *verified*: the build fails if the result
  cannot import the gateway, find the UI assets through `importlib.resources`, or validate a
  config. Works because both runtime dependencies are pure Python by decision, so the
  interpreter is the only per-target artifact.
- **`scripts/stage_ui.py`**: puts the admin UI where Tauri looks for a frontend without
  making a second copy of it — a symlink for development, and for a bundle a copy that takes
  `webui.ASSETS` as its manifest, so the app can never ship a file the HTTP server would not.
- **`ui/tauri-transport.js`**: the shell's transport. Inert in a browser.
- **Ad-hoc code signing** for the bundle (`signingIdentity: "-"`, no certificate needed).
  Without it Tauri writes no `_CodeSignature` at all and `codesign --verify` reports "code
  has no resources but signature indicates they must be present" — a bundle Apple Silicon
  may refuse, with a LaunchServices error code that names the symptom and not the cause.
- **Port discovery with no new Python surface.** The app starts the daemon with `--port 0` and
  reads the port out of its existing `listening on ws://…` line, so a desktop app and a
  `make run` daemon cannot collide on 8765.

### Changed

- **`rpc.js` has a transport seam.** Opening a socket is one replaceable function; reconnect,
  the backoff, the pending table and the MCP handshake are not. This is not only for the
  shell: `tests/ui/` previously had to stub `globalThis.WebSocket` with something that could
  silence a socket but never drive one, so the handshake, the timeout-cancellation and the
  backoff were all untested. They are now.
- **`app.js` stands its key handling down inside the shell.** No key is read, none is written
  to `localStorage`, and the gate becomes a status panel showing what the host knows — that
  the gateway is starting, restarting, or refused its config — instead of asking for a secret
  that does not exist. The three-strikes retry stop is a browser rule and does not apply.
- **`check_docs.py` skips `target/`.** Cargo's build tree fills with vendored crates whose
  READMEs link within their own repositories; without this, `make docs-check` fails on files
  nobody here wrote. The same bug as the `.venv` one it already records, in a second language.

### Fixed

- **`tests/ui/harness.mjs` no longer installs jsdom's timers over Node's.** jsdom's
  `setTimeout` calls `globalThis.setTimeout`, so the first scheduled callback recursed until
  the stack ran out. Latent while no suite scheduled anything; immediate once `rpc.js`'s
  reconnect and request timeouts were under test.


**The admin UI: a browser console for configuring the gateway and exercising what it
publishes, served by the daemon itself.**

Until now the only way to see what a backend actually publishes was to attach Claude and ask
it, and the only way to change `servers.yaml` was `$EDITOR` and a SIGHUP. This adds a page
that does both — and, because it is a test bench, one that talks to `/mcp` through a real
MCP handshake, so what you exercise in it is byte-identical to what the model gets.

### Added

- **`/ui`**: the admin UI's static assets, served from the same port as the two sockets.
  Six files — HTML, CSS and three ES modules — with no build step and no dependency. Three
  columns: backends and their config, the selected backend's tools/prompts/resources with a
  form generated from each one's own JSON Schema, and the results of invoking them. The
  assets carry no access key, because a browser cannot key a navigation and they contain
  nothing; see `src/mcp_gateway/webui.md`.
- **A form built from `inputSchema`**, following the contract in python-acp's
  `docs/tool-schema-contract.md`: enum dropdowns with `enumNames` labels, bounds, patterns,
  formats, array editors, nested objects and `dependentRequired`. A schema using
  `if`/`then`/`else`, `dependentSchemas`, `allOf` or a discriminated `oneOf` steps aside to
  a raw JSON box with a reason rather than rendering half a conditional schema. An untouched
  optional field is *omitted* rather than sent as `""`.
- **`config_writer.py`**, the only module that writes `servers.yaml`: a `ruamel.yaml`
  round-trip so the file's comments survive, validation through `config.parse` before
  anything reaches disk, an atomic replace with a `.bak`, and a warning — never a refusal —
  for an `env` value that is a literal rather than a `${VAR}` reference.
- **`admin.config.set`, `admin.backend.add`/`update`/`remove`** on `/admin`, each reloading
  by default. **`admin.secrets.missing`** names the `${VAR}` an enabled server needs and the
  store does not have.
- **`examples/zoo_server.py`** and **`servers.dev.yaml`**: a local MCP server whose only
  purpose is to be rendered — thirteen tools, one per JSON Schema construct including the
  four a form must decline, five prompts, eight resources and four URI templates. Vendored
  from python-acp (Apache-2.0, same author). `make run-dev` starts the gateway against it.
- **`completion/complete`, forwarded**: the MCP method that suggests what one argument may
  be, routed to the backend its `ref` names — a namespaced prompt name or a `mcpgw://` URI,
  resolved by the same finders `prompts/get` and `resources/read` use. `context.arguments`
  rides along, and is stripped for a backend that negotiated `2024-11-05`, where the member
  does not exist. A backend that declared no `completions` is answered with an empty list
  rather than an error: a completion is a hint, and a hint must never fail the form it was
  helping with. The capability is advertised unconditionally, for the reason the other three
  are — see `src/mcp_gateway/protocol.md`.
- **A cascade in the zoo, and in the UI's Variables column**: `zoo://continents` narrows to
  the countries of one continent, which narrow to the animals of one country, which read
  through the same `zoo://animals/{id}`. A listing names its child in its own body with
  `narrows`, the sibling of `readOne` — so every URI the column reads is one the server
  handed it, and "a resource that pairs with no template is never fetched" still holds all
  the way down. Picking a continent fills the group below it; changing one buries what it
  decided, picks and all. The same data answers `completion/complete`, so a template's two
  variables and `zoo-prompt-animal`'s three arguments cascade the spec's way too — offered
  in the field as you type, as a `<datalist>` that suggests without constraining.
- **Two templates sharing one listing no longer alias each other** in the Variables column.
  Both `zoo://animals/{id}` and a longer template over the same prefix cut back to one URI,
  and the cache and the picks are keyed by it; the simplest pairing now wins and the rest are
  reached through `narrows`.

### Security

- **A WebSocket carrying an `Origin` header must name this server, or it is refused with a
  403.** WebSocket has no same-origin policy, so before this any page you visited could open
  a socket to `127.0.0.1:8765` — a daemon that spawns the commands in its config and holds
  every credential on the machine. A browser becoming a first-class client is what turned
  that from a note into the thing to close. Non-browser clients send no `Origin` and are
  unaffected; `MCP_GATEWAY_WS_ALLOWED_ORIGINS` widens the set.
- The UI's assets are served under a `Content-Security-Policy` of `default-src 'none'` with
  `script-src 'self'`: the page loads nothing it did not ship, so an admin console for a
  credential store still works on a machine with no route off it.
- **Nothing gained the ability to write a credential.** Config writing landed on `/admin`,
  which the model cannot reach; there is still no method on any path that writes a value into
  `gateway.env`, and `tests/test_admin_channel.py` asserts it.

### Fixed

Three of these were found by loading the page in a real headless browser. None of them
could have been caught by an HTTP-level assertion, and the first two passed every DOM
assertion that was made about them.

- **The access-key gate was painted permanently, over a working page.** `.gate` sets
  `display: grid`, and a class selector outranks the user-agent stylesheet's
  `[hidden] { display: none }` — so the overlay never went away, while
  `document.getElementById('gate').hidden` cheerfully reported `true`. Fixed with an
  explicit `[hidden] { display: none !important }`, which is what makes the `el.hidden`
  idiom the UI uses throughout mean anything at all.
- **`/ui` served the page but the page could not load its own assets.** A document served
  *at* `/ui` has `/` as its base URL, so the browser resolved `style.css` to `/style.css`
  and got a 404: an unstyled page with no JavaScript, from a URL that answered `200`. `/ui`
  now answers `308` to `/ui/`, carrying the query string (which is where the key is), and
  the banner prints the canonical form.
- **A page sitting on the access-key gate reconnected forever**, a 401 against the daemon
  every few seconds for as long as the tab was open. A socket that has never once connected
  now stops after three attempts and says so; Connect builds fresh ones.
- **`make run` never started the daemon.** `scripts/start-gateway.sh` invokes
  `python -m mcp_gateway.cli`, and `cli.py` had no `if __name__ == "__main__"` guard — so
  the module imported, defined everything, and exited 0 without binding anything. The
  Makefile prints the banner itself, so the failure looked exactly like a daemon that had
  started and was quietly serving. The console script (`mcp-gateway`) goes through its entry
  point and was never affected, which is why every test and every hand-run missed it. Found
  while verifying `make run-dev`; `tests/test_cli_paths.py` now runs the module as a script.

### Changed

- `secrets.missing_for` is now the one implementation of "which `${VAR}` is missing", shared
  by `--check` and `admin.secrets.missing`. `cli._resolve_missing` delegates to it.
- The startup banner and `make run` print the UI URL.

## 0.1.0 — 2026-09-11

**The first working gateway: a WebSocket MCP daemon with a consolidated credential store,
fanning out to stdio backend servers whose lifecycle it owns.**

The problem it solves is credential sprawl. Every MCP server needs its own token wired into
its own client config, and every one inherits whatever happened to be in the shell that
launched the client. This puts every credential in one gitignored file, gives each backend
only its own, and presents all of them as a single MCP server.

### Added

#### The daemon

- MCP over WebSocket on `/mcp`, to any number of concurrent clients. Backends are a single
  shared pool: three clients attached means one copy of each backend, and one live state
  that everybody observes identically.
- Backends spawn eagerly and concurrently at daemon start, **before the socket binds**, so
  the first client finds a warm pool and nobody can connect into a half-populated catalogue.
- A backend that fails is skipped and logged; the daemon still serves. `required: true` opts
  one server into making that fatal instead.
- `mcp-gateway-connect`, a thin stdio↔WebSocket pump, so today's stdio-only clients attach
  unchanged. It reconnects with backoff rather than exiting, so a daemon restart does not
  make a client mark the server dead.

#### The credential store

- `gateway.env`, gitignored and mode-checked, holding every value. `servers.yaml`,
  committed, holding only `${NAME}` references.
- Each backend's environment is built **from nothing**: an allowlist of process-shaping
  variables, plus that server's own `env_passthrough` and `env`. No backend sees another's
  credential, or the operator's. `env_mode: inherit` is the per-server escape hatch and logs
  a warning every time it is used.
- `${VAR}` resolves from `gateway.env` only, never from the ambient environment — resolving
  from the shell is precisely the leak the project exists to stop.
- Interpolation is refused in `command` and `args`, with an error that explains why: argv is
  world-readable through `ps`.
- Every known secret value is scrubbed from every log record, at the **root** logger, so a
  backend that prints its own token to stderr is covered too.

#### Aggregation

- Tools, prompts and resources merged across backends and namespaced: `server__tool`, and
  `mcpgw://server/<encoded-uri>` for resources, which keeps two backends publishing the
  identical URI from colliding.
- Per-backend listing cache, invalidated by that backend's `list_changed`.
- `gateway__list_backends`, `gateway__backend_health`, `gateway__restart_backend` and
  `gateway__reload_config` — so a model can find out why a tool it expected is missing.

#### Reload

- `gateway__reload_config`, `admin.reload`, and SIGHUP all run one path. The diff is on
  **resolved** spawn identity, so rotating a token in `gateway.env` registers as a change
  while an untouched backend keeps its process and its in-flight work.
- If either file fails to parse, nothing changes at all.

#### The admin channel

- `/admin`, a plain JSON-RPC method table for the future UI: status, backends, health,
  config (with `${VAR}` references unresolved), secret **key names**, and a redacted log
  stream. Read-only in this release.
- Admin verbs are deliberately absent from the model's tool list, which is the whole reason
  for a second path.

### Notes

- No `mcp` SDK. The gateway is consume-and-forward in both directions, so it moves raw
  dicts: a pinned model layer would silently drop fields a newer spec revision adds. Runtime
  dependencies are `ruamel.yaml` and `websockets`, both exact-pinned.
- `mcp_stdio.py` and the transport half of `transport_ws.py` are lifted from
  [python-acp](https://github.com/Digital-Dimentia/python-acp), comments included, with four
  documented divergences recorded in `src/mcp_gateway/mcp_stdio.md`.
- 338 tests, all against real subprocesses and real sockets. A suite-wide guard fails the run
  if any test leaves a subprocess behind.
