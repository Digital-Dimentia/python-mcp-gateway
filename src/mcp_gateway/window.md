# `window.py`

A desktop window around a gateway running in this same process, for machines that cannot
build the Tauri shell.

## Why a second desktop host at all

The one in [`src/desktop/`](../desktop/README.md) is the better product. It bundles its own
CPython, so it installs onto a machine with no Python at all, and it ships as a signed
`.app`, `.deb` or `.exe`.

It is also built with `cargo`, and that is a hard floor. In a managed environment where
Rust is removed as unapproved software and crate downloads are blocked at the proxy, `make
tauri-bundle` does not fail slowly or partially — it cannot begin. What *is* installable
there is a Python wheel. So there is a second host, and the two are not ranked: they answer
different questions. Tauri answers "how does someone who does not have Python run this".
This one answers "how does someone who is not allowed a compiler run this".

Both drive the same daemon and serve the same UI bytes. Nothing in `src/desktop/` changed
to make room for this, and nothing here is reachable from there.

## Why the daemon runs in this process

The Rust host spawns the daemon as a child, and roughly half of its code is about that
child — `supervisor.rs`'s restart backoff and port-file polling, the process group on
macOS, `PDEATHSIG` on Linux, the Job Object on Windows, and `proxy.rs`, which exists so
that the *host* can hold the access key and put it in an `Authorization` header that a
webview page cannot send.

None of that is the cost of being a desktop app. It is the cost of the window and the
daemon being two processes. Put them in one and the list empties:

- **No orphan control.** There is no child to outlive the window. The backends are still
  subprocesses, but they were always `supervisor.py`'s to reap, and `Gateway.stop()`
  already does it on every exit path.
- **No port file.** `serve_in_background` reads `gateway.server.port` off the object.
- **No socket proxy, and no IPC bridge.** The window loads
  `http://127.0.0.1:<port>/ui/` as an ordinary page, so it is same-origin with `/mcp` and
  `/admin` and `rpc.js`'s `browserTransport` — the default path, the one a browser has
  always taken — is the one it uses. `tauri-transport.js` finds no `__TAURI_INTERNALS__`
  and no-ops, which it was already written to do.
- **No custom URI scheme.** `panelframe.rs` and `gw_panel_url` exist because the Tauri page
  lives at `tauri://localhost` and a backend's panel does not. Here the panel is at
  `/panel/<token>` on the page's own origin, and the UI's CSP already grants
  `frame-src 'self'`.

The one thing orphan control was protecting against survives the move, and it is worth
saying why rather than assuming it. If this process is killed outright — `kill`, a crash,
a force-quit — `Session.stop` never runs and the backends are never reaped by name. They
exit anyway, because a stdio MCP server's stdin is a pipe to this process and closing it is
EOF. That is what a stdio backend is *for*, and it is the difference from the Tauri case,
where the child being supervised was a long-lived daemon that would happily keep serving on
its port with every credential in `gateway.env` loaded. Verified by hand with `kill -TERM`
against `make run-window`: no shutdown line in the log, no surviving backends.

The price is honest and worth naming: **a fatal error in the gateway takes the window with
it.** There is no supervisor to restart it, because there is nothing outside it to be one.
For a single-user desktop session that is the right trade — a window that silently
reconnects to a daemon that keeps dying is a worse thing to own than one that closes.

## Why the split into `serve_in_background` and `run`

`serve_in_background` imports no pywebview and opens no window. It is the half with
something to get wrong — a loop on a foreign thread, a bind that may refuse, a shutdown
that has to cross back — and being GUI-free is what lets `tests/test_window.py` exercise it
headless, in CI, on a runner with no display: it starts a real gateway, connects a real
WebSocket, and stops it.

`run` is the remainder: parse, import, create window, block, stop. There is nothing in it a
test could assert that reading it would not tell you faster.

## Why `webview` is imported inside a function

`pip install python-mcp-gateway` must not acquire a GUI toolkit. The daemon's most common
deployment is a container with no display, and `window.py` has to stay importable there —
`tests/test_window.py` imports it, and so does anything that enumerates the package.

So the toolkit is the `desktop` extra, the import is in `_import_webview`, and the
`ImportError` becomes one line naming the install command. A missing extra is a thing the
user can fix in five seconds, and a traceback is a poor way to tell them so.

## What was deliberately not ported

- **Remote mode.** `settings.rs`, `tunnel.rs`, `session.rs` and the Connection screen — a
  supervised `ssh -N -L` to a gateway on another host. The UI already hides that screen
  outside the Tauri shell (`app.js` removes the `data-shell-only` option), so the window
  gets the correct behaviour for free. A backend that is itself remote is unaffected: that
  is the daemon's business, not the host's.
- **First-run seeding** (`appdata.rs`). This host takes `--config` and `--env` with the
  daemon's own defaults, because it is launched from a terminal by someone who has a
  checkout, not double-clicked by someone who has neither file.
- **`PATH` repair** (`pathenv.rs`). That exists because a Finder-launched `.app` inherits
  launchd's four-entry `PATH` and every `npx` backend fails to spawn. A console script
  inherits the shell's. If this ever grows a double-clickable launcher, this comes back
  first.
- **A minted per-launch key** (`key.rs`). The bind is loopback and the page is same-origin,
  which is the case `transport_ws` already treats as authenticated-enough. A key in
  `gateway.env` is still honoured — `webui.url` puts it in the URL, and the page strips it
  from the address bar — but nothing here invents one.

## Related

- [`cli.md`](cli.md) — the daemon entrypoint. `build_gateway` lives there and is shared:
  `install_redaction` has to run before anything can log a request line, and that is not a
  decision to hold in two places.
- [`webui.md`](webui.md) — what is served at `/ui/`, and `url()`.
- [`transport_ws.md`](transport_ws.md) — `own_origins`, and why a loopback page passes.
- [`src/desktop/README.md`](../desktop/README.md) — the other host.
