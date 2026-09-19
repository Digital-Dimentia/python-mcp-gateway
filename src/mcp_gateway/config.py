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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml import YAML, YAMLError

from mcp_gateway.branding import Branding, BrandingError
from mcp_gateway.branding import DEFAULT as DEFAULT_BRANDING
from mcp_gateway.branding import parse as parse_branding
from mcp_gateway.mcp_http import RESERVED_HEADERS
from mcp_gateway.naming import NamingError, validate_server_name
from mcp_gateway.secret_providers import ProviderSpec, SecretProviderError
from mcp_gateway.secret_providers import parse as parse_secret_providers

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

_TOP_KEYS = frozenset({"version", "defaults", "servers", "branding", "secrets"})
_DEFAULTS_KEYS = frozenset({"timeout", "startup_timeout", "env_mode", "cwd", "lazy", "idle_ttl"})
_ENTRY_KEYS = frozenset(
    {
        "command",
        "url",
        "headers",
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
        "lazy",
        "idle_ttl",
    }
)

#: The keys that describe a *process*, and so mean nothing on an entry that names a `url`.
#: Refused there rather than ignored: an `env` block on a URL backend is somebody expecting
#: a credential to reach a server that will never see it. See `config.md`.
_STDIO_ONLY_KEYS = frozenset({"command", "args", "env", "env_passthrough", "env_mode", "cwd"})
_HTTP_ONLY_KEYS = frozenset({"url", "headers"})

#: How a backend is reached. A `str` rather than an enum so it serialises as itself over
#: `/admin`, the same way `env_mode` does.
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"

#: RFC 9110's `token`: what a header field name may be made of.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


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
    #: Empty for a backend reached by `url`; exactly one of the two is set.
    command: str = ""
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
    #: Not spawned until something needs it. See `supervisor.md`.
    lazy: bool = False
    #: Seconds of inactivity after which the process is stopped, or `None` to keep it.
    #: The backend's *listings* survive the teardown, which is what makes it invisible.
    idle_ttl: float | None = None
    #: A Streamable HTTP endpoint, instead of a process to spawn. Never carries a
    #: credential -- `_parse_url` refuses one -- so it is as safe to show as `command`.
    url: str | None = None
    #: Request headers for a `url` backend, as templates: `Bearer ${TOKEN}`, never a value.
    #: The HTTP counterpart of `env`, resolved at start by `backend.resolve_headers`.
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def transport(self) -> str:
        return TRANSPORT_HTTP if self.url is not None else TRANSPORT_STDIO

    @property
    def header_keys(self) -> list[str]:
        """The header names this backend will be sent. Names only, like `env_keys`."""
        return sorted(self.headers)

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
    #: What this deployment calls itself. Display only -- see `branding.md`. Defaulted
    #: rather than optional so every reader can say `config.branding.title` without a
    #: branch, including the readers that run before any file has been loaded.
    branding: Branding = DEFAULT_BRANDING
    #: The `secrets:` block: where secret *values* come from, ahead of `gateway.env`.
    #: Validated here but not loaded here -- importing the operator's Python is
    #: `secret_providers.build_store`'s job, so parsing a catalogue never runs their code.
    #: Empty means the file alone, which is every deployment that has not asked otherwise.
    secret_providers: tuple[ProviderSpec, ...] = ()

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


def _as_idle_ttl(value: Any, *, where: str) -> float | None:
    """A positive number of seconds, or `None` for "never sleep".

    `None` and `0` are deliberately different: `null` means the backend is never torn down,
    and a `0` would mean "tear it down the instant it goes idle", which is a thrash rather
    than a policy. So zero is refused, and the way to say "do not do this" is to omit it.
    """
    if value is None:
        return None
    return _as_positive_float(value, where=where)


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

    if "url" in entry:
        return _parse_http_entry(name, entry, defaults, where=where)

    stray = sorted(_HTTP_ONLY_KEYS & set(entry))
    if stray:
        raise ConfigError(f"{where}: {stray} only apply to a backend with a 'url'")

    if "command" not in entry:
        raise ConfigError(f"{where}: 'command' or 'url' is required")

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
        **_common(entry, defaults, where=where),
    )


def _common(entry: dict[str, Any], defaults: dict[str, Any], *, where: str) -> dict[str, Any]:
    """The keys every backend has, however it is reached."""
    return {
        "timeout": _as_positive_float(
            entry.get("timeout", defaults.get("timeout", DEFAULT_TIMEOUT)), where=f"{where}.timeout"
        ),
        "startup_timeout": _as_positive_float(
            entry.get("startup_timeout", defaults.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT)),
            where=f"{where}.startup_timeout",
        ),
        "enabled": _as_bool(entry.get("enabled", True), where=f"{where}.enabled"),
        "required": _as_bool(entry.get("required", False), where=f"{where}.required"),
        "description": _as_str(
            entry.get("description", ""), where=f"{where}.description", allow_empty=True
        ),
        "lazy": _as_bool(entry.get("lazy", defaults.get("lazy", False)), where=f"{where}.lazy"),
        "idle_ttl": _as_idle_ttl(
            entry.get("idle_ttl", defaults.get("idle_ttl")), where=f"{where}.idle_ttl"
        ),
    }


def _parse_url(value: Any, *, where: str) -> str:
    """An `http` or `https` URL with a host, and nothing secret in it.

    `${VAR}` is refused here for a different reason than in `command`: a URL is shown --
    by `--list`, by `admin.status`, on the About screen -- and logged whenever the backend
    cannot be reached, so a token interpolated into it would be published by the very
    tools that exist to check the plan. `user:pass@` is refused for the same reason. A
    credential goes in `headers`, which only ever reports its names.
    """
    url = _as_str(value, where=where)
    if "${" in url:
        raise ConfigError(
            f"{where}: '${{...}}' is not interpolated in a url. The url is displayed and "
            f"logged; put the credential in 'headers' instead."
        )
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{where}: expected an http:// or https:// URL, got {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(
            f"{where}: a url must not carry user:password. It is displayed and logged; "
            f"send the credential in 'headers' instead."
        )
    if parts.fragment:
        raise ConfigError(f"{where}: a url with a #fragment names nothing a server can see")
    try:
        parts.port
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    return url


def _parse_headers(value: Any, *, where: str) -> dict[str, str]:
    headers = _as_str_map(value, where=where)
    seen: set[str] = set()
    for name, template in headers.items():
        if not _HEADER_NAME_RE.match(name):
            raise ConfigError(f"{where}: {name!r} is not a valid header name")
        if name.lower() in RESERVED_HEADERS:
            raise ConfigError(
                f"{where}.{name}: the gateway sets this header itself. "
                f"Reserved: {sorted(RESERVED_HEADERS)}."
            )
        if name.lower() in seen:
            raise ConfigError(f"{where}: {name!r} is named twice")
        seen.add(name.lower())
        if any(ch in template for ch in "\r\n\x00"):
            raise ConfigError(f"{where}.{name}: a header value cannot contain a line break")
    return headers


def _parse_http_entry(
    name: str, entry: dict[str, Any], defaults: dict[str, Any], *, where: str
) -> ServerSpec:
    stray = sorted(_STDIO_ONLY_KEYS & set(entry))
    if stray:
        raise ConfigError(
            f"{where}: {stray} describe a process, and this backend is a url. "
            f"A credential for it goes in 'headers'."
        )
    return ServerSpec(
        name=name,
        url=_parse_url(entry["url"], where=f"{where}.url"),
        headers=_parse_headers(entry.get("headers"), where=f"{where}.headers"),
        **_common(entry, defaults, where=where),
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
    if "idle_ttl" in defaults:
        _as_idle_ttl(defaults["idle_ttl"], where=f"{label}.defaults.idle_ttl")
    if "lazy" in defaults:
        _as_bool(defaults["lazy"], where=f"{label}.defaults.lazy")

    # Before the servers, so a broken logo is reported next to the other structural
    # refusals rather than after a hundred lines of catalogue have parsed cleanly.
    try:
        branding = parse_branding(
            document.get("branding"),
            base_dir=source.parent if source is not None else None,
            where=f"{label}.branding",
        )
    except BrandingError as exc:
        raise ConfigError(str(exc)) from exc

    # Beside branding, and for the same reason: a `secrets:` block that cannot be honoured
    # is a structural refusal, and it decides where every `${VAR}` below will resolve from.
    try:
        secret_providers = parse_secret_providers(
            document.get("secrets"), where=f"{label}.secrets"
        )
    except SecretProviderError as exc:
        raise ConfigError(str(exc)) from exc

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

    return GatewayConfig(
        source=source,
        servers=servers,
        defaults=dict(defaults),
        branding=branding,
        secret_providers=secret_providers,
    )


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
