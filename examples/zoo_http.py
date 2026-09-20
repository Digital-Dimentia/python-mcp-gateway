#!/usr/bin/env python3
"""The schema zoo behind a URL: MCP's Streamable HTTP in front of an stdio server.

`zoo_server.py` speaks stdio, like most MCP servers. In a container deployment the gateway
cannot spawn a backend -- it reaches one over the network, by `url:` -- so an stdio server
needs something in front of it. This is that something, for the zoo specifically and as a
worked example of the shape generally: spawn the child once, serve its conversation over
HTTP, and let the gateway treat it like any other URL backend.

    gateway (container)  --POST http://127.0.0.1:9001/mcp-->  this  --stdio-->  zoo_server.py

Why not change `zoo_server.py` instead: it is **vendored verbatim** from `python-acp` and
says so in its own header. A fork that drifts is worth less than a wrapper that does not.

Why stdlib only: the image this runs in carries Python and nothing else (see
`Containerfile`), and the zoo itself is pure stdlib. A dependency here would be the one
thing in the deployment that needed a build step.

## What it implements, and what it deliberately does not

`src/mcp_gateway/mcp_http.md` documents the client side of this conversation, and it is the
contract this file is written against -- not the specification in the abstract, but the four
rows of that table:

| The gateway sends | This answers |
|---|---|
| `POST` carrying a request (it has an `id`) | `200`, `application/json`, the one reply |
| `POST` carrying a notification (no `id`) | `202`, no body |
| `GET`, to open a stream for unprompted messages | `405` |
| `DELETE`, ending the session | `405` |

**The `405`s are the interesting part.** That document says of the GET stream: "`405` is the
server saying it has no such stream, which is allowed and final" -- the client logs "offers
no GET stream" once and never retries. So refusing it costs nothing and buys a server with
no SSE writer, no held-open connections and no reconnect backoff to get wrong. The price is
that anything the zoo says **unprompted** has nowhere to go, and is dropped; `_reader`
below is where that happens and says so again. The zoo only speaks when spoken to, so the
price is zero here. A server that pushes `notifications/resources/updated` would need the
GET stream, and that is the point at which this file stops being the right example.

Sessions are minted on `initialize` and checked after it. An unrecognised one gets a `404`,
which is the client's cue to re-initialise rather than an error it surfaces -- the same
answer a restarted container would give. Because there is exactly one child process, a
second `initialize` replaces the session rather than adding one: this is a fixture for
verifying a deployment, not a multi-tenant server, and pretending otherwise would be a lie
told in code that nothing exercises.

## Running it

    python3 zoo_http.py                     # 127.0.0.1:9001, path /mcp
    python3 zoo_http.py --port 9100 --host 0.0.0.0

`MCP_ZOO_HTTP_HOST`, `MCP_ZOO_HTTP_PORT` and `MCP_ZOO_PATH` do the same, so a compose file
can set them without an entrypoint. The catalogue entry on the other side is three lines:

    servers:
      zoo:
        url: http://127.0.0.1:9001/mcp

No `headers:`: there is no credential here, and inventing one would suggest the transport
needs it. See `examples/remote-compose/README.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: Where `zoo_server.py` lives, relative to this file. Both are copied into the container
#: together, so "beside me" is the only assumption that survives being mounted anywhere.
ZOO = Path(__file__).resolve().parent / "zoo_server.py"

#: The zoo publishes its full catalogue only with this set, and `env_mode: curated` in
#: `servers.dev.yaml` means it is named explicitly there too. Same reason here: the child
#: inherits a deliberate environment, not this process's.
ZOO_ENV = {"MOCK_MCP_SCHEMA_ZOO": "1"}

#: How long to wait for the child's reply to one request. Long enough that a slow container
#: start is not mistaken for a hang, short enough that a wedged child fails the call instead
#: of holding the gateway's request task until its own timeout.
REPLY_TIMEOUT = 30.0


class Zoo:
    """One `zoo_server.py` child, and the mailbox that turns its stdout into replies.

    A single stdio pipe cannot interleave, so every write is serialised. Reads are not:
    a background thread owns stdout and hands each reply to whoever is waiting for that id,
    which keeps a slow call from blocking the answer to a fast one behind it.
    """

    def __init__(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, str(ZOO)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # straight to ours, so `docker compose logs` shows the zoo's own
            env={**os.environ, **ZOO_ENV},
            text=True,
            bufsize=1,
        )
        self._write_lock = threading.Lock()
        self._waiting: dict[Any, queue.Queue] = {}
        self._waiting_lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        """Deliver each line the zoo writes to whoever asked for it, and drop the rest.

        The "rest" is anything the zoo says unprompted. With no GET stream there is nowhere
        to deliver it, so it is dropped on purpose rather than queued for a reader that will
        never come. See the module docstring.
        """
        for line in self._proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(f"zoo_http: unparseable line from the zoo: {line[:200]}", file=sys.stderr)
                continue
            with self._waiting_lock:
                mailbox = self._waiting.pop(message.get("id"), None)
            if mailbox is not None:
                mailbox.put(message)

    def request(self, payload: dict) -> dict:
        """Send one request and wait for the reply with its id."""
        mailbox: queue.Queue = queue.Queue(maxsize=1)
        with self._waiting_lock:
            self._waiting[payload["id"]] = mailbox
        try:
            self._send(payload)
            return mailbox.get(timeout=REPLY_TIMEOUT)
        except queue.Empty:
            raise TimeoutError(f"the zoo did not answer {payload.get('method')} in {REPLY_TIMEOUT}s")
        finally:
            with self._waiting_lock:
                self._waiting.pop(payload["id"], None)

    def notify(self, payload: dict) -> None:
        """Send one notification. There is nothing to wait for, by definition."""
        self._send(payload)

    def _send(self, payload: dict) -> None:
        if self._proc.poll() is not None:
            raise BrokenPipeError(f"the zoo exited with {self._proc.returncode}")
        with self._write_lock:
            self._proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]

    def stop(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class Handler(BaseHTTPRequestHandler):
    """The four rows of the table in the module docstring, and nothing else."""

    protocol_version = "HTTP/1.1"  # so Content-Length framing is honoured and connections reused

    zoo: Zoo
    path_name: str
    session: str | None = None

    # --- the three methods MCP uses -------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler's spelling)
        if self.path != self.path_name:
            return self._plain(404, "no such path")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._plain(400, "bad Content-Length")
        try:
            payload = json.loads(self.rfile.read(length) or "null")
        except json.JSONDecodeError:
            return self._rpc_error(None, -32700, "parse error")
        if not isinstance(payload, dict):
            # 2025-06-18 removed batching, and the gateway sends one message per POST.
            return self._rpc_error(None, -32600, "one JSON-RPC object per request")

        method = payload.get("method")
        if method != "initialize" and not self._session_ok():
            # Not an error the caller should see: it is the client's cue to re-initialise.
            return self._plain(404, "unknown session")

        try:
            if "id" not in payload:
                type(self).zoo.notify(payload)
                return self._empty(202)
            reply = type(self).zoo.request(payload)
        except (BrokenPipeError, TimeoutError) as exc:
            return self._rpc_error(payload.get("id"), -32603, str(exc))

        extra = {}
        if method == "initialize" and "result" in reply:
            # A new session replaces the old one: one child, one conversation.
            type(self).session = secrets.token_urlsafe(24)
            extra["Mcp-Session-Id"] = type(self).session
        self._json(200, reply, extra)

    def do_GET(self) -> None:  # noqa: N802
        """No unprompted stream. Allowed, and final -- see the module docstring."""
        self._plain(405, "no GET stream")

    def do_DELETE(self) -> None:  # noqa: N802
        """Ending a session is a courtesy; the child outlives it either way."""
        self._plain(405, "sessions are not ended here")

    # --- helpers ---------------------------------------------------------------------

    def _session_ok(self) -> bool:
        offered = self.headers.get("Mcp-Session-Id")
        # Lenient when the client sends none: the transport doc says a client carries one
        # only "when the server minted one", and refusing would break the first POST after
        # a restart of *this* process for no gain.
        return offered is None or offered == type(self).session

    def _json(self, status: int, body: dict, extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _rpc_error(self, request_id: Any, code: int, message: str) -> None:
        self._json(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _plain(self, status: int, message: str) -> None:
        raw = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        """One line per request on stderr, beside the zoo's own, rather than stdout."""
        print(f"zoo_http: {self.address_string()} {fmt % args}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("MCP_ZOO_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_ZOO_HTTP_PORT", "9001")))
    parser.add_argument("--path", default=os.environ.get("MCP_ZOO_PATH", "/mcp"))
    args = parser.parse_args()

    if not ZOO.exists():
        print(f"zoo_http: no zoo_server.py beside me at {ZOO}", file=sys.stderr)
        return 1

    Handler.zoo = Zoo()
    Handler.path_name = args.path
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"zoo_http: MCP on http://{args.host}:{args.port}{args.path} (zoo pid {Handler.zoo._proc.pid})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.zoo.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
