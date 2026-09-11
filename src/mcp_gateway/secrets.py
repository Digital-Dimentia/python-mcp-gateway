"""Parse `gateway.env` and resolve `${VAR}` references against it.

The only module that ever holds a secret *value*. Everything else moves key names around.

## Why the parser is hand-rolled rather than `python-dotenv`

Not to save a dependency -- to own the grammar. dotenv implementations disagree with each
other and with their own past versions about `export`, about interpolation inside the
file, about multiline values, and about whether `#` in an unquoted value starts a comment.
A credential file whose *meaning* depends on a third party's changing heuristics is
precisely what a credential consolidator must not have: the failure mode is a token that
silently parses as a prefix of itself.

The grammar below is deliberately tiny, fully specified, and will not move.

## The three rules where implementations disagree, and what this one does

- **No inline comment stripping in unquoted values.** `#` is a legal character in a token,
  and `KEY=abc#def` is the four-plus-four-character value `abc#def`. Quote the value if a
  trailing comment is wanted.
- **No interpolation inside this file, and no multiline values.** Interpolation happens in
  exactly one direction and one place: `servers.yaml` -> this store, via `interpolate`.
- **A duplicate key is an error**, naming both line numbers. Silently taking the last is
  how a freshly rotated credential gets shadowed by a stale one further up the file, which
  is a bug that presents as "the rotation did not work" hours later.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - `config` does not import this module, so this is
    # a one-way type-only edge rather than a cycle.
    from mcp_gateway.config import GatewayConfig

logger = logging.getLogger(__name__)

#: POSIX-shell identifier rules. A key that is not one of these could not be exported to a
#: subprocess anyway, so accepting it would only defer the failure.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: `${NAME}` and nothing else. **Bare `$NAME` is deliberately not interpolated**: `$` is
#: common inside real passwords and generated tokens, and a bare-name rule would silently
#: eat part of one. `$${NAME}` escapes to a literal `${NAME}`.
_REFERENCE_RE = re.compile(r"\$(\$)?\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Only the two escapes a person actually writes, plus the two that make them writable.
_DOUBLE_QUOTE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}

#: Below this length, redacting a value would mangle unrelated log lines -- a two-character
#: secret occurs inside ordinary words. Such a value is not a secret worth having anyway.
MIN_REDACTABLE_LENGTH = 4


class SecretError(ValueError):
    """A malformed credential file, or a reference to a key it does not hold."""


@dataclass(frozen=True)
class MissingSecret:
    """One unresolvable `${VAR}`, described well enough to fix without opening the file."""

    key: str
    where: str

    def __str__(self) -> str:
        return f"missing secret {self.key} (referenced by {self.where})"


@dataclass(frozen=True)
class SecretStore:
    """The parsed contents of `gateway.env`.

    Frozen, and `_values` is private, because the store is handed to code that has no
    business enumerating secrets: a caller asks for a key it names, or asks which keys
    exist. `redactable_values()` is the single exception, and it exists so the logging
    filter can do its job.
    """

    source: Path | None = None
    _values: dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        """Key names only.

        The first of the three redaction layers. A store that renders its values would put
        every credential into any log line, traceback, or `pytest` assertion diff that
        happened to include it -- and a traceback is the one place nobody thinks to check.
        """
        return f"SecretStore(source={self.source!s}, keys={sorted(self._values)})"

    __str__ = __repr__

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def keys(self) -> list[str]:
        """The key names, sorted. Safe to log, safe to return over `/admin`."""
        return sorted(self._values)

    def get(self, key: str) -> str | None:
        """The value, or `None` when the key is absent.

        An **empty value is present, not missing**: `TOKEN=` in the file is how someone
        spells "deliberately blank", and treating it as absent would be a second rule
        nobody asked for -- one that turns a blanked credential into a "missing secret"
        error naming a key that is plainly there.
        """
        return self._values.get(key)

    def redactable_values(self) -> list[str]:
        """Every value long enough to be worth scrubbing from a log line.

        Sorted longest-first so that a value which contains another -- `Bearer <token>`
        alongside `<token>` -- is replaced before its substring, leaving `***` rather than
        `Bearer ***` and a fragment.
        """
        return sorted(
            (v for v in self._values.values() if len(v) >= MIN_REDACTABLE_LENGTH),
            key=len,
            reverse=True,
        )


def _unquote(raw: str, *, path: Path | None, lineno: int) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        # Single quotes are literal, shell-style. No escapes at all, which is what makes
        # them the safe choice for a value containing backslashes.
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        body = raw[1:-1]
        out: list[str] = []
        index = 0
        while index < len(body):
            char = body[index]
            if char == "\\" and index + 1 < len(body):
                nxt = body[index + 1]
                if nxt in _DOUBLE_QUOTE_ESCAPES:
                    out.append(_DOUBLE_QUOTE_ESCAPES[nxt])
                    index += 2
                    continue
                raise SecretError(
                    f"{_where(path, lineno)}: unknown escape '\\{nxt}' in a double-quoted "
                    f"value. Supported: \\n \\t \\r \\\\ \\\". Use single quotes for a "
                    f"value that contains backslashes literally."
                )
            out.append(char)
            index += 1
        return "".join(out)
    return raw.strip()


def _where(path: Path | None, lineno: int) -> str:
    return f"{path if path is not None else '<string>'}:{lineno}"


def parse(text: str, *, path: Path | None = None) -> dict[str, str]:
    """Parse the dotenv-ish grammar in the module docstring. Raises `SecretError`."""
    values: dict[str, str] = {}
    seen_at: dict[str, int] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            # People paste from a shell profile. Accepting the word costs nothing and
            # removes a confusing "invalid key 'export TOKEN'" from the first-run path.
            line = line[len("export ") :].lstrip()
        key, sep, raw_value = line.partition("=")
        if not sep:
            raise SecretError(f"{_where(path, lineno)}: expected KEY=VALUE, got {raw_line!r}")
        key = key.strip()
        if not _KEY_RE.match(key):
            raise SecretError(
                f"{_where(path, lineno)}: {key!r} is not a valid variable name; it must "
                f"start with a letter or '_' and contain only letters, digits and '_'."
            )
        if key in seen_at:
            raise SecretError(
                f"{_where(path, lineno)}: duplicate key {key!r}, first set on line "
                f"{seen_at[key]}. Silently keeping one of the two is how a rotated "
                f"credential gets shadowed by a stale one; remove the line you do not want."
            )
        seen_at[key] = lineno
        values[key] = _unquote(raw_value.strip(), path=path, lineno=lineno)
    return values


def load(path: Path, *, required: bool = False) -> SecretStore:
    """Read and parse `path`.

    A missing file is an **empty store**, not an error, unless `required`. The daemon must
    start on a machine that has not been configured yet -- it still serves its admin tools,
    which are how an operator finds out what is missing.

    A world- or group-readable file gets a WARNING naming it. Deliberately not fatal:
    refusing to start over a permission bit is hostile on Windows, on WSL's DrvFs where
    modes are synthetic, and in CI where the file was just written by a runner.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise SecretError(f"{path}: credential store not found") from None
        logger.info("no credential store at %s; no secrets available", path)
        return SecretStore(source=path)
    except OSError as exc:
        raise SecretError(f"{path}: cannot read credential store: {exc}") from exc

    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover - the read above just succeeded
        mode = 0
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "%s is readable by other accounts on this machine (mode %o); run `chmod 600 %s`",
            path,
            stat.S_IMODE(mode),
            path,
        )

    return SecretStore(source=path, _values=parse(text, path=path))


def interpolate(
    template: str, store: SecretStore, *, where: str, missing: list[MissingSecret] | None = None
) -> str:
    """Substitute `${NAME}` references in `template` from `store`.

    **Resolves against the store only, never `os.environ`.** Falling back to the ambient
    environment is exactly the leak this project exists to stop: it would make a backend's
    credential depend on whichever shell happened to launch the daemon, produce the classic
    "works on my machine", and quietly defeat curated env mode by the back door. The one
    deliberate door to a parent variable is a server's `env_passthrough` list.

    `where` names the config site for the error message -- `servers.github.env.GITHUB_TOKEN`
    -- so a missing key is fixable without hunting.

    When `missing` is given, unresolvable references are appended to it and substituted as
    the empty string, letting a caller collect *every* problem in one pass rather than
    reporting the first. That is what `--check` wants. Without it, the first miss raises.
    """
    out: list[str] = []
    index = 0
    for match in _REFERENCE_RE.finditer(template):
        out.append(template[index : match.start()])
        index = match.end()
        escaped, key = match.group(1), match.group(2)
        if escaped:
            out.append("${" + key + "}")
            continue
        value = store.get(key)
        if value is None:
            problem = MissingSecret(key=key, where=where)
            if missing is None:
                raise SecretError(str(problem))
            missing.append(problem)
            continue
        out.append(value)
    out.append(template[index:])
    return "".join(out)


def missing_for(config: "GatewayConfig", store: SecretStore) -> dict[str, list[MissingSecret]]:
    """Every unresolvable `${VAR}`, per enabled server.

    Collects rather than raising, so one pass reports every problem instead of the first.
    Disabled servers are skipped entirely: parking a backend is how an operator defers
    dealing with its credential, and reporting it anyway would defeat that.

    Lives here rather than in `cli.py` because two callers need it -- `--check` before the
    daemon starts, and `admin.secrets.missing` while it is running, so the UI can name the
    key to add to `gateway.env` without ever asking for its value. Two copies of this loop
    is exactly how one of them ends up forgetting `cwd`.
    """
    problems: dict[str, list[MissingSecret]] = {}
    for name, spec in config.enabled.items():
        missing: list[MissingSecret] = []
        for key, template in spec.env.items():
            interpolate(template, store, where=f"servers.{name}.env.{key}", missing=missing)
        if spec.cwd is not None:
            interpolate(spec.cwd, store, where=f"servers.{name}.cwd", missing=missing)
        if missing:
            problems[name] = missing
    return problems


def expand_path(value: str, store: SecretStore, *, where: str) -> str:
    """`interpolate`, then `~` expansion. Used for `cwd` and nothing else.

    `os.path.expanduser` and not `expandvars`: the latter would resolve `$HOME` from the
    ambient environment, reintroducing the fallback the module refuses everywhere else.
    """
    return os.path.expanduser(interpolate(value, store, where=where))
