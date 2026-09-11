# Changelog

Notable changes to python-mcp-gateway, newest first.

This project is pre-1.0. Until 1.0, a **minor** bump is allowed to break the client-facing
wire, the `servers.yaml` schema, or the `gateway.env` grammar; a patch bump is not. Each
release opens with what it is *for*, because a list of changes without a thesis is a list
nobody reads.

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
