# Changelog

Notable changes to python-mcp-gateway, newest first.

This project is pre-1.0. Until 1.0, a **minor** bump is allowed to break the client-facing
wire, the `servers.yaml` schema, or the `gateway.env` grammar; a patch bump is not. Each
release opens with what it is *for*, because a list of changes without a thesis is a list
nobody reads.

## Unreleased

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
