"""Parse and validate `servers.yaml` into `GatewayConfig`.

No secrets and no spawning: this module decides what a launch *would* look like, and
`backend.py` resolves credentials into it at spawn time. That split is why `--check` can
report a structural problem and a missing credential in the same pass without starting
anything, and why a parse failure during reload can leave the running set untouched.

## Validation is loud

A catalogue that half-parses is worse than one that refuses. An unknown key at any level
is an error naming the file, the entry, and the keys that *are* allowed -- because the
overwhelmingly common cause is a typo or a key borrowed from a different tool's schema,
and silently ignoring it produces a server that runs with settings the operator believes
they applied.

## JSON works for free

The loader is YAML 1.2 in safe, pure-Python mode, and YAML 1.2 is a superset of JSON. So
`--config servers.json` -- including a file pasted straight out of a Claude Desktop
config -- goes through the identical code path with no branch and no second parser.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from mcp_gateway.naming import NamingError, validate_server_name

logger = logging.getLogger(__name__)

#: Bumped only for a change that would make an older gateway misread a newer file.
SUPPORTED_VERSIONS = frozenset({1})

DEFAULT_TIMEOUT = 30.0
DEFAULT_STARTUP_TIMEOUT = 20.0

#: How a backend's environment is built. See `backend.py` for the layers, and `config.md`
#: for why `curated` is the default.
ENV_MODE_CURATED = "curated"
ENV_MODE_INHERIT = "inherit"
ENV_MODES = frozenset({ENV_MODE_CURATED, ENV_MODE_INHERIT})

_TOP_KEYS = frozenset({"version", "defaults", "servers"})
_DEFAULTS_KEYS = frozenset({"timeout", "startup_timeout", "env_mode", "cwd"})
_ENTRY_KEYS = frozenset(
    {
        "command",
        "args",
        "env",
        "env_passthrough",
        "env_mode",
        "cwd",
        "timeout",
        "startup_timeout",
        "enabled",
        "required",
        "description",
    }
)


class ConfigError(ValueError):
    """A catalogue that cannot be trusted. A `ValueError`, so it maps to `-32602`."""


@dataclass(frozen=True)
class ServerSpec:
    """One backend's launch specification, with `${VAR}` references still unresolved.

    Frozen and secret-free by construction: `env` holds the *template* strings from the
    file, so a `ServerSpec` is safe to log, to return over `/admin`, and to hold in a diff
    against the previous config.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: tuple[str, ...] = ()
    env_mode: str = ENV_MODE_CURATED
    cwd: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    enabled: bool = True
    required: bool = False
    description: str = ""

    @property
    def env_keys(self) -> list[str]:
        """The variable names this backend will receive from its own `env` block.

        Names only. This is what `gateway__list_backends` reports, and the reason it can
        be reported at all is that a `ServerSpec` never holds a value.
        """
        return sorted(self.env)


@dataclass(frozen=True)
class GatewayConfig:
    """A whole catalogue. `servers` preserves the file's order, which `--list` prints."""

    source: Path | None
    servers: dict[str, ServerSpec] = field(default_factory=dict)
    #: The file's `defaults:` block, exactly as written and already validated.
    #:
    #: Redundant to the running gateway -- every value here is folded into the specs above
    #: by `_parse_entry`, and nothing at runtime consults it. It is kept for the one reader
    #: that needs the block itself rather than its effect: an editor offering to add a
    #: server has to say what omitting a key will mean, and the folded specs cannot answer
    #: that. `admin.config.get` reports it; see python-mcp-gateway-cw4.
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> dict[str, ServerSpec]:
        return {name: spec for name, spec in self.servers.items() if spec.enabled}


def _require(mapping: Any, *, what: str, where: str) -> dict[str, Any]:
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise ConfigError(f"{where}: {what} must be a mapping, got {type(mapping).__name__}")
    return mapping


def _reject_unknown(mapping: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {unknown}. Allowed here: {sorted(allowed)}."
        )


def _as_str(value: Any, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {type(value).__name__}")
    if not value and not allow_empty:
        raise ConfigError(f"{where}: must not be empty")
    return value


def _as_str_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list of strings, got {type(value).__name__}")
    return tuple(_as_str(item, where=f"{where}[{i}]", allow_empty=True) for i, item in enumerate(value))


def _as_str_map(value: Any, *, where: str) -> dict[str, str]:
    mapping = _require(value, what="this", where=where)
    out: dict[str, str] = {}
    for key, item in mapping.items():
        key_str = _as_str(key, where=f"{where}: key")
        out[key_str] = _as_str(item, where=f"{where}.{key_str}", allow_empty=True)
    return out


def _as_positive_float(value: Any, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {type(value).__name__}")
    if value <= 0:
        raise ConfigError(f"{where}: must be greater than zero, got {value}")
    return float(value)


def _as_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        # Deliberately strict. YAML 1.2 already reads `yes`/`on` as the *strings* they are,
        # so a file written against YAML 1.1 habits would otherwise set `enabled` to a
        # truthy string and disable nothing.
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _as_env_mode(value: Any, *, where: str) -> str:
    mode = _as_str(value, where=where)
    if mode not in ENV_MODES:
        raise ConfigError(f"{where}: expected one of {sorted(ENV_MODES)}, got {mode!r}")
    return mode


def _parse_entry(name: str, raw: Any, defaults: dict[str, Any], *, where: str) -> ServerSpec:
    entry = _require(raw, what=f"server {name!r}", where=where)
    _reject_unknown(entry, _ENTRY_KEYS, where=where)

    if "command" not in entry:
        raise ConfigError(f"{where}: 'command' is required")

    command = _as_str(entry["command"], where=f"{where}.command")
    args = _as_str_tuple(entry.get("args"), where=f"{where}.args")

    # `${VAR}` is refused in command and args, and the refusal is a feature worth
    # explaining: argv is world-readable through `ps` on every platform this runs on, so a
    # secret on a command line publishes itself to every other account on the machine at
    # the moment it is used to protect something.
    for label, value in [("command", command), *((f"args[{i}]", a) for i, a in enumerate(args))]:
        if "${" in value:
            raise ConfigError(
                f"{where}.{label}: '${{...}}' is not interpolated in command or args. "
                f"argv is world-readable through `ps`, so a secret there is published to "
                f"every other account on this machine. Put it in the 'env' block instead."
            )

    cwd = entry.get("cwd", defaults.get("cwd"))
    if cwd is not None:
        cwd = _as_str(cwd, where=f"{where}.cwd")

    return ServerSpec(
        name=name,
        command=command,
        args=args,
        env=_as_str_map(entry.get("env"), where=f"{where}.env"),
        env_passthrough=_as_str_tuple(entry.get("env_passthrough"), where=f"{where}.env_passthrough"),
        env_mode=_as_env_mode(
            entry.get("env_mode", defaults.get("env_mode", ENV_MODE_CURATED)),
            where=f"{where}.env_mode",
        ),
        cwd=cwd,
        timeout=_as_positive_float(
            entry.get("timeout", defaults.get("timeout", DEFAULT_TIMEOUT)), where=f"{where}.timeout"
        ),
        startup_timeout=_as_positive_float(
            entry.get("startup_timeout", defaults.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT)),
            where=f"{where}.startup_timeout",
        ),
        enabled=_as_bool(entry.get("enabled", True), where=f"{where}.enabled"),
        required=_as_bool(entry.get("required", False), where=f"{where}.required"),
        description=_as_str(entry.get("description", ""), where=f"{where}.description", allow_empty=True),
    )


def parse(text: str, *, source: Path | None = None) -> GatewayConfig:
    """Parse a catalogue from YAML or JSON text. Raises `ConfigError`."""
    label = str(source) if source is not None else "<string>"
    # pure=True keeps this off `ruamel.yaml.clib`, a C extension that would put a
    # per-architecture wheel into a release that builds for both amd64 and arm64.
    yaml = YAML(typ="safe", pure=True)
    try:
        raw = yaml.load(text)
    except YAMLError as exc:
        raise ConfigError(f"{label}: not valid YAML or JSON: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{label}: empty catalogue; expected a 'servers' mapping")
    document = _require(raw, what="the catalogue", where=label)
    _reject_unknown(document, _TOP_KEYS, where=label)

    version = document.get("version", 1)
    if version not in SUPPORTED_VERSIONS:
        raise ConfigError(
            f"{label}: version {version!r} is not supported by this gateway "
            f"(supported: {sorted(SUPPORTED_VERSIONS)})"
        )

    defaults = _require(document.get("defaults"), what="'defaults'", where=f"{label}.defaults")
    _reject_unknown(defaults, _DEFAULTS_KEYS, where=f"{label}.defaults")
    if "env_mode" in defaults:
        _as_env_mode(defaults["env_mode"], where=f"{label}.defaults.env_mode")
    if "timeout" in defaults:
        _as_positive_float(defaults["timeout"], where=f"{label}.defaults.timeout")
    if "startup_timeout" in defaults:
        _as_positive_float(defaults["startup_timeout"], where=f"{label}.defaults.startup_timeout")

    servers_raw = document.get("servers")
    if servers_raw is None:
        raise ConfigError(f"{label}: 'servers' is required")
    servers_map = _require(servers_raw, what="'servers'", where=f"{label}.servers")

    servers: dict[str, ServerSpec] = {}
    for name, entry in servers_map.items():
        if not isinstance(name, str):
            raise ConfigError(f"{label}.servers: server names must be strings, got {name!r}")
        try:
            validate_server_name(name)
        except NamingError as exc:
            raise ConfigError(f"{label}.servers.{name}: {exc}") from exc
        servers[name] = _parse_entry(name, entry, defaults, where=f"{label}.servers.{name}")

    return GatewayConfig(source=source, servers=servers, defaults=dict(defaults))


def load(path: Path) -> GatewayConfig:
    """Read and parse `path`.

    Returns a new `GatewayConfig` or raises; it never mutates anything. That ordering is
    the whole guarantee behind reload: a catalogue that fails to parse leaves the running
    one untouched because there was never a moment when half of it had been applied.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path}: no such catalogue") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read catalogue: {exc}") from exc
    return parse(text, source=path)
