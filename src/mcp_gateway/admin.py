"""The gateway's own meta-tools, and the `/admin` method table they share an implementation with.

One implementation, two surfaces:

- **`gateway__*` MCP tools on `/mcp`**, so the model can diagnose its own missing tools --
  "GitHub is not configured" is an answer a model can give a human, where a silent absence
  is not.
- **`admin.*` JSON-RPC methods on `/admin`**, so the admin UI never has to speak MCP to
  ask which backends are up.

They are always present, even with zero live backends. That is why `protocol.py` advertises
`tools` unconditionally: there is always at least this much to list.

## What is deliberately not here

There is **no `gateway__set_secret`, and no `admin.secrets.set`.** Writing a credential from
a tool call means a model can be talked into writing one, and the blast radius of this
particular process is every credential on the machine. The admin UI did not change that: it
added `admin.config.*` and `admin.backend.*` on `/admin`, a surface the model cannot reach,
and nothing anywhere that writes a value.

What the UI got instead is `admin.secrets.missing` -- the *names* an enabled server needs
and the store does not have. Naming the key is what makes "you add it to gateway.env
yourself" a workable answer rather than a dead end.

`admin.secrets.keys` returns key *names*. `gateway__list_backends` reports `env_keys`, also
names. `tests/test_admin_tools.py` asserts that no known secret value appears anywhere in
either payload.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from mcp_gateway import errors, naming
from mcp_gateway.backend import Backend
from mcp_gateway.secrets import missing_for

logger = logging.getLogger(__name__)

#: The namespace the meta-tools live in. `naming.RESERVED_SERVER_NAMES` refuses it as a
#: backend name, so a backend cannot publish a tool that shadows one of these.
ADMIN_PREFIX = "gateway"

_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _tool(name: str, description: str, schema: dict | None = None) -> dict[str, Any]:
    return {
        "name": naming.compose(ADMIN_PREFIX, name),
        "description": description,
        "inputSchema": schema or _EMPTY_SCHEMA,
    }


def tool_definitions() -> list[dict[str, Any]]:
    """What `tools/list` publishes for the gateway itself."""
    return [
        _tool(
            "list_backends",
            "List every configured backend MCP server, whether it is running, and why not "
            "if it is not. Credential VALUES are never included; only the names of the "
            "environment variables each backend receives.",
        ),
        _tool(
            "backend_health",
            "Health of one backend or all of them: uptime, restart count, last error, and "
            "a live ping round-trip in milliseconds. A backend can be alive and wedged; "
            "only the round trip tells the two apart.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Backend name; omit for all"}
                },
                "required": [],
                "additionalProperties": False,
            },
        ),
        _tool(
            "restart_backend",
            "Stop and respawn one backend, re-reading its credentials from the currently "
            "loaded store. Use after rotating a token for a single service.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "reload_config",
            "Re-read servers.yaml and gateway.env and apply the difference. Backends whose "
            "resolved launch is unchanged are left running untouched. If either file fails "
            "to parse, nothing changes at all.",
            {
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "Report the plan without applying it",
                        "default": False,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        ),
    ]


def is_admin_tool(public_name: str) -> bool:
    try:
        server, _ = naming.split(public_name)
    except naming.NamingError:
        return False
    return server == ADMIN_PREFIX


def describe_backend(backend: Backend) -> dict[str, Any]:
    """One backend, as both surfaces report it.

    `env_keys` is the *names* of the variables from the server's `env` block. A
    `ServerSpec` holds templates rather than values, so there is no way for a value to
    reach this payload even by accident -- which is the point of that split.
    """
    spec = backend.spec
    return {
        "name": backend.name,
        "enabled": spec.enabled,
        "required": spec.required,
        "status": backend.status.value,
        "description": spec.description,
        "command": spec.command,
        "args": list(spec.args),
        "cwd": spec.cwd,
        "env_mode": spec.env_mode,
        "env_keys": spec.env_keys,
        "env_passthrough": list(spec.env_passthrough),
        "pid": backend.pid,
        "protocol_version": backend.protocol_version,
        "error": backend.error,
    }


async def describe_health(
    backend: Backend, *, ping: bool = True, counts: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = describe_backend(backend)
    payload.update(counts or {})
    payload.update(
        {
            "uptime_seconds": backend.uptime_seconds,
            "restart_count": backend.restart_count,
            "consecutive_failures": backend.consecutive_failures,
            # Down and *not being retried yet* is a different situation from simply down,
            # and an operator watching a restart button do nothing deserves to see which.
            # `0` whenever an attempt is allowed, which is the ordinary case.
            "retry_after_seconds": round(backend.retry_after_seconds, 1),
            "last_error": backend.error,
            "last_call_at": backend.last_call_at,
            "skipped_tools": list(backend.skipped_tools),
            # The live round trip is what makes this a health check rather than a status
            # dump. `None` means "did not answer", which is not the same as "not running".
            "ping_ms": (await backend.ping()) if ping and backend.running else None,
        }
    )
    return payload


def text_result(payload: Any) -> dict[str, Any]:
    """An MCP tool result carrying pretty JSON.

    Pretty rather than compact because the reader is a model or a person, and both do
    better with line breaks than with one long line.
    """
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


def error_result(message: str) -> dict[str, Any]:
    """A tool-level failure: a **successful** result carrying `isError`.

    MCP's own contract, and the reason it matters here is that a model can read this and
    adapt -- it can tell the human "GitHub is not configured" instead of the turn dying on
    a protocol error it has no way to act on.
    """
    return {"content": [{"type": "text", "text": message}], "isError": True}


class Admin:
    """Implements the meta-tools once, for both surfaces."""

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.started_at = time.time()

    async def call(self, tool: str, arguments: dict) -> dict[str, Any]:
        """Dispatch a `gateway__*` tool call."""
        _, name = naming.split(tool)
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise errors.InvalidParams(
                f"unknown gateway tool {tool!r}",
                {"knownTools": [t["name"] for t in tool_definitions()]},
            )
        return await handler(arguments)

    # --- the four tools -----------------------------------------------------------------

    async def _tool_list_backends(self, _arguments: dict) -> dict[str, Any]:
        return text_result([describe_backend(b) for b in self.gateway.backends()])

    async def _tool_backend_health(self, arguments: dict) -> dict[str, Any]:
        name = arguments.get("name")
        if name is None:
            return text_result([await self._health(b) for b in self.gateway.backends()])
        backend = self.gateway.backend(name)
        if backend is None:
            return error_result(
                f"no backend named {name!r}. Configured: "
                f"{', '.join(b.name for b in self.gateway.backends()) or '(none)'}"
            )
        return text_result(await self._health(backend))

    async def _health(self, backend: Backend) -> dict[str, Any]:
        return await describe_health(backend, counts=self.gateway.catalogue.counts(backend.name))

    async def _tool_restart_backend(self, arguments: dict) -> dict[str, Any]:
        name = arguments.get("name")
        backend = self.gateway.backend(name) if name else None
        if backend is None:
            return error_result(f"no backend named {name!r}")
        ok = await self.gateway.restart_backend(name)
        return text_result({"restarted": name, "running": ok, "error": backend.error})

    async def _tool_reload_config(self, arguments: dict) -> dict[str, Any]:
        return await self.gateway.reload(dry_run=bool(arguments.get("dry_run")))

    # --- the /admin method table --------------------------------------------------------

    async def status(self, _params: dict) -> dict[str, Any]:
        return {
            "uptime_seconds": time.time() - self.started_at,
            "version": self.gateway.server_info()["version"],
            "bind": self.gateway.bind_address(),
            "connections": self.gateway.describe_connections(),
            "config_path": str(self.gateway.config_path),
            "env_path": str(self.gateway.env_path),
        }

    async def backends(self, _params: dict) -> dict[str, Any]:
        return {"backends": [describe_backend(b) for b in self.gateway.backends()]}

    async def health(self, params: dict) -> dict[str, Any]:
        name = params.get("name")
        if name is None:
            return {"backends": [await self._health(b) for b in self.gateway.backends()]}
        backend = self.gateway.backend(name)
        if backend is None:
            raise errors.InvalidParams(f"no backend named {name!r}")
        return await self._health(backend)

    async def secrets_keys(self, _params: dict) -> dict[str, Any]:
        """Key **names** only. There is no method that returns a value, deliberately."""
        return {"keys": self.gateway.store.keys(), "source": str(self.gateway.env_path)}

    async def secrets_missing(self, _params: dict) -> dict[str, Any]:
        """Which `${VAR}` references each enabled server needs and the store does not have.

        Names, again -- and the reason this method exists at all. The UI has to be able to
        tell someone *why* a backend is down, and "GITHUB_TOKEN is not in gateway.env" is
        that answer. Being able to name the key is what makes not being able to write it
        an acceptable limitation rather than a dead end: the person adds the line by hand,
        in a file only they can read, and presses reload.
        """
        problems = missing_for(self.gateway.config, self.gateway.store)
        return {
            "source": str(self.gateway.env_path),
            "servers": {
                name: sorted({problem.key for problem in found})
                for name, found in problems.items()
            },
        }
