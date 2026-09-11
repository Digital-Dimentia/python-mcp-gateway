# `cli.py`

The command line, logging configuration, and the process-level entrypoint.

## What this module owns alone

`sys.argv` and `SystemExit`. Nothing below this module reads the command line or exits
the process; they take their configuration as arguments and raise. That is what lets a
test drive the entire daemon in-process, with two clients attached, without a subprocess.

## Path resolution

`--config` falls back to `$MCP_GATEWAY_CONFIG`, then to `./servers.yaml`.

`--env` falls back to `$MCP_GATEWAY_ENV`, then to `gateway.env` **beside the resolved
config file** — not beside the cwd. launchd starts a job in `/`; a supervisor starts it
in whatever directory it happens to hold. Resolving the credential store against the
caller's cwd would work in every development run and fail in the one deployment the
daemon exists for, with the confusing symptom that every backend is skipped for a missing
secret that is plainly sitting in the file.

`default_config_path` and `default_env_path` both take an `environ` parameter defaulting
to `os.environ`. That is the same testability trick `transport_ws.py` uses for the access
key: precedence can be asserted without monkeypatching the running process.

## Logging goes to stderr, in both entrypoints

`configure_logging` names `stderr` explicitly. This process's stdout is not a protocol
wire — it serves MCP over WebSocket — but `mcp_gateway.bridge` calls the same function,
and there stdout *is* the wire. One stray log line on it desynchronizes the client. A
formatting decision that is merely tidy in one entrypoint and corrupting in the other is
not worth having in two places, so both take the safe one.

Two formats rather than one with an optional field: the default case is read by a human
watching a terminal, and a bare `%(message)s` is what that wants. `--debug` prepends the
logger name, which is the only thing that makes a message from `mcp_gateway.mcp_stdio`
distinguishable from one the gateway itself emitted.

## Exit codes

`0` on a clean shutdown or a successful `--check`. `2` for a startup refusal — a config
that does not parse, a required backend with a missing secret, a non-loopback bind with
no access key — matching argparse's own code for "you asked for something I will not do".

`KeyboardInterrupt` is caught and swallowed. Ctrl+C is how a foreground daemon is stopped;
printing a traceback for it tells the operator something went wrong when nothing did.

## Not yet wired

`_run` currently raises `NotImplementedError`. The daemon lands across phases 1–3 of
`python-mcp-gateway-vew`; the parser is complete now because its shape is what the
Makefile banner, the README, and the launchd plist all quote.
