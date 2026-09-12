# `config.py`

`servers.yaml` → `GatewayConfig`. No secrets, no spawning.

## The split that makes reload safe

This module decides what a launch *would* look like; `backend.py` (phase 2) resolves
credentials into it at spawn time. A `ServerSpec` holds `env` as the **template** strings
from the file — `"${GITHUB_TOKEN}"`, not the token — which is why a spec is safe to log, to
return over `/admin`, and to hold in a diff against the previous config.

It is also why `load()` returning a new object and never mutating anything is the whole
guarantee behind reload: a catalogue that fails to parse leaves the running one untouched,
because there was never a moment when half of it had been applied.

## Validation is loud

A catalogue that half-parses is worse than one that refuses. An unknown key at any level is
an error naming the file, the entry, and the keys that *are* allowed — because the
overwhelmingly common cause is a typo or a key borrowed from another tool's schema, and
ignoring it silently produces a server running with settings the operator believes they
applied.

`_as_bool` is strict for a related reason: YAML 1.2 reads `yes` and `on` as the strings
they are, so a file written with YAML 1.1 habits would otherwise set `enabled` to a truthy
string and disable nothing.

## `${VAR}` is refused in `command` and `args`

With an error that explains why, because the refusal looks like an arbitrary limitation
until you know: argv is world-readable through `ps` on every platform this runs on, so a
secret on a command line publishes itself to every other account on the machine at the
moment it is being used to protect something. It belongs in the `env` block, which is
private to the child.

Interpolation is permitted in `env` values and in `cwd`, and nowhere else.

## JSON works for free

The loader is YAML 1.2 in safe, pure-Python mode, and YAML 1.2 is a superset of JSON. So
`--config servers.json` — including a file pasted straight out of a Claude Desktop config —
goes through the identical code path, with no branch and no second parser. There is
deliberately no `tomllib` alternative: it would be a third grammar for no gain.

`pure=True` is load-bearing, not a default worth inheriting: it keeps this off
`ruamel.yaml.clib`, a C extension that would put a per-architecture wheel into a release
that builds for both amd64 and arm64.

## Schema

```yaml
version: 1                     # optional; must be 1
defaults:                      # optional; each key optional
  timeout: 30.0                # per-request seconds
  startup_timeout: 20.0        # spawn + initialize + first listing
  env_mode: curated            # curated | inherit
  cwd: null
servers:                       # required
  github:
    command: npx               # required
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
    env_passthrough: ["HTTPS_PROXY"]
    env_mode: curated
    cwd: "~/src/foo"
    timeout: 60.0
    startup_timeout: 20.0
    enabled: true              # default true
    required: false            # default false
    description: "GitHub issues and PRs"
```

Server names are validated by [`naming.py`](naming.md): no `__`, no `/` or `:`, and
`gateway` is reserved.

## `defaults:` is folded in, and also kept

Every key under `defaults:` is applied to each server that omits it, at parse time, by
`_parse_entry`. Nothing at runtime consults the block again — a `ServerSpec` is complete on
its own, which is what makes it safe to hand around.

`GatewayConfig.defaults` keeps the block anyway, and it is the one piece of this module
that exists for a reader outside it. An editor offering to *add* a server has to show what
leaving a field blank will do, and the folded specs cannot say: a spec reading `timeout:
30.0` looks identical whether the file set it, `defaults:` set it, or nothing did.
`admin.config.get` reports both, and the admin UI uses the block for its placeholders.

## `enabled` and `required`

`enabled: false` means never launched, never resolved, never checked — the way to park a
backend without deleting its entry.

`required: false` (the default) means an unresolvable `${VAR}` **skips that server**, logs
an ERROR naming the key and the field that referenced it, and lets the daemon start with
the rest. One expired token should not cost the operator every other tool. The skipped
server still appears in `gateway__list_backends` with `status: "failed"` and the reason,
and that discoverability is what makes skip-and-continue defensible rather than silent.

`required: true` makes the same condition fatal — exit 2 — for the deployment where silent
degradation is unacceptable.
