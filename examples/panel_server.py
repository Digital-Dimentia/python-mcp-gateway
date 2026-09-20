#!/usr/bin/env python3
"""A backend that ships its own control surface: MCP Apps, declarative tier.

The schema zoo next door exists to be *rendered as a form* — one tool per JSON Schema
construct, so a form renderer fails visibly on whichever one it gets wrong. This exists to be
rendered as a **panel**: a small control surface the backend describes for itself, which the
admin UI draws instead of the generic form.

It is a separate file, and that is deliberate. `zoo_server.py` is vendored verbatim from
`python-acp` and its header says so; adding a panel to it would quietly make that false. A
vendored file that has been edited is worse than no example at all, because the next person
to diff it against upstream has no way to know which differences were intended.

## How a panel is addressed

Three things, and only the first is ours:

1. A tool carries `_meta: {"ui": {"resourceUri": "ui://panel/board"}}` — SEP-1865's way of
   saying "there is an interface for this".
2. That URI is an ordinary MCP resource, which this server publishes and reads like any
   other.
3. The resource's `mimeType` carries a `profile` parameter naming the tier. This server
   publishes `application/json;profile=mcp-app-declarative`, whose document is JSON.

The gateway rewrites the reference into its own address space on the way out (see
`src/mcp_gateway/ui_apps.md`), so a client resolves it with a plain `resources/read`. Nothing
here knows or cares that a gateway is in the middle.

**The `panel` authority in `ui://panel/board` means nothing.** It is a string this file chose.
The gateway never parses it, and a backend that pointed at `ui://somebodyelse/board` would
still be asked for that URI itself. Worth seeing in an example, because the alternative
reading — that the authority names a server — is the one that looks sensible and is a
security hole.

## What the panel does

A fleet of three machines, held in memory, with a reading per machine. The document shows a
table over `structuredContent`, a couple of single values, and one `action` that calls
`restart` — so the consent path has something real to guard. `describe` returns the same data
with no panel at all, which is the control: the same server, the same shape, rendered as a
form because no tool named an interface.

Pure stdlib and stdio, like everything else in `examples/`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

#: The tier a panel is written in. The `profile` parameter is SEP-1865's own, and it is what
#: lets one tool serve two tiers: a resource's `mimeType` says which tier its document is,
#: and the tool definition never changes.
PANEL_MIME = "application/json;profile=mcp-app-declarative"
HTML_PANEL_MIME = "text/html;profile=mcp-app"

#: The two panels' addresses. `panel` here is an authority this file invented; see the header.
PANEL_URI = "ui://panel/board"
HTML_PANEL_URI = "ui://panel/gauge"

#: The HTML tier, in full. Two things about it are worth copying into a real server.
#:
#: **The script is inline, and it has to be.** The gateway frames this with no
#: `allow-same-origin`, so the document's origin is opaque and `'self'` in a CSP matches
#: nothing at all -- a `<script src="./panel.js">` beside this file would be refused. That is
#: a conformance gap in the host rather than a rule of the spec, and it is written down in
#: `src/mcp_gateway/panels.md` rather than left to be discovered.
#:
#: **The port arrives; it is never asked for.** The host posts a `MessagePort` to this
#: document once it has loaded, and every message after that crosses that port. Nothing here
#: reads `event.origin`: a sandboxed document's origin is the string `"null"`, which every
#: sandboxed frame on the page shares, so it identifies nobody.
HTML_PANEL = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Fleet gauge</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 13px/1.4 system-ui, sans-serif; margin: 0; padding: 10px; }
  .bar { height: 10px; background: #8884; border-radius: 5px; overflow: hidden; }
  .bar span { display: block; height: 100%; background: #4a8; }
  .row { margin-bottom: 8px; }
  .id { font-weight: 600; }
  button { font: inherit; margin-top: 6px; }
</style>
</head>
<body>
<div id="fleet">Waiting for the host&hellip;</div>
<button id="restart" hidden>Restart the stopped one</button>
<script>
(function () {
  var port = null;
  var next = 1;
  var stopped = null;
  // The name this tool has *in the host*, learned from the host. A gateway namespaces its
  // backends' tools, so `restart` here is `<backend>__restart` there -- and the backend's
  // name is chosen by whoever wrote `servers.yaml`, not by this file. Reading it off the
  // `tool-input` we are handed is how a panel stays correct under any name.
  var prefix = '';

  function send(message) { if (port) port.postMessage(message); }
  function notify(method, params) { send({ jsonrpc: '2.0', method: method, params: params }); }

  function resize() {
    notify('ui/notifications/size-changed', { height: document.body.scrollHeight + 20 });
  }

  function draw(result) {
    var fleet = (result && result.structuredContent) || { machines: [] };
    var host = document.getElementById('fleet');
    host.textContent = '';
    fleet.machines.forEach(function (machine) {
      var row = document.createElement('div');
      row.className = 'row';
      var label = document.createElement('div');
      label.textContent = machine.id + ' \u00b7 ' + machine.state;
      label.className = 'id';
      var bar = document.createElement('div');
      bar.className = 'bar';
      var fill = document.createElement('span');
      fill.style.width = Math.round(machine.load * 100) + '%';
      bar.appendChild(fill);
      row.appendChild(label);
      row.appendChild(bar);
      host.appendChild(row);
      if (machine.state !== 'running') stopped = machine.id;
    });
    var button = document.getElementById('restart');
    button.hidden = !stopped;
    resize();
  }

  document.getElementById('restart').addEventListener('click', function () {
    if (!stopped) return;
    var id = next++;
    send({
      jsonrpc: '2.0', id: id, method: 'tools/call',
      params: { name: prefix + 'restart', arguments: { id: stopped } }
    });
  });

  function onPortMessage(event) {
    var message = event.data || {};
    if (message.method === 'ui/notifications/tool-input') {
      var name = String(message.params.name || '');
      var cut = name.lastIndexOf('__');
      prefix = cut < 0 ? '' : name.slice(0, cut + 2);
    }
    if (message.method === 'ui/notifications/tool-result') draw(message.params.result);
    if (message.result && message.result.structuredContent) draw(message.result);
  }

  // The host hands the port in; nothing here dials out. No origin is checked, because a
  // sandboxed document has none worth checking -- arrival on the port is the identity.
  window.addEventListener('message', function (event) {
    if (port || !event.ports || !event.ports.length) return;
    port = event.ports[0];
    port.onmessage = onPortMessage;
    port.start();
    send({ jsonrpc: '2.0', id: 0, method: 'ui/initialize', params: {} });
    notify('ui/notifications/initialized', {});
    resize();
  });
}());
</script>
</body>
</html>
"""

#: The fleet, such as it is. Mutable so `restart` visibly changes something.
FLEET: list[dict[str, Any]] = [
    {"id": "alpha", "role": "web", "state": "running", "load": 0.42},
    {"id": "beta", "role": "worker", "state": "running", "load": 0.91},
    {"id": "gamma", "role": "worker", "state": "stopped", "load": 0.0},
]


def fleet_snapshot() -> dict[str, Any]:
    running = [machine for machine in FLEET if machine["state"] == "running"]
    busiest = max(FLEET, key=lambda machine: machine["load"])
    return {
        "machines": [dict(machine) for machine in FLEET],
        "counts": {"total": len(FLEET), "running": len(running)},
        "busiest": busiest["id"],
    }


PANEL_DOCUMENT: dict[str, Any] = {
    "panel": 1,
    "title": "Fleet",
    "blocks": [
        {"type": "value", "label": "Running", "path": "/counts/running"},
        {"type": "value", "label": "Busiest", "path": "/busiest"},
        {
            "type": "table",
            "path": "/machines",
            "columns": [
                {"label": "Machine", "path": "/id"},
                {"label": "Role", "path": "/role"},
                {"label": "State", "path": "/state"},
                {"label": "Load", "path": "/load"},
            ],
        },
        {
            "type": "markdown",
            "text": "`restart` asks before it runs. A panel proposes; a person disposes.",
        },
        {
            "type": "action",
            "tool": "restart",
            "label": "Restart a machine",
            # An ordinary JSON Schema, rendered by the same code as the form column. An enum
            # becomes a select; the gateway's own renderer decides, not this file.
            "schema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "title": "Machine",
                        "enum": [machine["id"] for machine in FLEET],
                    },
                },
                "required": ["id"],
            },
        },
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "board",
        "description": "The fleet, with a control surface of its own.",
        "inputSchema": {"type": "object", "properties": {}},
        # The one line that makes this a panel rather than a tool result.
        "_meta": {"ui": {"resourceUri": PANEL_URI}},
    },
    {
        "name": "gauge",
        "description": "The same fleet, as SEP-1865's HTML tier -- a document this server wrote.",
        "inputSchema": {"type": "object", "properties": {}},
        # The tool definition is identical in shape to `board` above. Only the *resource* it
        # names differs, which is the whole point of the `profile` parameter.
        "_meta": {"ui": {"resourceUri": HTML_PANEL_URI}},
    },
    {
        "name": "describe",
        "description": "The same fleet, with no panel -- the control for comparison.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "restart",
        "description": "Restart one machine.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "title": "Machine"}},
            "required": ["id"],
        },
    },
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id: Any, payload: dict) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def call_tool(name: str, arguments: dict) -> dict:
    """Every tool answers with both `content` and `structuredContent`.

    The split matters here more than it usually does: a panel's pointers resolve against
    `structuredContent`, and the `content` text is what a model reads. Sending only one would
    make the server render *or* explain, never both.
    """
    if name in ("board", "gauge", "describe"):
        snapshot = fleet_snapshot()
        summary = f"{snapshot['counts']['running']} of {snapshot['counts']['total']} running"
        return {"content": [{"type": "text", "text": summary}], "structuredContent": snapshot}

    if name == "restart":
        wanted = arguments.get("id")
        for machine in FLEET:
            if machine["id"] == wanted:
                machine["state"] = "running"
                machine["load"] = 0.05
                snapshot = fleet_snapshot()
                return {
                    "content": [{"type": "text", "text": f"restarted {wanted}"}],
                    "structuredContent": snapshot,
                }
        # A tool-level failure is a successful result carrying `isError`, which is MCP's own
        # contract and what lets a model read it and adapt.
        return {
            "content": [{"type": "text", "text": f"no machine {wanted!r}"}],
            "isError": True,
        }

    raise LookupError(name)


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}, "resources": {}},
                "serverInfo": {"name": "panel-example", "version": "1.0.0"},
            },
        )
    elif method == "tools/list":
        result(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        try:
            result(request_id, call_tool(params.get("name", ""), params.get("arguments") or {}))
        except LookupError as exc:
            error(request_id, -32602, f"unknown tool {exc.args[0]!r}")
    elif method == "resources/list":
        result(
            request_id,
            {
                "resources": [
                    {"uri": PANEL_URI, "name": "Fleet panel", "mimeType": PANEL_MIME},
                    {
                        "uri": HTML_PANEL_URI,
                        "name": "Fleet gauge",
                        "mimeType": HTML_PANEL_MIME,
                        # A panel may ask for the hosts it needs. This one needs none, and
                        # says so: the gateway turns this into a real `Content-Security-Policy`
                        # on the document, and an empty list is a document that can reach
                        # nothing at all. See `src/mcp_gateway/panels.md`.
                        "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}},
                    },
                ]
            },
        )
    elif method == "resources/read":
        uri = params.get("uri")
        if uri == PANEL_URI:
            item = {"uri": uri, "mimeType": PANEL_MIME, "text": json.dumps(PANEL_DOCUMENT)}
        elif uri == HTML_PANEL_URI:
            item = {"uri": uri, "mimeType": HTML_PANEL_MIME, "text": HTML_PANEL}
        else:
            # MCP's dedicated code for "that URI is not something I can read".
            error(request_id, -32002, f"no resource at {uri!r}")
            return
        result(request_id, {"contents": [item]})
    elif method == "resources/templates/list":
        result(request_id, {"resourceTemplates": []})
    elif method == "ping":
        result(request_id, {})
    elif request_id is not None:
        error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        # A notification has no id and takes no answer; `notifications/initialized` is the
        # only one this server is sent.
        handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
