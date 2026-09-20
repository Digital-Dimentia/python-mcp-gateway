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
| `MOCK_COMPLETIONS` | Declare `completions` and answer `completion/complete`; the values echo the ref, the argument, and one `ctx:<k>=<v>` per `context.arguments` entry received |
| `MOCK_COMPLETIONS_ERROR` | Declare `completions` and answer `completion/complete` with `-32601` |
| `MOCK_COMPLETIONS_MANY` | Answer with 120 values plus `total`/`hasMore`, past the spec's 100 cap |
| `MOCK_RESOURCES` | Comma-separated concrete resource URIs |
| `MOCK_SUBSCRIBE` | Declare `resources.subscribe` and answer subscribe/unsubscribe |
| `MOCK_LOGGING` | Declare `logging`, answer `logging/setLevel`, and publish a `log` tool that emits one `notifications/message` at a requested level |
| `MOCK_UPDATE_ON_SUBSCRIBE` | On a subscribe, send `notifications/resources/updated` for that URI after 50ms |
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
| `MOCK_UI_TOOL` | Give that tool a `_meta.ui.resourceUri` pointing at `MOCK_UI_REF` |
| `MOCK_UI_REF` | What that reference says (default `ui://<name>/panel`). Set it to another server's `ui://` for the cross-backend case |
| `MOCK_UI_FLAT` | Use the deprecated flat `_meta["ui/resourceUri"]` spelling instead |
| `MOCK_UI_BOTH` | Send both spellings, disagreeing, to pin which one wins |
| `MOCK_UI_CSP` | A raw JSON object for `_meta.ui.csp` on the resource, so hostile values are expressible |
| `MOCK_UI_READ_CSP` | The same, on the *content item* a read returns -- which the spec says overrides the listing |
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
COMPLETIONS = _flag("MOCK_COMPLETIONS")
COMPLETIONS_ERROR = _flag("MOCK_COMPLETIONS_ERROR")
COMPLETIONS_MANY = _flag("MOCK_COMPLETIONS_MANY")
RESOURCES = _csv("MOCK_RESOURCES")
SUBSCRIBE = _flag("MOCK_SUBSCRIBE")
LOGGING = _flag("MOCK_LOGGING")
UPDATE_ON_SUBSCRIBE = _flag("MOCK_UPDATE_ON_SUBSCRIBE")
TEMPLATES = _csv("MOCK_TEMPLATES")
PROMPTS = _csv("MOCK_PROMPTS", "greet")
CRASH_ON = _env("MOCK_CRASH_ON_CALL")
ERROR_ON = _env("MOCK_ERROR_ON_CALL")
ISERROR_ON = _env("MOCK_ISERROR_ON_CALL")
STALL_ON = _env("MOCK_STALL_ON_CALL")
LIST_PAGES = int(_env("MOCK_LIST_PAGES", "1"))
LIST_STUCK = _flag("MOCK_LIST_STUCK")
UI_TOOL = _env("MOCK_UI_TOOL", "")
UI_REF = _env("MOCK_UI_REF", "")
UI_FLAT = _flag("MOCK_UI_FLAT")
UI_BOTH = _flag("MOCK_UI_BOTH")
UI_CSP = _env("MOCK_UI_CSP", "")
UI_READ_CSP = _env("MOCK_UI_READ_CSP", "")

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
    if LOGGING:
        names.append("log")
    return names


def tool_definitions(page: int) -> list[dict]:
    if page > 0:
        return [{"name": f"page{page}-tool", "description": "", "inputSchema": {"type": "object"}}]
    definitions = [
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
    for definition in definitions:
        if definition["name"] == UI_TOOL:
            definition["_meta"] = _ui_meta()
    return definitions


def _ui_meta() -> dict:
    """The MCP Apps reference, in whichever spelling this run is exercising."""
    reference = UI_REF or f"ui://{NAME}/panel"
    meta: dict = {}
    if UI_BOTH:
        meta["ui"] = {"resourceUri": reference}
        meta["ui/resourceUri"] = reference + "-flat"
    elif UI_FLAT:
        meta["ui/resourceUri"] = reference
    else:
        meta["ui"] = {"resourceUri": reference}
    return meta


def capabilities() -> dict:
    block: dict[str, dict] = {}
    if "tools" in CAPABILITIES:
        block["tools"] = {"listChanged": True}
    if "prompts" in CAPABILITIES:
        block["prompts"] = {"listChanged": True}
    if "resources" in CAPABILITIES:
        block["resources"] = {"listChanged": True}
        if SUBSCRIBE or UPDATE_ON_SUBSCRIBE:
            block["resources"]["subscribe"] = True
    if LOGGING:
        block["logging"] = {}
    if "completions" in CAPABILITIES:
        # No options to set, which is the commonest capability shape there is -- and the
        # one a gateway reading `bool(block)` rather than presence gets wrong.
        block["completions"] = {}
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

    if name == "log" and LOGGING:
        # Emitting on demand rather than on a timer: a test needs the message to arrive
        # *after* it has set a level, and a timer cannot promise that ordering.
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {
                    "level": arguments.get("level", "info"),
                    "logger": arguments.get("logger") or None,
                    "data": arguments.get("text", f"{NAME} says something"),
                },
            }
        )
        result(request_id, {"content": [{"type": "text", "text": f"level={_log_level}"}]})
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
    resources = []
    for uri in RESOURCES:
        entry = {"uri": uri, "name": uri.rsplit("/", 1)[-1] or uri}
        if UI_CSP and uri.startswith("ui://"):
            # Raw JSON, not a structured knob, so a test can express a hostile csp block --
            # a source list carrying a `;`, a non-list directive -- that a typed knob would
            # quietly make well-formed before the gateway ever saw it.
            entry["mimeType"] = "application/json;profile=mcp-app-declarative"
            entry["_meta"] = {"ui": {"csp": json.loads(UI_CSP)}}
        resources.append(entry)
    result(request_id, {"resources": resources})


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
    item = {"uri": uri, "mimeType": "text/plain", "text": f"{NAME}:{uri}"}
    if UI_READ_CSP and uri.startswith("ui://"):
        item["mimeType"] = "application/json;profile=mcp-app-declarative"
        item["_meta"] = {"ui": {"csp": json.loads(UI_READ_CSP)}}
    result(request_id, {"contents": [item]})


def handle_resources_subscribe(request_id, params: dict) -> None:
    """Accept a subscription, and optionally prove it by firing one update.

    The update carries the backend's *own* URI, which is the whole point of the test: what
    reaches the client has to have been rewritten on the way through, and the only way to
    see that is for this end to have never said the public form.
    """
    uri = params.get("uri", "")
    if uri not in RESOURCES:
        error(request_id, -32002, f"{NAME} cannot subscribe to {uri!r}")
        return
    _subscribed.add(uri)
    result(request_id, {})
    if UPDATE_ON_SUBSCRIBE:
        timer = threading.Timer(
            0.05,
            lambda: send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/updated",
                    "params": {"uri": uri},
                }
            ),
        )
        timer.daemon = True
        timer.start()


def handle_resources_unsubscribe(request_id, params: dict) -> None:
    _subscribed.discard(params.get("uri", ""))
    result(request_id, {})


def handle_logging_set_level(request_id, params: dict) -> None:
    """Remember the level. The `log` tool then reports it, which is how a test sees that a
    `logging/setLevel` really reached *this* process rather than stopping at the gateway."""
    global _log_level
    _log_level = params.get("level", "")
    result(request_id, {})


def handle_completion_complete(request_id, params: dict) -> None:
    """Echo the request back as values, which is what makes forwarding observable.

    A completion result carries only strings, so the only way for a test to see what
    actually reached this process is to answer *with* it. The `ctx:` values are the point:
    they appear only when a `context.arguments` really arrived, so both forwarding it and
    stripping it for a `2024-11-05` backend are visible from the far end without anybody
    spying on the wire.
    """
    if COMPLETIONS_ERROR:
        error(request_id, -32601, f"{NAME} does not implement completions after all")
        return
    ref = params.get("ref") or {}
    argument = params.get("argument") or {}
    if COMPLETIONS_MANY:
        values = [f"{NAME}-{n:03d}" for n in range(120)]
        # The spec caps `values` at 100 and says what the rest were with `total`/`hasMore`.
        result(request_id, {"completion": {"values": values[:100], "total": len(values), "hasMore": True}})
        return
    context = (params.get("context") or {}).get("arguments") or {}
    values = [
        f"ref:{ref.get('type')}",
        f"named:{ref.get('name') or ref.get('uri')}",
        f"arg:{argument.get('name')}={argument.get('value')}",
        *[f"ctx:{key}={value}" for key, value in sorted(context.items())],
    ]
    result(request_id, {"completion": {"values": values, "total": len(values), "hasMore": False}})


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

#: What this process believes it is subscribed to. Not reported anywhere: what a test can
#: observe is the `notifications/resources/updated` a subscribe provokes, which is also the
#: only thing a real client can observe.
_subscribed: set[str] = set()

if SUBSCRIBE or UPDATE_ON_SUBSCRIBE:
    HANDLERS["resources/subscribe"] = handle_resources_subscribe
    HANDLERS["resources/unsubscribe"] = handle_resources_unsubscribe

#: The level this process was last told to use, `""` until it is told. Reported by the `log`
#: tool, so a test can tell a level that arrived from one that stopped at the gateway.
_log_level = ""

if LOGGING:
    HANDLERS["logging/setLevel"] = handle_logging_set_level

if COMPLETIONS or COMPLETIONS_ERROR or COMPLETIONS_MANY:
    HANDLERS["completion/complete"] = handle_completion_complete
    CAPABILITIES.append("completions")


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
