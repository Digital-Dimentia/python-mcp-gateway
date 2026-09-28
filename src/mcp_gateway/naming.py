"""Compose and split the public names the gateway exposes.

Pure functions, no I/O, no logging. Everything here is a string transformation whose
correctness is the difference between a call reaching the right backend and reaching the
wrong one.

## The one load-bearing coupling in this module

Public tool and prompt names are `f"{server}__{name}"`, and routing is
`public.split("__", 1)` — a pure string split with **no lookup table**. That is only
unambiguous because `validate_server_name` refuses a server name containing `__`, so the
first occurrence is always the separator and never part of the server's own name.

The pair buys something worth the coupling: a backend tool that already contains the
separator, like `create__issue`, needs no escaping and works unmodified
(`github__create__issue` splits to `github` / `create__issue`), and there is no table to
go stale when a backend renames its tools after a `list_changed`.

Break either half and the other silently misroutes. `tests/test_naming.py` asserts both.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

#: Separates the backend's name from the backend's own name for that tool or prompt.
SEPARATOR = "__"

#: The scheme every proxied resource URI is rewritten into.
RESOURCE_SCHEME = "mcpgw"

#: Reserved: the gateway's own meta-tools are `gateway__*`, and a backend that could take
#: this name could shadow them -- offering the model a `gateway__reload_config` of its own
#: devising. Refused at config load, which is the only place a server name is chosen.
RESERVED_SERVER_NAMES = frozenset({"gateway"})

#: A server name must be a plausible identifier, must not contain the separator, and must
#: survive being the authority component of a `mcpgw://` URI -- hence no `/` or `:`, which
#: `urlsplit` would read as a path or a port.
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SERVER_NAME_MAX = 32

#: What real clients accept for a tool name. The spec does not impose this; implementations
#: do, and a name they reject is worse than a name we never offered -- the model sees the
#: tool, calls it, and gets a protocol error it cannot act on.
_PUBLIC_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: One RFC 6570 expression, braces included. `[^{}]*` rather than `.*?` because 6570 braces
#: never nest, so an unmatched `{` should not be able to swallow the expression after it.
_TEMPLATE_EXPR_RE = re.compile(r"\{[^{}]*\}")


class NamingError(ValueError):
    """A name that cannot be composed, split, or trusted.

    A `ValueError` because that is what it is -- a malformed argument -- and because
    `errors.to_error_object` maps `ValueError` to `-32602`, which is the right answer when
    the malformed name came from a client's `tools/call`.
    """


def validate_server_name(name: str) -> None:
    """Raise unless `name` is usable as a namespace prefix and a URI authority."""
    if not name:
        raise NamingError("server name must not be empty")
    if len(name) > _SERVER_NAME_MAX:
        raise NamingError(
            f"server name {name!r} is {len(name)} characters; the limit is {_SERVER_NAME_MAX}"
        )
    if SEPARATOR in name:
        raise NamingError(
            f"server name {name!r} contains {SEPARATOR!r}, which separates the server from "
            f"the tool in every public name. A name containing it would make routing "
            f"ambiguous: {name}{SEPARATOR}x could mean two different things."
        )
    if not _SERVER_NAME_RE.match(name):
        raise NamingError(
            f"server name {name!r} must start with a letter or digit and contain only "
            f"letters, digits, '_' and '-'. It becomes the authority of a "
            f"{RESOURCE_SCHEME}:// URI, so '/' and ':' are excluded too."
        )
    if name in RESERVED_SERVER_NAMES:
        raise NamingError(
            f"server name {name!r} is reserved for the gateway's own meta-tools "
            f"({name}{SEPARATOR}list_backends and friends)."
        )


def compose(server: str, name: str) -> str:
    """The public name for `name` as published by `server`."""
    return f"{server}{SEPARATOR}{name}"


def split(public: str) -> tuple[str, str]:
    """Split a public name into `(server, backend_name)`.

    A pure split on the *first* separator. See the module docstring for why that is
    unambiguous, and why there is no lookup table here.
    """
    server, found, name = public.partition(SEPARATOR)
    if not found or not server or not name:
        raise NamingError(
            f"{public!r} is not a namespaced name; expected <server>{SEPARATOR}<name>"
        )
    return server, name


def is_publishable(public: str) -> bool:
    """Whether a composed name is one a client will actually accept.

    Callers drop a name that fails this and record it as skipped, rather than publishing
    something the client rejects at call time.
    """
    return bool(_PUBLIC_NAME_RE.match(public))


def encode_resource_uri(server: str, uri: str) -> str:
    """Rewrite a backend's resource URI into the gateway's own address space.

    **Why rewrite rather than pass the original through and search for it later.** Two
    backends can publish the identical URI -- two filesystem servers rooted differently
    both offering `file:///README.md` -- and then which one answers a `resources/read` is
    decided by dict ordering. Rewriting makes every address unambiguous by construction,
    at the cost of a URI the client cannot interpret on its own. Clients treat resource
    URIs as opaque handles, so that cost is nominal.

    **Everything outside `{...}` is percent-encoded; everything inside is passed through
    verbatim.** A `uriTemplate` is expanded *client-side*, so the expression the client
    receives has to be byte-identical to the one the backend published -- and that means the
    modifiers and operators too, not just the braces. `quote(uri, safe="{}")` spared the
    braces alone, which silently turned `git://repositories/{repo*}` into `{repo%2A}`: still
    decodable, but naming a variable that does not exist, so no client could expand it. All
    six operators (`+ # / ; ? &`) and both modifiers (`*`, `:n`) survive this way.

    The one casualty is a *concrete* URI containing a literal brace, which is
    indistinguishable from a template expression; that is documented as unsupported rather
    than worked around. A stray unmatched brace is not spared -- the regex requires a pair.
    """
    out: list[str] = []
    pos = 0
    for match in _TEMPLATE_EXPR_RE.finditer(uri):
        out.append(quote(uri[pos : match.start()], safe=""))
        out.append(match.group(0))
        pos = match.end()
    out.append(quote(uri[pos:], safe=""))
    return f"{RESOURCE_SCHEME}://{server}/{''.join(out)}"


def decode_resource_uri(public_uri: str) -> tuple[str, str]:
    """Reverse `encode_resource_uri`, returning `(server, original_uri)`."""
    parts = urlsplit(public_uri)
    if parts.scheme != RESOURCE_SCHEME:
        raise NamingError(
            f"{public_uri!r} is not a gateway resource URI (expected scheme "
            f"{RESOURCE_SCHEME!r}, got {parts.scheme!r})"
        )
    # Split the authority off by hand rather than trusting `urlsplit`'s path/query/fragment
    # carve-up. A gateway resource URI carries no query or fragment of its own, so a literal
    # `?` or `#` after the authority is always the backend's: either the `{?q}`/`{#f}` form of
    # a template we published verbatim, or what a client made of it by expanding one. Reading
    # `parts.path` would drop everything from that character on. `urlsplit` is kept only for
    # the scheme check above, which is what tells us the URI is ours at all.
    rest = public_uri[len(parts.scheme) + len("://") :]
    server, found, encoded = rest.partition("/")
    if not server:
        raise NamingError(f"{public_uri!r} names no backend")
    if not found:
        raise NamingError(f"{public_uri!r} carries no resource")
    return server, unquote(encoded)


def compose_display_name(server: str, name: str) -> str:
    """A human-facing label for a resource or template.

    `server/name`, not the `__` form: this is read by a person in a picker, never split by
    code, and a slash is what a person reads as "from".
    """
    return f"{server}/{name}"
