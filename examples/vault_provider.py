"""A worked secret provider, with no dependency beyond the standard library.

Copy this file, keep the shape, replace `_read` with a call to whatever actually holds your
credentials -- `hvac` for Vault, `boto3` for AWS Secrets Manager, `requests` for an internal
service. The gateway never imports this module itself; `servers.yaml` points at it:

    secrets:
      providers:
        - provider: "./examples/vault_provider.py:JsonFileProvider"
          options:
            path: "/run/secrets/mcp-gateway.json"

Then every `${NAME}` in the catalogue resolves from that JSON file first, and from
`gateway.env` only for the keys the file does not carry.

See `src/mcp_gateway/secret_providers.md` for the reasoning behind the interface -- in
particular why `load` hands back a whole snapshot rather than resolving one key at a time,
and why omitting a key and raising mean two different things.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonFileProvider:
    """Reads a flat `{"NAME": "value"}` JSON document.

    A stand-in for a real remote store, and a real provider in its own right: a container
    that mounts its secrets as a JSON file (Kubernetes projected volumes, Docker secrets,
    a systemd credential) can use this as it is.

    Options arrive as keyword arguments, so this signature *is* the schema for the
    `options:` mapping -- a missing `path` is a `TypeError` that the gateway reports as
    "JsonFileProvider rejected its options", naming the file that configured it.
    """

    def __init__(self, path: str, *, required: bool = True) -> None:
        self._path = Path(path).expanduser()
        self._required = required

    def load(self, request: Any) -> dict[str, str]:
        """Return the requested keys this document carries.

        Three behaviours worth copying into a real provider:

        - **Only the keys that were asked for.** `request.keys` is every `${NAME}` the
          catalogue references. Filtering to it means a document holding a hundred
          unrelated credentials puts ninety of them nowhere near this process.
        - **A key that is absent is simply omitted**, not returned as `None` or `""`. A
          later provider, or `gateway.env`, may still have it -- and if nobody does, the
          gateway's existing missing-secret report names the config site that wanted it.
        - **A store this provider cannot read raises.** That is how a provider says "I am
          broken" as opposed to "I do not have these", and it is what stops a daemon from
          starting with empty tokens. Set `required: false` in options to make an absent
          file mean an empty answer instead, which is what you want for a provider that is
          genuinely optional on some hosts.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if self._required:
                raise
            return {}
        if not isinstance(raw, dict):
            raise ValueError(f"{self._path}: expected a JSON object of name -> value")

        return {
            key: value
            for key, value in raw.items()
            # `request.keys` may be ignored -- returning everything is allowed, and the
            # gateway keeps keys nobody asked for. Honouring it is just better hygiene.
            if key in request.keys and isinstance(value, str)
        }
