# `config_writer.py` — the only module that writes `servers.yaml`

[`config.py`](config.md) reads a catalogue and mutates nothing. That is not an accident of
how it was written; it is the guarantee reload rests on, and the reason a parse failure
during a reload leaves the running set untouched. Adding a writer to it would have made
"the reader cannot write" a claim nobody could check.

So the writer is a separate module, imported by [`admin_channel.py`](admin_channel.md) and
by nothing else.

## Four rules, in this order

### 1. Round-trip, so the comments survive

`servers.yaml` is committed, and most of it is prose explaining why each knob is set the way
it is — including the warnings about what must never go in it. An editor that dropped that
would be a tool you can use exactly once before the file stops being worth reading.

`ruamel.yaml` in `typ="rt"` mode preserves comments, key order and quoting style. `pure=True`
keeps it off `ruamel.yaml.clib`, for the reason `pyproject.toml` spells out: a C extension
would put a per-architecture wheel into a release that builds for amd64 *and* arm64.

Note the asymmetry: the reader uses `typ="safe"` and the writer `typ="rt"`. The reader has
no use for comments and should have the smaller, stricter loader; the writer needs nothing
else.

### 2. Validate before writing

The candidate tree is serialised, handed to `config.parse`, and written only if that
succeeds. `parse` is the same function the daemon loads with, so there is no second opinion
about what is valid, and no way for the writer to accept something the reader will reject
three seconds later when the reload fires.

A refused write leaves the file byte for byte as it was. There is no moment at which half
of it has been applied.

This matters more than it looks. The person using the UI is editing the file the daemon is
about to re-read, and an invalid write is not "an error message" — it is a daemon that will
not come back.

### 3. A literal `env` value is a warning, not a refusal

`servers.yaml` is committed and must never hold a credential *value*; that is the entire
argument for `gateway.env`. So any `env` value with no `${VAR}` reference in it comes back
in a `warnings` list, which the UI shows in red and the daemon logs.

It cannot be a refusal. `MOCK_MCP_SCHEMA_ZOO: "1"` in `servers.dev.yaml` is a perfectly
legitimate literal, and a writer that refused it would be one people route around by editing
the file by hand — which costs the warning *and* the validation, and is strictly worse than
a warning someone reads.

The check is deliberately dumb: it looks for `${NAME}` and nothing else. Guessing which
strings "look like a secret" would produce false confidence in both directions.

### 4. Atomically, with a backup

Temp file in the same directory, original mode copied onto it, `os.replace`. A crash
mid-write leaves the old file intact rather than a truncated one.

`servers.yaml.bak` is written first, by **copy** rather than rename, so the original keeps
its inode — a daemon holding the path, or an editor with the file open, is not silently left
pointing at the backup. The backup is for the edit that was valid and still wrong, which
validation cannot catch by construction.

## Merge, don't replace

`update_server` merges: an edit that toggles `enabled` does not drop a `cwd` the UI did not
happen to send. A key given as `None`, `""`, `[]` or `{}` is *removed* rather than written,
because an empty `args: []` means exactly what its absence means and is noise in a file
people read.

`set_servers` replaces the whole mapping, for a UI editing the catalogue as a whole, and
still keeps `version`, `defaults` and their comments. An entry that survives keeps its own
comments, because the round-trip tree holds them on the node; replacing one entry loses that
entry's comments and nothing else's.

## The path is the daemon's, not the caller's

`admin_channel._write` passes `self.gateway.config_path`. No admin method takes a path.

A method that did would turn "edit the catalogue this daemon was started with" into "write
YAML anywhere this process can write", which is a materially larger capability and one
nobody asked for. The daemon already runs as a user with a credential file; that is not a
capability to hand out for free.

## What is not here

Nothing writes `gateway.env`. [`admin.md`](admin.md) makes that argument at length; the
short version is that a process which can be talked into writing a credential has a blast
radius of every credential on the machine. Config writing landing on `/admin` did not move
that line — it is *why* config writing is on `/admin` and not on `/mcp`.

The UI's answer to a missing credential is `admin.secrets.missing`, which names the key and
stops there. See [`secrets.md`](secrets.md).
