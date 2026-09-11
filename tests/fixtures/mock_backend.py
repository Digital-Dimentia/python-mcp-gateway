"""A stdlib-only MCP server, driven entirely by environment variables.

Run as a subprocess by the tests, exactly the way a real backend is. **Not a mock object**:
a mock proves nothing about a subprocess's lifetime, its environment, or what happens when
it dies mid-call, and those are the three things this project has to get right.

Every behaviour is an env-var knob, because that is also how the gateway configures a real
backend -- so a test that wants a misbehaving server configures it through the same
mechanism it is testing.

## Knobs

| Variable | Effect |
|---|---|
| `MOCK_NAME` | Included in `echo` output, so two instances are distinguishable |
| `MOCK_TOOLS` | Comma-separated tool names (default `echo`). Two servers sharing one name is the collision case |
| `MOCK_TOOL_SEP` | Also publish a tool literally named `create__issue` |
| `MOCK_LONG_TOOL_NAME` | Also publish a 70-character tool name, over the composed-name ceiling |
| `MOCK_REQUIRE_ENV` | Exit 1 at startup unless that variable is set |
| `MOCK_ENV_REPORT` | Publish `env-report`, returning the KEY NAMES of this process's environment |
| `MOCK_SLOW_START_MS` | Sleep this long before answering `initialize` |
| `MOCK_CRASH_ON_CALL` | `os._exit(1)` when that tool is called |
| `MOCK_ERROR_ON_CALL` | Answer that tool with a JSON-RPC error `-32042` carrying `data` |
| `MOCK_ISERROR_ON_CALL` | Answer that tool with a successful result carrying `isError: true` |
| `MOCK_STALL_ON_CALL` | Never answer that tool; record the cancellation for `cancel-report` |
| `MOCK_CAPABILITIES` | Comma-separated subset of `tools,prompts,resources` (default: all) |
| `MOCK_RESOURCES` | Comma-separated concrete resource URIs |
| `MOCK_TEMPLATES` | Comma-separated `uriTemplate` values |
| `MOCK_PROMPTS` | Comma-separated prompt names |
| `MOCK_LIST_CHANGED_AFTER_MS` | Send an unprompted `tools/list_changed` after this delay |
| `MOCK_ASK_ROOTS` | Send `roots/list` to the client during a tool call and return the answer |
| `MOCK_ASK_ELICIT` | Send `elicitation/create` during a tool call and return the answer |
| `MOCK_STDERR_SECRET` | Write this value to stderr at startup -- the redaction proof |
| `MOCK_IGNORE_EOF` | Do not exit when stdin closes, forcing the SIGTERM escalation |
| `MOCK_IGNORE_SIGTERM` | Ignore SIGTERM, forcing the SIGKILL escalation |
| `MOCK_PROTOCOL_VERSION` | Counter-offer this version regardless of what was proposed |
| `MOCK_OMIT_PROTOCOL_VERSION` | Leave `protocolVersion` out of the initialize result |
| `MOCK_LIST_PAGES` | Serve this many `tools/list` pages via `nextCursor` |
| `MOCK_LIST_STUCK` | Always hand back the same `nextCursor`, forever |
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

PROTOCOL_VERSION = "2025-06-18"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _flag(name: str) -> bool:
    return os.environ.get(name) == "1"


def _csv(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [part for part in (piece.strip() for piece in raw.split(",")) if part]


NAME = _env("MOCK_NAME", "mock")
TOOLS = _csv("MOCK_TOOLS", "echo")
CAPABILITIES = _csv("MOCK_CAPABILITIES", "tools,prompts,resources")
RESOURCES = _csv("MOCK_RESOURCES")
TEMPLATES = _csv("MOCK_TEMPLATES")
PROMPTS = _csv("MOCK_PROMPTS", "greet")
CRASH_ON = _env("MOCK_CRASH_ON_CALL")
ERROR_ON = _env("MOCK_ERROR_ON_CALL")
ISERROR_ON = _env("MOCK_ISERROR_ON_CALL")
STALL_ON = _env("MOCK_STALL_ON_CALL")
LIST_PAGES = int(_env("MOCK_LIST_PAGES", "1"))
LIST_STUCK = _flag("MOCK_LIST_STUCK")

# Startup-time behaviour, before a single message is read.
if _flag("MOCK_IGNORE_SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

if required := _env("MOCK_REQUIRE_ENV"):
    if required not in os.environ:
        sys.stderr.write(f"{NAME}: {required} is not set; refusing to start\n")
        sys.exit(1)

if secret := _env("MOCK_STDERR_SECRET"):
    # The redaction proof. A real server does this on a 401, helpfully echoing the
    # credential it just tried.
    sys.stderr.write(f"{NAME}: auth failed for token {secret}\n")
    sys.stderr.flush()

_write_lock = threading.Lock()
#: Request ids for `MOCK_STALL_ON_CALL` that were received and deliberately not answered.
_stalled: set = set()
#: Request ids this server was told to forget, so `cancel-report` can prove the
#: cancellation reached it rather than merely being sent.
_cancelled: list[int] = []
#: Answers to our own outbound requests, keyed by id.
_answers: dict[int, object] = {}
_next_outbound_id = 1000


def send(message: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def result(request_id, payload: dict) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id, code: int, message: str, data=None) -> None:
    body = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    send({"jsonrpc": "2.0", "id": request_id, "error": body})


def ask_client(method: str, params: dict, timeout: float = 5.0):
    """Send a server-to-client request and wait for its answer.

    The read loop resolves it into `_answers`; polling is fine here because this fixture
    is single-threaded apart from the one timer below, and a condition variable would be
    more machinery than the fixture earns.
    """
    global _next_outbound_id
    with _write_lock:
        _next_outbound_id += 1
        request_id = _next_outbound_id
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if request_id in _answers:
            return _answers.pop(request_id)
        time.sleep(0.005)
    return {"timeout": True}


def tool_names() -> list[str]:
    names = list(TOOLS)
    if _flag("MOCK_TOOL_SEP"):
        names.append("create__issue")
    if _flag("MOCK_LONG_TOOL_NAME"):
        names.append("l" + "o" * 68 + "ng")
    if _flag("MOCK_ENV_REPORT"):
        names.append("env-report")
    if STALL_ON:
        names.append("cancel-report")
    return names


def tool_definitions(page: int) -> list[dict]:
    if page > 0:
        return [{"name": f"page{page}-tool", "description": "", "inputSchema": {"type": "object"}}]
    return [
        {
            "name": name,
            "description": f"{name} on {NAME}",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        }
        for name in tool_names()
    ]


def capabilities() -> dict:
    block: dict[str, dict] = {}
    if "tools" in CAPABILITIES:
        block["tools"] = {"listChanged": True}
    if "prompts" in CAPABILITIES:
        block["prompts"] = {"listChanged": True}
    if "resources" in CAPABILITIES:
        block["resources"] = {"listChanged": True}
    return block


def handle_initialize(request_id, params: dict) -> None:
    if delay := int(_env("MOCK_SLOW_START_MS", "0")):
        time.sleep(delay / 1000.0)
    payload: dict = {
        "capabilities": capabilities(),
        "serverInfo": {"name": NAME, "version": "0.0.1"},
    }
    if not _flag("MOCK_OMIT_PROTOCOL_VERSION"):
        countered = _env("MOCK_PROTOCOL_VERSION")
        payload["protocolVersion"] = countered or params.get("protocolVersion", PROTOCOL_VERSION)
    result(request_id, payload)

    if after := int(_env("MOCK_LIST_CHANGED_AFTER_MS", "0")):
        timer = threading.Timer(
            after / 1000.0,
            lambda: send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}),
        )
        timer.daemon = True
        timer.start()


def handle_tools_list(request_id, params: dict) -> None:
    cursor = params.get("cursor")
    page = 0 if cursor is None else int(cursor)
    payload: dict = {"tools": tool_definitions(page)}
    if LIST_STUCK:
        payload["nextCursor"] = "stuck"
    elif page + 1 < LIST_PAGES:
        payload["nextCursor"] = str(page + 1)
    result(request_id, payload)


def handle_tools_call(request_id, params: dict) -> None:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}

    if name and name == CRASH_ON:
        # Not sys.exit: a clean exit would flush and close, which is a different failure
        # from a backend that dies mid-call.
        os._exit(1)
    if name and name == ERROR_ON:
        error(request_id, -32042, f"{name} refused by {NAME}", {"tool": name})
        return
    if name and name == ISERROR_ON:
        result(
            request_id,
            {"content": [{"type": "text", "text": f"{name} failed"}], "isError": True},
        )
        return
    if name and name == STALL_ON:
        _stalled.add(request_id)
        return  # deliberately never answered
    if name == "cancel-report":
        result(request_id, {"content": [{"type": "text", "text": json.dumps(_cancelled)}]})
        return
    if name == "env-report":
        result(
            request_id,
            {"content": [{"type": "text", "text": json.dumps(sorted(os.environ))}]},
        )
        return
    if name == "ask":
        answers = {}
        if _flag("MOCK_ASK_ROOTS"):
            answers["roots"] = ask_client("roots/list", {})
        if _flag("MOCK_ASK_ELICIT"):
            answers["elicit"] = ask_client(
                "elicitation/create",
                {"message": "how many?", "requestedSchema": {"type": "object"}},
            )
        result(request_id, {"content": [{"type": "text", "text": json.dumps(answers)}]})
        return

    if name not in tool_names():
        error(request_id, -32602, f"{NAME} has no tool {name!r}")
        return
    result(
        request_id,
        {"content": [{"type": "text", "text": f"{NAME}:{name}:{arguments.get('text', '')}"}]},
    )


def handle_prompts_list(request_id, _params: dict) -> None:
    result(request_id, {"prompts": [{"name": p, "description": f"{p} on {NAME}"} for p in PROMPTS]})


def handle_prompts_get(request_id, params: dict) -> None:
    name = params.get("name", "")
    if name not in PROMPTS:
        error(request_id, -32602, f"{NAME} has no prompt {name!r}")
        return
    result(
        request_id,
        {
            "description": f"{name} on {NAME}",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": f"{NAME} says {name}"}}
            ],
        },
    )


def handle_resources_list(request_id, _params: dict) -> None:
    result(
        request_id,
        {"resources": [{"uri": uri, "name": uri.rsplit("/", 1)[-1] or uri} for uri in RESOURCES]},
    )


def handle_resources_templates_list(request_id, _params: dict) -> None:
    result(
        request_id,
        {"resourceTemplates": [{"uriTemplate": t, "name": f"{NAME} template"} for t in TEMPLATES]},
    )


def handle_resources_read(request_id, params: dict) -> None:
    uri = params.get("uri", "")
    if uri not in RESOURCES:
        error(request_id, -32002, f"{NAME} cannot read {uri!r}")
        return
    result(request_id, {"contents": [{"uri": uri, "mimeType": "text/plain", "text": f"{NAME}:{uri}"}]})


HANDLERS = {
    "initialize": handle_initialize,
    "ping": lambda request_id, _params: result(request_id, {}),
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "prompts/list": handle_prompts_list,
    "prompts/get": handle_prompts_get,
    "resources/list": handle_resources_list,
    "resources/templates/list": handle_resources_templates_list,
    "resources/read": handle_resources_read,
}


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")

    if method is None and request_id is not None:
        # A response to one of our own outbound requests.
        _answers[request_id] = message.get("result", message.get("error"))
        return

    if method == "notifications/cancelled":
        cancelled_id = (message.get("params") or {}).get("requestId")
        if cancelled_id is not None:
            _cancelled.append(cancelled_id)
            _stalled.discard(cancelled_id)
        return

    if request_id is None:
        return  # any other notification: nothing to answer

    handler = HANDLERS.get(method)
    if handler is None:
        error(request_id, -32601, f"{NAME} does not implement {method!r}")
        return

    # Each request runs in its own thread, mirroring the one-task-per-request rule a real
    # server follows. Without it a handler that waits on the client -- `ask` does, and so
    # does any real `elicitation/create` -- would block this process's only reader, so the
    # very answer it is waiting for could never be read. That is a deadlock the fixture
    # would otherwise present as a timeout, which is the wrong lesson entirely.
    thread = threading.Thread(
        target=handler, args=(request_id, message.get("params") or {}), daemon=True
    )
    thread.start()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            handle(message)

    # stdin reached EOF: the MCP shutdown sequence says exit now.
    if _flag("MOCK_IGNORE_EOF"):
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
