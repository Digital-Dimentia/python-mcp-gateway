# `secrets.py`

`gateway.env`, and the `${VAR}` resolution that reads from it. The only module that holds
a secret value.

## Why the parser is hand-rolled

Not to save a dependency. To own the grammar.

dotenv implementations disagree with one another, and with their own past versions, about
`export`, about interpolation inside the file, about multiline values, and about whether
`#` in an unquoted value starts a comment. For most projects that is a footnote. For a
credential consolidator it is the product: a file whose *meaning* moves under a pinned
dependency's minor bump can turn a token into a prefix of itself, and the symptom arrives
as a 401 from a third party hours later.

The grammar is small enough to state completely, and it will not move:

```
UTF-8, line-oriented. Blank lines and #-comments ignored. Leading `export ` stripped.
KEY matches ^[A-Za-z_][A-Za-z0-9_]*$ — anything else is an error naming the line number.
VALUE:  '...' literal | "..." with \n \t \r \\ \" | otherwise raw-to-end-of-line, stripped
```

## Three rules stated loudly, because implementations disagree

**No inline comment stripping in unquoted values.** `#` is a legal character in a token.
`KEY=abc#def` is `abc#def`. Quote it if a trailing comment is wanted.

**No interpolation inside this file, and no multiline values.** Interpolation happens in
exactly one direction and one place: `servers.yaml` → the store, via `interpolate`.

**A duplicate key is an error**, naming both line numbers. Silently keeping the last is how
a freshly rotated credential gets shadowed by a stale one further up the file.

## An empty value is present, not missing

`TOKEN=` is how someone spells "deliberately blank". Treating it as absent would be a
second rule nobody asked for, and one whose error message — "missing secret TOKEN" —
names a key that is plainly in the file.

## `interpolate` never reads `os.environ`

This is the security thesis, stated in one function. Falling back to the ambient
environment would make a backend's credential depend on whichever shell launched the
daemon, produce the classic works-on-my-machine, and defeat curated env mode by the back
door. The only deliberate route to a parent variable is a server's `env_passthrough` list,
which names the variable explicitly, per server.

`${NAME}` only. **Bare `$NAME` is not interpolated**: `$` is common inside generated tokens
and real passwords, and a bare-name rule would silently eat part of one. `$${NAME}` escapes
to a literal.

The optional `missing` list is what `--check` uses: unresolvable references accumulate
instead of raising, so one run reports every problem rather than the first.

## Three layers of "values are never logged"

1. `SecretStore.__repr__` renders key names only. A store that rendered its values would
   put every credential into any traceback or `pytest` assertion diff that included it —
   and a traceback is the one place nobody thinks to check.
2. [`logging_redaction.py`](logging_redaction.md) scrubs known values out of every record.
3. That filter sits on the **root** logger, which is why a backend that prints its own
   token to stderr comes out redacted too — see that module's doc.

## File mode

A group- or world-readable `gateway.env` gets a WARNING naming it and suggesting
`chmod 600`. Deliberately not fatal: refusing to start over a permission bit is hostile on
WSL's DrvFs where modes are synthetic, and in CI where a runner just wrote the file.

**Not checked at all off POSIX.** Windows reports `0o666` for every readable file whatever
its ACL says, so the check there cannot distinguish a private file from an exposed one — it
fired on every start of the bundled desktop daemon, about a file in the user's own profile,
advising a `chmod` that machine does not have. The honest options were an unconditional
warning or none, and a warning that is always on is one nobody reads. Privacy on Windows is
an ACL question, and reading ACLs is not something this module does.

## A missing file is an empty store

Not an error, unless the caller asks for `required`. The daemon has to start on a machine
nobody has configured yet — it still serves its admin tools, and those are how an operator
finds out what is missing. See [`config.md`](config.md) for what happens to a server whose
secret cannot be resolved.
