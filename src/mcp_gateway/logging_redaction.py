"""Scrub known secret values out of every log record in the process.

Thirty lines, and the highest-value thirty in the project.

## Why it goes on the *root* logger

Not because our own modules cannot be trusted -- because the interesting leak is not ours.
`MCPStdioClient._drain_stderr` logs each backend's stderr verbatim at DEBUG, which is the
right thing to do: a backend that fails to start explains itself there and nowhere else.
But a backend that prints its own token to stderr -- and some do, on a 401, helpfully
echoing the credential it tried -- would put that token into our log through a code path
that never saw the value and could not know to hide it.

Installing on the root logger catches that, and catches a third-party library's logging
too. The filter is the only thing standing between "the daemon logged a backend's startup
noise" and "the daemon wrote every credential on the machine to a file".

## Why it replaces before formatting

A `logging.Filter` runs on the record, so `record.msg` is still the format *string* and
`record.args` are still separate. Both are scrubbed: a value arrives as an argument at
least as often as it is interpolated in by the caller, and scrubbing only the rendered
line would mean relying on every handler to route through one formatter.

## What it cannot do

It scrubs values it was told about. A secret the store has never seen -- one a backend
minted at runtime, a session token from an OAuth exchange -- passes through. That is a
real limit, and it is why `--debug` remains a deliberate choice rather than a default.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from mcp_gateway.secrets import MIN_REDACTABLE_LENGTH, SecretStore

#: What a redacted value is replaced with. Fixed-width and obviously not data, so a log
#: line that contained a secret still reads as a log line that contained *something*.
PLACEHOLDER = "***"


class RedactingFilter(logging.Filter):
    """Replace every known secret value in a record with `PLACEHOLDER`.

    Values are applied longest-first (see `SecretStore.redactable_values`) so a value
    containing another is replaced before its substring, leaving one `***` rather than
    `***` glued to a surviving fragment.
    """

    def __init__(self, store: SecretStore, extra: Iterable[str] = ()) -> None:
        super().__init__()
        values = set(store.redactable_values())
        values.update(v for v in extra if v and len(v) >= MIN_REDACTABLE_LENGTH)
        # Longest-first, as `SecretStore.redactable_values` does, for the same reason: a
        # value containing another must be replaced before its substring.
        self._values = sorted(values, key=len, reverse=True)

    def _scrub(self, value: Any) -> Any:
        if not isinstance(value, str):
            # Deliberately shallow: a value nested inside a dict or a dataclass argument is
            # not reached. Rendering it with `str()` here to scrub it would change what the
            # formatter later produces, which is a worse failure than the one it prevents.
            return value
        for secret in self._values:
            value = value.replace(secret, PLACEHOLDER)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        record.msg = self._scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._scrub(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            # The `%(name)s` style. Rare, but `logging` supports it and a caller using it
            # would otherwise bypass the filter entirely.
            record.args = {key: self._scrub(val) for key, val in record.args.items()}
        return True


def install_redaction(store: SecretStore, extra: Iterable[str] = ()) -> RedactingFilter:
    """Put a fresh `RedactingFilter` on the root logger's handlers, replacing any prior one.

    Handlers rather than the logger itself: a `Filter` attached to a logger is consulted
    only for records logged *through that logger*, not for records propagating up from
    children -- which is every record this process cares about. Handler-level filters run
    on everything that reaches them.

    `extra` carries secrets that are not in the store. The access key is the one that
    matters: it may come from `MCP_GATEWAY_WS_KEY` rather than from `gateway.env`, and
    `websockets` logs the full HTTP request line at DEBUG -- query string included -- so
    without this, `--debug` would write `?key=<secret>` into the log. That is precisely the
    failure that makes a query parameter the wrong carrier, arriving through a library we
    do not control.

    Called again after a config reload. Replacing rather than adding matters in both
    directions: a rotated credential's *old* value must stop being scrubbed (or a log line
    legitimately containing it becomes unreadable) and the new one must start.
    """
    filt = RedactingFilter(store, extra)
    root = logging.getLogger()
    for handler in root.handlers:
        for existing in [f for f in handler.filters if isinstance(f, RedactingFilter)]:
            handler.removeFilter(existing)
        handler.addFilter(filt)
    return filt
