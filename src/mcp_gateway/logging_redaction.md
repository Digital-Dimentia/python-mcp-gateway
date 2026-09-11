# `logging_redaction.py`

Scrub known secret values out of every log record in the process.

## Why the root logger

Not because our own modules cannot be trusted. Because the interesting leak is not ours.

`MCPStdioClient._drain_stderr` logs each backend's stderr verbatim at DEBUG, and it should:
a backend that fails to start explains itself there and nowhere else. But some servers
print the credential they tried on a 401, helpfully. That token would reach our log through
a code path that never saw the value and could not possibly know to hide it.

Attaching here catches that, and catches any third-party library's logging as well. This
filter is the difference between "the daemon logged a backend's startup noise" and "the
daemon wrote every credential on the machine into a log file".

## Handlers, not the logger

A `Filter` attached to a *logger* is consulted only for records logged through that logger
— not for records propagating up from its children, which is every record this process
cares about. Handler-level filters run on everything that reaches them.

## Before formatting, on both `msg` and `args`

A filter runs on the record, while `record.msg` is still a format string and `record.args`
are still separate values. Both are scrubbed, because a secret arrives as an argument at
least as often as it is interpolated by the caller — and scrubbing only the rendered line
would mean trusting every handler to route through one formatter.

Values are applied longest-first (see `SecretStore.redactable_values`) so that a value
containing another — `Bearer <token>` alongside `<token>` — is replaced before its
substring, leaving one `***` rather than `***` glued to a surviving fragment.

## Re-installed on reload

`install_redaction` **replaces** any prior filter rather than adding one. That matters in
both directions after a credential rotation: the old value must stop being scrubbed, or a
log line that legitimately contains it becomes unreadable, and the new value must start.

## The `MIN_REDACTABLE_LENGTH` floor

Below four characters, redaction does more harm than good — a two-character secret occurs
inside ordinary English words, and scrubbing it would mangle every log line in the process.
A value that short is not a secret worth having.

## What this cannot do

It scrubs values it was told about. A secret minted at runtime — a session token from an
OAuth exchange, a presigned URL — passes through untouched. That is a real limit, and it is
why `--debug` stays a deliberate choice rather than a default.
