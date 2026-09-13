# `portfile.py`

Writes the bound port where a supervisor can read it, and takes it away on the way out.

`--port 0` is how you avoid a port conflict, and it means nobody knows the port until the
socket exists. Something has to publish it.

## What this replaced

The desktop shell used to learn the port by parsing the daemon's own startup line off
stderr:

```text
listening on ws://127.0.0.1:49613/mcp
```

It worked, and it was pinned from both sides so it was safe. What it was not is *free*: a
sentence written for a human had become a wire format, and could never be reworded,
re-levelled or reordered again. `parse_listening` existed twice, once in Python and once in
Rust, and the two had to be kept in step by a test that checked they mentioned each other.
See python-mcp-gateway-9p3.

## Why a file rather than a better log line

Two facts have to cross the boundary, and a file carries both where a line carries one.

The number is the obvious half. The other is **readiness**: the file does not exist until
the socket is bound, so its appearance *is* the signal. A supervisor polling for it needs no
agreement about wording, no parser, and no assumption about which line comes first. A log
line can only ever be the number, with readiness inferred from having seen it.

It also generalises past the one caller that prompted it. Anything starting this daemon on
`--port 0` — a test harness, a systemd unit, a script — has the same problem, and
`--port-file` is the answer daemons have been giving to it for decades. That is what settled
the bead's open question: the flag is not surface added for one consumer.

## Why the write is atomic

A supervisor polling for the file will open it the instant it appears. A plain write can be
observed after `open` and before the bytes land, which hands the reader an empty file to
parse as a port number.

So the content goes to a temporary file **in the same directory** and is renamed into place.
`rename` is atomic within one filesystem, so the path either does not exist or has the whole
number in it, and there is no third state to trip on. The same directory matters: the system
temp dir is frequently a different filesystem, where `rename` degrades to a copy.

## What is in it

The port in decimal, one line, and nothing else.

Not JSON. Everything else a reader might want — the host, the pid — it either already knows
(it chose the host, it spawned the process) or has a better source for. A format with one
field cannot be misparsed and cannot grow a compatibility question.

## Failures are not fatal

`write` logs and swallows. The daemon is bound and serving by the time it is called, and
refusing to run because a convenience file could not be written would take a working gateway
down over its own bookkeeping. A supervisor waiting on the file times out and says so, which
is the right place for that failure to surface.

`read` is tolerant for the mirror-image reason: a reader polling this file races the writer
on any platform where the writer did not use `rename`, and "not a port yet" is a state to
keep waiting through rather than an error to report.

## A stale file is possible

`remove` runs on every exit path that gets the chance. The ones it cannot cover are
`SIGKILL` and the power going out. So a reader should treat the contents as a hint to be
confirmed by connecting, and a supervisor that spawns the daemon should delete any existing
file *before* spawning — an old number may name a port something else now holds.
`desktop/src-tauri/src/supervisor.rs` does both.
