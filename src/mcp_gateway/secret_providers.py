"""Where secret *values* come from, when they do not come from `gateway.env`.

[`secrets.py`](secrets.md) owns one store and one grammar: a file this project defines.
This module owns the seam in front of it, so a deployment that keeps its credentials in
Vault, in AWS Secrets Manager, or behind an internal service can hand the gateway a Python
file of its own without forking anything.

A `secrets:` block in `servers.yaml` names the providers. They are asked in order, the
first answer for a key wins, and **`gateway.env` is always the last link** -- so the
deployment that moves its backend tokens to Vault does not also have to move the daemon's
own access key out of the file it already sits in.

## The interface is a snapshot, not a lazy lookup

`load(request) -> Mapping[str, str]`. One call, every key, up front. The obvious
alternative -- `get(key)`, resolved on demand -- was rejected, and for a reason that is
worth stating in full because it is not a matter of taste.

Three separate things in this project depend on the set of secret values being *known at a
single moment*:

- [`logging_redaction.py`](logging_redaction.md) installs a filter holding every value, on
  the root logger. A value fetched after that filter was installed is a value the filter
  has never heard of, and it is unredacted in every log line, every traceback, and
  `admin.logs.tail`, for the rest of the process's life. A lazy provider makes that the
  *normal* case rather than a bug.
- Reload is load-then-replace and therefore atomic: a store that fails to build leaves the
  running one untouched. A lazy store cannot fail at reload time, because it has not
  fetched anything yet -- it fails later, one key at a time, half-applied.
- `--check` and `admin.secrets.missing` answer "what is this deployment missing" without
  starting anything. They can only do that against a store that has already resolved.

So the cost of the snapshot is paid where it belongs: a provider that cannot reach its
backing store fails at startup or at reload, loudly, naming itself.

## The request says which keys, so nothing has to enumerate

`SecretRequest.keys` is exactly the set of `${NAME}` references in `servers.yaml`, plus the
daemon's own `WS_ACCESS_KEY`. A remote store is then asked for named keys and does not need
an enumeration API at all -- which matters, because listing a Vault mount and reading a
Vault path are different permissions, and the smaller one is the one a gateway should need.

A provider may return keys that were not asked for; they are kept. It may also return
fewer, and a key nobody answers is reported by the existing "missing secret" path, which
already names the config site that referenced it.

## Sync, and called off the event loop

`load` is a plain `def`. Provider files in the wild will reach for `boto3`, `hvac` or
`requests`, all of which are synchronous, and making the interface `async` would tax every
third-party file to spare this one module an `asyncio.to_thread`. `--check` is a sync path
too, and an async interface would have it spinning up an event loop to validate a file.

The daemon calls providers through `to_thread` during reload so a slow network fetch does
not stall the sessions already attached.

## Loading a provider is running the operator's code

`provider: "./providers/vault.py:VaultProvider"` imports a file and calls into it. That is
arbitrary code execution, and it is not a new trust boundary: `servers.yaml` already names
`command:` lines this daemon spawns as itself. Anyone who can edit the catalogue can
already run code. What this module does add is a refusal to take the *path* from anywhere
but the catalogue, and no `${VAR}` interpolation inside the block -- see below.

## No `${VAR}` inside the `secrets:` block

There is nothing to interpolate against. The block is what decides how the store gets
built, so it necessarily parses before any store exists, and a `${VAULT_TOKEN}` here could
only ever resolve from `os.environ` -- the exact fallback
[`secrets.md`](secrets.md) exists to refuse. A provider that needs its own credential reads
it itself, from its own environment or its own file, in code the deployment owns.

## Options are handed over unvalidated

`options:` is whatever mapping the provider wants; this module checks that it is a mapping
and stops there. Validating it would mean this project knowing what a Vault mount is, and
the entire point of the seam is that it does not.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from mcp_gateway.secrets import SecretError, SecretStore
from mcp_gateway.secrets import load as load_env_file

if TYPE_CHECKING:  # pragma: no cover - type-only, as in `secrets.py`
    from collections.abc import Mapping

    from mcp_gateway.config import GatewayConfig

logger = logging.getLogger(__name__)

#: The origin label for a value that came from `gateway.env`. Reported per key by
#: `admin.secrets.list`, which used to be able to say "the source is that file" once for
#: the whole store and now cannot.
FILE_ORIGIN = "gateway.env"

_BLOCK_KEYS = frozenset({"providers"})
_PROVIDER_KEYS = frozenset({"provider", "options"})

#: A provider reference is `<target>:<attribute>`. The attribute half is required rather
#: than defaulting to something like `Provider`: a file may sensibly hold two, and an
#: implicit name is a convention to remember rather than a line to read.
_REF_SEPARATOR = ":"


class SecretProviderError(SecretError):
    """A provider that cannot be loaded, or one that failed while loading secrets.

    A subclass of `SecretError` so every existing caller -- `--check`, the reload path's
    "refused, nothing changed" branch -- already handles it without a second except clause.
    """


@dataclass(frozen=True)
class SecretRequest:
    """What a provider is told when it is asked for secrets.

    Frozen and small on purpose: it is the one object third-party code sees, so every field
    here is a field this project has to keep working.
    """

    #: Every `${NAME}` referenced by `servers.yaml`, plus the daemon's own access key. A
    #: provider is free to ignore it and return everything it has.
    keys: frozenset[str] = frozenset()
    #: The `options:` mapping from this provider's entry, verbatim and unvalidated.
    options: "Mapping[str, Any]" = field(default_factory=dict)
    #: The catalogue that named the provider, so a provider resolving a relative path of
    #: its own resolves it against the file rather than the daemon's working directory --
    #: which is launchd's, or a container entrypoint's, or a terminal's.
    config_path: Path | None = None

    @property
    def base_dir(self) -> Path | None:
        """The directory holding the catalogue. `None` when the config came from a string."""
        return self.config_path.parent if self.config_path is not None else None


@runtime_checkable
class SecretProvider(Protocol):
    """One source of secret values.

    Implement this in a file of your own and point `servers.yaml` at it::

        # /etc/mcp-gateway/providers/vault.py
        class VaultProvider:
            def __init__(self, **options):
                self._client = hvac.Client(url=options["addr"])
                self._mount = options["mount"]

            def load(self, request):
                return {
                    key: self._client.secrets.kv.read_secret_version(
                        path=f"{self._mount}/{key}"
                    )["data"]["data"]["value"]
                    for key in request.keys
                }

    ::

        secrets:
          providers:
            - provider: "./providers/vault.py:VaultProvider"
              options: {addr: "https://vault.internal:8200", mount: "kv/mcp-gateway"}

    **Raise on failure; do not return an empty mapping.** An empty return is indistinguishable
    from "this store legitimately holds none of these keys", which is a real and useful
    answer in a chain. An exception is how a provider says "I could not reach my backing
    store", and it is what stops a half-resolved catalogue from starting.
    """

    def load(self, request: SecretRequest) -> "Mapping[str, str]":
        """Return the values this provider can supply, keyed by name."""
        ...  # pragma: no cover - a Protocol body


@dataclass(frozen=True)
class ProviderSpec:
    """One entry in the `secrets:` block, validated but not yet loaded.

    Parsing and loading are separate so a catalogue can be checked for shape without
    importing anybody's Python -- `config.load` stays a pure read-and-validate, and the
    code execution happens exactly where a reader expects it, in `build_store`.
    """

    #: `module.path:Attr` or `./relative/file.py:Attr`, exactly as written.
    ref: str
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return self.ref.rsplit(_REF_SEPARATOR, 1)[0]

    @property
    def attribute(self) -> str:
        return self.ref.rsplit(_REF_SEPARATOR, 1)[1]

    @property
    def is_path(self) -> bool:
        """Whether the target names a file rather than an importable module.

        A `.py` suffix, not a leading `./`: an absolute `/etc/mcp-gateway/vault.py` is a
        path too, and `mypkg.vault` is not one however it is spelled.
        """
        return self.target.endswith(".py")


def parse(raw: Any, *, where: str) -> tuple[ProviderSpec, ...]:
    """Validate a `secrets:` block. `raw` may be `None`, meaning the file alone.

    Refuses unknown keys the way [`config.py`](config.md) does everywhere else. A block
    that half-applies is worse than one that refuses: a typo'd `provdier:` that parsed to
    "no providers" would start a daemon that silently reads only the local file, and the
    symptom is a 401 from a third party rather than an error naming the line.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SecretProviderError(f"{where}: must be a mapping, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _BLOCK_KEYS)
    if unknown:
        raise SecretProviderError(
            f"{where}: unknown key(s) {unknown}. Allowed here: {sorted(_BLOCK_KEYS)}."
        )

    entries = raw.get("providers")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise SecretProviderError(
            f"{where}.providers: must be a list, got {type(entries).__name__}. Providers "
            f"are ordered -- the first one to answer a key wins -- so a mapping, which "
            f"would leave that order to chance, is not accepted."
        )
    return tuple(
        _parse_entry(entry, where=f"{where}.providers[{index}]")
        for index, entry in enumerate(entries)
    )


def _parse_entry(raw: Any, *, where: str) -> ProviderSpec:
    if isinstance(raw, str):
        # The short form. `- provider: "x:Y"` with no options is the common case and does
        # not deserve two lines.
        return ProviderSpec(ref=_validate_ref(raw, where=where))
    if not isinstance(raw, dict):
        raise SecretProviderError(
            f"{where}: expected a mapping or a 'module:Attr' string, got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - _PROVIDER_KEYS)
    if unknown:
        raise SecretProviderError(
            f"{where}: unknown key(s) {unknown}. Allowed here: {sorted(_PROVIDER_KEYS)}."
        )
    if "provider" not in raw:
        raise SecretProviderError(f"{where}: 'provider' is required")
    options = raw.get("options")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise SecretProviderError(
            f"{where}.options: must be a mapping, got {type(options).__name__}"
        )
    return ProviderSpec(
        ref=_validate_ref(raw["provider"], where=f"{where}.provider"), options=dict(options)
    )


def _validate_ref(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecretProviderError(
            f"{where}: expected 'module.path:Attr' or './file.py:Attr', got {value!r}"
        )
    ref = value.strip()
    if "${" in ref:
        # Caught here rather than left to fail as a missing module, because the failure it
        # produces otherwise ("no module named '${VAULT_DIR}/vault'") reads as a path bug
        # and sends the operator looking in the wrong place entirely.
        raise SecretProviderError(
            f"{where}: {ref!r} contains a ${{VAR}} reference. The secrets: block decides "
            f"how the store is built, so it is parsed before any store exists and nothing "
            f"here can be interpolated. A provider that needs a credential of its own "
            f"reads it itself."
        )
    target, separator, attribute = ref.rpartition(_REF_SEPARATOR)
    if not separator or not target or not attribute:
        raise SecretProviderError(
            f"{where}: {ref!r} is missing the ':Attribute' half. Write "
            f"'mypkg.vault:VaultProvider' or './providers/vault.py:VaultProvider' -- the "
            f"attribute is named explicitly because one file may hold more than one."
        )
    if not attribute.isidentifier():
        raise SecretProviderError(f"{where}: {attribute!r} is not a valid Python identifier")
    return ref


def _import_target(spec: ProviderSpec, *, base_dir: Path | None, where: str) -> Any:
    """Import the module half of a ref and return it."""
    if not spec.is_path:
        try:
            return importlib.import_module(spec.target)
        except ImportError as exc:
            raise SecretProviderError(
                f"{where}: cannot import {spec.target!r}: {exc}. A provider given as a "
                f"dotted path must be importable by the interpreter running the gateway; "
                f"give a path ending in .py to load a file directly."
            ) from exc

    path = Path(spec.target).expanduser()
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    path = path.resolve()
    if not path.is_file():
        raise SecretProviderError(f"{where}: no such provider file: {path}")

    # A module name that cannot collide with anything importable, and stable across
    # reloads so a provider module's own globals survive one -- a client holding a pooled
    # connection should not be rebuilt because someone toggled `enabled:` on a backend.
    module_name = f"mcp_gateway._secret_provider_{abs(hash(str(path))):x}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:  # pragma: no cover - defensive
        raise SecretProviderError(f"{where}: {path} is not loadable as a Python module")
    module = importlib.util.module_from_spec(module_spec)
    # Registered before execution so a provider file containing a dataclass or a pickle
    # target can find its own module, which is how `module_from_spec` is meant to be used.
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise SecretProviderError(f"{where}: {path} failed to import: {exc!r}") from exc
    return module


def load_provider(
    spec: ProviderSpec, *, base_dir: Path | None = None, where: str | None = None
) -> SecretProvider:
    """Import a `ProviderSpec` and instantiate it. Raises `SecretProviderError`.

    The attribute may be a class or a factory. A class is constructed with the options as
    keyword arguments -- `VaultProvider(addr=..., mount=...)` -- because that is how a
    Python programmer expects to receive configuration, and it means a provider taking no
    options needs no `__init__` at all. An attribute that is already an instance with a
    `load` method is taken as-is, which is what a module-level singleton looks like.
    """
    site = where or f"secrets.providers[{spec.ref}]"
    module = _import_target(spec, base_dir=base_dir, where=site)
    try:
        attribute = getattr(module, spec.attribute)
    except AttributeError:
        raise SecretProviderError(
            f"{site}: {spec.target} has no attribute {spec.attribute!r}"
        ) from None

    if inspect.isclass(attribute) or (callable(attribute) and not hasattr(attribute, "load")):
        try:
            instance = attribute(**spec.options)
        except TypeError as exc:
            raise SecretProviderError(
                f"{site}: {spec.attribute} rejected its options "
                f"{sorted(spec.options)}: {exc}"
            ) from exc
        except Exception as exc:
            raise SecretProviderError(
                f"{site}: {spec.attribute} failed to initialise: {exc!r}"
            ) from exc
    else:
        instance = attribute

    if not callable(getattr(instance, "load", None)):
        raise SecretProviderError(
            f"{site}: {spec.attribute} has no callable 'load'. A secret provider is any "
            f"object with load(request) -> Mapping[str, str]; see secret_providers.md."
        )
    return instance


def _coerce(values: Any, *, site: str) -> dict[str, str]:
    """Check what a provider handed back, before any of it reaches a store.

    Strict, and about the *types* only. A provider returning `{"TOKEN": None}` for a key it
    could not find would otherwise put a `None` into the store, where it is
    indistinguishable from an absent key until something tries to put it in a subprocess
    environment and gets a `TypeError` from `Popen` -- a stack trace three modules from the
    cause.
    """
    if values is None:
        return {}
    if not isinstance(values, dict):
        try:
            values = dict(values)
        except (TypeError, ValueError) as exc:
            raise SecretProviderError(
                f"{site}: load() returned {type(values).__name__}, expected a mapping of "
                f"name to value"
            ) from exc
    out: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise SecretProviderError(
                f"{site}: load() returned a non-string key {key!r}"
            )
        if not isinstance(value, str):
            raise SecretProviderError(
                f"{site}: load() returned a non-string value for {key!r} "
                f"({type(value).__name__}). Omit a key you cannot supply; a later provider "
                f"or gateway.env may still have it."
            )
        out[key] = value
    return out


def referenced_keys(config: "GatewayConfig | None") -> frozenset[str]:
    """Every `${NAME}` the catalogue mentions, plus the daemon's own access key.

    This is `SecretRequest.keys`, and it is what lets a remote provider be asked for named
    paths instead of needing permission to list a whole mount.

    **Disabled servers are included**, unlike `secrets.missing_for`, and the two differ on
    purpose. `missing_for` answers "what is broken right now", where a parked backend is
    deliberately not the operator's problem. This answers "what might this catalogue want",
    and fetching a key for a backend somebody is about to enable costs one lookup, while
    not fetching it costs a reload that fails on the first line of the block someone just
    uncommented.
    """
    # Imported here rather than at module scope: `secrets.py` keeps `config` behind
    # TYPE_CHECKING to avoid a cycle, and this module sits on the same edge.
    from mcp_gateway.secrets import _REFERENCE_RE  # noqa: PLC0415
    from mcp_gateway.transport_ws import ACCESS_KEY_SECRET_NAME  # noqa: PLC0415

    keys = {ACCESS_KEY_SECRET_NAME}
    if config is None:
        return frozenset(keys)
    for spec in config.servers.values():
        templates = list(spec.env.values())
        if spec.cwd is not None:
            templates.append(spec.cwd)
        for template in templates:
            for match in _REFERENCE_RE.finditer(template):
                if not match.group(1):  # `$${NAME}` is an escape, not a reference
                    keys.add(match.group(2))
    return frozenset(keys)


def build_store(
    config: "GatewayConfig | None",
    env_path: Path,
    *,
    required: bool = False,
) -> SecretStore:
    """The whole chain, in one call: every provider in order, then `gateway.env`.

    This is what replaced `secrets.load(env_path)` at the three sites that build a store --
    `--check`, daemon startup, and reload. It keeps that function's contract exactly:
    it builds a new object or raises, and never mutates anything, which is the entire
    reason a reload that refuses leaves a running daemon untouched.

    **First answer wins, and the file is last.** The order reads oddly for a fallback chain
    until you name the case it is for: a deployment moves its backend tokens to Vault and
    leaves `WS_ACCESS_KEY` in the file it is already in. Putting the file first would make
    a stale local copy of a rotated credential quietly beat the live one -- the same
    failure the duplicate-key rule in [`secrets.md`](secrets.md) exists to prevent, one
    file up.
    """
    specs = config.secret_providers if config is not None else ()
    base_dir = config.source.parent if config is not None and config.source is not None else None
    request_config_path = config.source if config is not None else None
    request_keys = referenced_keys(config)

    values: dict[str, str] = {}
    origins: dict[str, str] = {}
    for index, spec in enumerate(specs):
        site = f"secrets.providers[{index}] ({spec.ref})"
        provider = load_provider(spec, base_dir=base_dir, where=site)
        request = SecretRequest(
            keys=request_keys, options=spec.options, config_path=request_config_path
        )
        try:
            supplied = provider.load(request)
        except SecretProviderError:
            raise
        except Exception as exc:
            # Deliberately fatal. A provider that cannot reach its backing store is not the
            # same thing as a machine nobody has configured yet, and starting anyway would
            # hand every backend an empty token and turn one clear error into N confusing
            # ones from third-party APIs. See the module doc.
            raise SecretProviderError(f"{site}: load() failed: {exc!r}") from exc
        for key, value in _coerce(supplied, site=site).items():
            if key in values:
                continue
            values[key] = value
            origins[key] = spec.ref
        logger.info(
            "secret provider %s supplied %d key(s)", spec.ref, sum(1 for k in origins if origins[k] == spec.ref)
        )

    from_file = load_env_file(env_path, required=required)
    for key in from_file.keys():
        if key in values:
            continue
        value = from_file.get(key)
        if value is not None:
            values[key] = value
            origins[key] = FILE_ORIGIN

    if specs:
        logger.info(
            "secret store built from %d provider(s) and %s: %d key(s)",
            len(specs),
            env_path,
            len(values),
        )
    return SecretStore(source=env_path, _values=values, origins=origins)
