"""Write `servers.yaml`. The only module in this package that does.

`config.py` reads a catalogue and never mutates anything, which is the guarantee reload
rests on. This is the other half, added for the admin UI, and it is deliberately a separate
module: a reader that cannot write is a property worth being able to state about a file, and
stating it means keeping the writer somewhere else.

## Four rules, in this order

**1. Round-trip, so the comments survive.** `servers.yaml` is committed, and most of it is
prose explaining why each knob is set the way it is. An edit that dropped that would make
the UI a tool you can use once. `ruamel.yaml` in `typ="rt"` mode preserves comments, key
order and quoting style; `pure=True` keeps it off `ruamel.yaml.clib`, for the reason
`pyproject.toml` spells out.

**2. Validate before writing.** The candidate is serialised, handed to `config.parse`, and
only written if that succeeds. `parse` is the same function the daemon loads with, so there
is no second opinion about what is valid -- and a rejected edit leaves the file byte for
byte as it was. This matters more than it looks: the UI's user is editing the file the
daemon is about to reload, and a half-valid write is a daemon that will not come back.

**3. A literal `env` value is a warning, not a refusal.** The file is committed and must
never contain a secret *value* -- that is the whole argument for `gateway.env` -- so a value
that is not a `${VAR}` reference is reported. It cannot be refused: `MOCK_MCP_SCHEMA_ZOO: "1"`
is a legitimate literal, and a writer that refused it would be one people work around by
editing the file by hand, which is worse than a warning they read.

**4. Atomically, with a backup.** Write a temp file in the same directory, copy the original
mode onto it, `os.replace`. A crash mid-write leaves the old file intact rather than a
truncated one, and `.bak` is there for the edit that was valid and still wrong.

## What is not here

Nothing writes `gateway.env`. `admin.py` explains why at length: a process that can be
talked into writing a credential is one whose blast radius is every credential on the
machine. Config writing moving onto `/admin` does not move that line; it is why config
writing is on `/admin` and not on `/mcp`.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from mcp_gateway import config as config_module
from mcp_gateway.config import ConfigError, GatewayConfig
from mcp_gateway.naming import NamingError, validate_server_name

logger = logging.getLogger(__name__)

#: The keys an entry may carry, straight from the reader, so the two cannot drift.
ENTRY_KEYS = config_module._ENTRY_KEYS  # noqa: SLF001 - one definition, deliberately shared

#: A value made entirely of `${VAR}` references and surrounding text is fine; one with no
#: reference at all is the thing worth warning about.
_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


class ConfigWriteError(ConfigError):
    """A write that was refused. A `ConfigError`, so it maps to `-32602` like a bad read."""


def _yaml() -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    # Deep enough for `servers: -> name: -> env: -> KEY:` without reflowing what is there.
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 100
    return yaml


def load_document(path: Path) -> Any:
    """The file as a round-trip tree, comments and all.

    A missing file is not an error: adding the first server to a checkout that has no
    catalogue yet should work, and the alternative is telling someone to create an empty
    file by hand before the UI will talk to them.
    """
    yaml = _yaml()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "servers": {}}
    except OSError as exc:
        raise ConfigWriteError(f"{path}: cannot read catalogue: {exc}") from exc
    try:
        document = yaml.load(text)
    except YAMLError as exc:
        raise ConfigWriteError(f"{path}: not valid YAML or JSON: {exc}") from exc
    if document is None:
        return {"version": 1, "servers": {}}
    if not isinstance(document, dict):
        raise ConfigWriteError(f"{path}: the catalogue is not a mapping")
    if document.get("servers") is None:
        document["servers"] = {}
    return document


def dump(document: Any) -> str:
    buffer = io.StringIO()
    _yaml().dump(document, buffer)
    return buffer.getvalue()


def warnings_for(document: Any) -> list[str]:
    """Every `env` value that is a literal rather than a `${VAR}` reference.

    Advice, never a refusal -- see rule 3 in the module docstring.
    """
    found: list[str] = []
    servers = document.get("servers") or {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        for key, value in (entry.get("env") or {}).items():
            if isinstance(value, str) and not _REFERENCE.search(value):
                found.append(
                    f"servers.{name}.env.{key} is a literal value. servers.yaml is "
                    f"committed and must hold no credential: put the value in gateway.env "
                    f"and reference it here as ${{{key}}}."
                )
    return found


def validate(document: Any, *, source: Path | None) -> GatewayConfig:
    """Parse the candidate with the reader the daemon itself uses. Raises `ConfigWriteError`."""
    try:
        return config_module.parse(dump(document), source=source)
    except ConfigError as exc:
        raise ConfigWriteError(str(exc)) from exc


def write(path: Path, document: Any) -> list[str]:
    """Validate, back up, and replace `path` atomically. Returns the warnings.

    Validation happens before anything touches the filesystem, so a refused write leaves
    the file exactly as it was -- there is no moment at which half of it has been applied.
    """
    validate(document, source=path)
    warnings = warnings_for(document)
    text = dump(document)

    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # A copy rather than a rename: the original keeps its inode, so a daemon
            # holding the path -- or an editor with it open -- is not left pointing at
            # `.bak`.
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=f".{path.name}.", suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, mode)
            os.replace(handle.name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise
    except OSError as exc:
        raise ConfigWriteError(f"{path}: cannot write catalogue: {exc}") from exc

    logger.info("wrote %s (%d server(s))", path, len(document.get("servers") or {}))
    for warning in warnings:
        logger.warning("%s", warning)
    return warnings


# --- the operations `/admin` exposes ----------------------------------------------------


def _entry_for(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """One server entry, with only the keys the reader accepts and nothing invented.

    Unknown keys are refused here rather than at parse time so the message names the UI's
    own field. `config.parse` would catch them anyway; this just says so earlier and better.
    """
    unknown = sorted(set(spec) - ENTRY_KEYS)
    if unknown:
        raise ConfigWriteError(
            f"servers.{name}: unknown key(s) {unknown}. Allowed: {sorted(ENTRY_KEYS)}."
        )
    # Dropped rather than written: an empty `args: []` or `env: {}` is noise in a file
    # people read, and means exactly what its absence means.
    return {key: value for key, value in spec.items() if value not in (None, "", [], {})}


def add_server(path: Path, name: str, spec: dict[str, Any]) -> tuple[list[str], str]:
    """Add one server. Refuses a name that already exists."""
    try:
        validate_server_name(name)
    except NamingError as exc:
        raise ConfigWriteError(str(exc)) from exc
    document = load_document(path)
    if name in (document.get("servers") or {}):
        raise ConfigWriteError(f"a server named {name!r} is already configured")
    document["servers"][name] = _entry_for(name, spec)
    return write(path, document), dump(document)


def update_server(path: Path, name: str, changes: dict[str, Any]) -> tuple[list[str], str]:
    """Merge `changes` into one server. A key set to `None` is removed."""
    document = load_document(path)
    servers = document.get("servers") or {}
    if name not in servers:
        raise ConfigWriteError(f"no server named {name!r} in {path}")
    unknown = sorted(set(changes) - ENTRY_KEYS)
    if unknown:
        raise ConfigWriteError(
            f"servers.{name}: unknown key(s) {unknown}. Allowed: {sorted(ENTRY_KEYS)}."
        )
    entry = servers[name]
    if not isinstance(entry, dict):
        raise ConfigWriteError(f"servers.{name} is not a mapping")
    for key, value in changes.items():
        # A merge rather than a replace, so an edit that touches `enabled` does not silently
        # drop a `cwd` the UI did not happen to send.
        if value is None or value == "" or value == [] or value == {}:
            entry.pop(key, None)
        else:
            entry[key] = value
    return write(path, document), dump(document)


def remove_server(path: Path, name: str) -> tuple[list[str], str]:
    """Remove one server."""
    document = load_document(path)
    servers = document.get("servers") or {}
    if name not in servers:
        raise ConfigWriteError(f"no server named {name!r} in {path}")
    del servers[name]
    return write(path, document), dump(document)


def set_servers(path: Path, servers: dict[str, Any]) -> tuple[list[str], str]:
    """Replace the whole `servers` mapping, keeping `version`, `defaults` and their comments.

    The blunt instrument, for a UI editing the catalogue as a whole. Entries that survive
    keep their own comments because the round-trip tree keeps them attached to the node --
    replacing an entry loses that entry's comments and nothing else's.
    """
    if not isinstance(servers, dict):
        raise ConfigWriteError("'servers' must be a mapping of name to entry")
    document = load_document(path)
    rebuilt: dict[str, Any] = {}
    for name, spec in servers.items():
        try:
            validate_server_name(name)
        except NamingError as exc:
            raise ConfigWriteError(str(exc)) from exc
        if not isinstance(spec, dict):
            raise ConfigWriteError(f"servers.{name}: entry must be a mapping")
        rebuilt[name] = _entry_for(name, spec)
    existing = document.get("servers") or {}
    for name in list(existing):
        if name not in rebuilt:
            del existing[name]
    for name, entry in rebuilt.items():
        if name in existing and isinstance(existing[name], dict):
            current = existing[name]
            for key in list(current):
                if key not in entry:
                    del current[key]
            current.update(entry)
        else:
            existing[name] = entry
    document["servers"] = existing
    return write(path, document), dump(document)
