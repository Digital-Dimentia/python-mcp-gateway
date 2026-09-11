# `bridge.py`

`mcp-gateway-connect` — pumps newline-delimited JSON between stdin/stdout and the daemon.

Claude Code and Claude Desktop attach an MCP server over stdio. The daemon speaks
WebSocket. This is the ~200 lines that let today's clients reach it unchanged, and it is
deliberately the *only* place that bridges them: the daemon stays one protocol on one port,
with no second transport to implement, test, or keep in step.

```
client ──stdio──► mcp-gateway-connect ──ws──► mcp-gateway daemon ──stdio──► backends
```

## It does not parse MCP

It reads a line, checks it is JSON, sends it as a frame; reads a frame, writes it as a line.
It never inspects `method`, never tracks a correlation, and answers nothing on its own
except the one case below.

That is the correctness argument, not laziness. A bridge that understood MCP would have a
revision of the protocol baked into it and would silently drop or mangle whatever a newer
one added — the same lossy-proxy failure that kept the `mcp` SDK out of the daemon. This one
is correct for every revision, forever, because it has no opinion.

## stdout is the wire

The one module in this project where that is true, and the reason `configure_logging` names
stderr in *both* entrypoints.

`_reserve_stdout` points `sys.stdout` at stderr for the life of the process, after the
`Bridge` has captured the real handle. A stray `print` — ours, or a library's — then lands
somewhere harmless instead of desynchronising the client, which is a failure that presents
as the client hanging rather than as an error.

POSIX only. Windows is excluded because some transports resolve `sys.stdout` at write time,
which would send the protocol to stderr instead — the exact inversion this is meant to
prevent.

## Reconnecting rather than exiting

A daemon restart is routine: an upgrade, a launchd cycle, a reload that went wrong. If the
bridge exited, `claude mcp` would mark the server dead and a human would have to notice and
re-add it.

So a dropped connection is retried with backoff — fast at first, because the common cause
takes under a second to resolve, capped at 5 s so a person waiting does not conclude it is
broken.

**Any request in flight when the socket died is answered with an error** naming the reason.
A client waiting forever on a request the daemon never saw is worse than a request that
failed, and this is the one thing the bridge answers on its own — which is why it reads
exactly one field, `id`, and nothing else.

`--no-reconnect` exits instead, for scripts and tests.

## The key goes in a header

The daemon accepts `?key=` and `Authorization: Bearer`. The bridge is the one client we
control, so it uses the carrier that does not end up in an access log. See
[`transport_ws.md`](transport_ws.md) for why the query form exists at all.

`resolve_key` reads `gateway.env` only when told where it is, or when the environment names
a config file it can sit beside. A bridge is spawned from an arbitrary directory by a
client, so guessing `./gateway.env` would be guessing.

## Usage

```bash
claude mcp add gateway -- mcp-gateway-connect --url ws://127.0.0.1:8765/mcp
```

with `MCP_GATEWAY_WS_KEY` in the environment, or `--env /path/to/gateway.env`.
