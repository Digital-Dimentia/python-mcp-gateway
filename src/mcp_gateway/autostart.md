# `autostart.py`

Start a daemon for a bridge that found nothing listening, and take it away again.

```
IDE ──stdio──► mcp-gateway-connect ──ws──► mcp-gateway ──stdio──► backends
                      │                         ▲
                      └──── starts, if the ─────┘
                            first connect is refused
```

## The exception this module is

[`bridge.md`](bridge.md) argues that the bridge is correct for every revision of MCP forever
*because* it has no opinions: it never parses a method, and it manages no lifecycle. This is
the one deliberate exception, and it lives in its own file so that it stays an exception
rather than becoming a habit. `bridge.py`'s share of it is three lines — resolve a `Daemon`,
call `start()` after a failed attempt, call `stop()` in a `finally`.

**Why an exception was worth making.** A stdio-only IDE spawns one process and gives you
nowhere to say "and also run the daemon". Without this, the daemon has to be alive by some
other arrangement — `make run`, a launchd job, a systemd unit — and when it is not, the bridge
retries forever while the IDE shows a server that never answers and the explanation sits on a
stderr that many IDEs do not show. The failure is invisible in exactly the place a new user
meets it.

## The port is the lock

Two IDEs open, two bridges, both find nothing listening, both start a daemon. Only one can
bind; the loser exits on `address already in use`, and the bridge that started it connects to
the winner on its next retry — using the backoff loop that was already there for daemon
restarts.

So there is no lockfile, no pidfile and no pid in a portfile, because there is nothing left
for them to arbitrate. This is also why `Daemon` takes a host and a port instead of finding
them: the URL named them, and [`portfile.py`](portfile.md) exists for the opposite problem,
`--port 0`, where nobody can know the port until the socket is bound.

## What it refuses, and why each is a refusal rather than a default

| Given | Why not |
|---|---|
| `wss://…` | Serving TLS needs a certificate and a key that nothing here can choose. A `wss://` URL is someone else's daemon by construction. |
| A non-loopback host | `ws://gateway.lan:8765` names a daemon on another machine. Binding a local port would not make this process into the thing the URL meant. |
| No port in the URL | `ws://127.0.0.1/mcp` means port 80. Binding that needs root, and a URL with no port is far more likely a typo than a request. |
| No `--config` and no `$MCP_GATEWAY_CONFIG` | The daemon's own default is `./servers.yaml`, and a bridge runs in whatever directory its client chose — so inheriting that default starts a daemon against a config nobody picked, or fails in a directory that has none. [`bridge.md`](bridge.md) makes the same argument about guessing at `./gateway.env`. |

Every one of them logs what to pass instead. A feature that silently does nothing is worse
than a feature that is not there, because the second is at least something you can look up.

## Teardown

`SIGTERM` to the daemon **process**, not to its group, then `SIGKILL` if it is still there
after five seconds. The daemon has its own handler and its own shutdown ladder for the
backends it owns ([`cli.md`](cli.md), [`mcp_stdio.md`](mcp_stdio.md)); signalling the group
would reach those backends around it, which is precisely what the daemon's teardown exists
to do properly.

**The child is not put in a new session**, which is the opposite of what a daemoniser would
do, and deliberate. Sharing the bridge's process group means whatever kills the bridge takes
the daemon with it — including a `SIGKILL` this code never gets to react to. The promise that
nothing is left running that nobody started then holds even in the case where none of the
code above gets to run.

**It stops on the way out even though another client may be attached.** That client's bridge
sees a dropped socket, waits out the backoff, and reconnects — starting a daemon itself if it
also has `--autostart`. The cost is a blip for the other client, paid by machinery that
already existed; the alternative is a daemon running with every backend and credential loaded
that nobody started deliberately and nothing will ever stop.

## Starting at most once

`start()` spawns on the first call and returns `False` forever after. A daemon that exits
immediately is usually the one that lost the bind race, and respawning into that is a fork
bomb wearing a retry loop. One that exits for another reason has already said why on the
stderr it inherited, and saying it again on a timer would not add anything.

## stdout goes to the void

The bridge's stdout is the protocol wire, so a child that inherited it could desynchronise the
client with one stray byte. It gets `DEVNULL`, and nothing is lost: `configure_logging` sends
every daemon diagnostic to stderr, which the child *does* inherit, so an IDE that shows its
server's stderr shows why a daemon failed to start.

The access key goes to the child in its environment and never in argv, which is world-readable
through `ps` — the same rule [`run`](../../Makefile) follows. With no key, nothing is set and
the daemon runs keyless, which is what a keyless bridge needs if the two are to agree about
whether there is a key at all.

## `--no-reconnect`

The two are compatible, and starting a daemon suspends `--no-reconnect` until that daemon
answers or a deadline passes (`_AUTOSTART_READY_SECONDS` in [`bridge.py`](bridge.md)). One
retry would not do: a cold start loads the config, spawns every backend and only *then* binds
the socket, so on a loaded machine the daemon is seconds away rather than milliseconds, and
giving up early turns `--autostart` into a coin toss.

Once something answers, the flag means what it says again — a later disconnection ends the
bridge.

**It waits on a clock rather than on the child, which looks like the weaker test and is not.**
The obvious refinement is to stop as soon as the child process exits, since that is what losing
the bind race looks like. But the daemon that *won* does not accept connections until it has
spawned every backend, so there is a window where our child is already gone, the winner is not
listening yet, and the connect attempt still fails — and a bridge that gave up there would
fail precisely when two editors opened at once, which is the case this design is supposed to
handle for free. The clock cannot make that mistake. The cost is that a daemon which is broken
rather than merely slow is waited out; it is not silent while that happens, because it inherits
stderr and says why immediately.
