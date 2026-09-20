"""Read and rewrite `_meta.ui` -- MCP Apps (SEP-1865) -- at the proxy boundary.

Pure functions, no I/O. A tool that ships a user interface says so with
`_meta: {"ui": {"resourceUri": "ui://..."}}`, naming a resource the same backend publishes.
The gateway republishes resources under `mcpgw://<server>/<percent-encoded>`, and nothing
else in this process parses `_meta`, so without this module a tool reaches a client carrying
a `ui://` address that resolves for nobody.

## The authority in a `ui://` URI is never parsed. Ever.

This is the whole security argument, and it inverts the design that looks right.

`ui://zoo/panel` has `zoo` in the authority position, and a gateway that has a backend named
`zoo` is *invited* to connect the two. **Do not.** That authority is a string the backend
chose, with no relationship to the names this gateway gave its backends. The rewrite is:

    encode_resource_uri(<the backend that published this tool>, <the whole ui:// URI, verbatim>)

So a tool published by `evil` that references `ui://zoo/panel` becomes
`mcpgw://evil/ui%3A%2F%2Fzoo%2Fpanel`. Reading it asks **evil** for `ui://zoo/panel` and gets
evil's answer or `-32002`. `zoo` is never reached, and could not have been.

Resolving the authority against a server name -- the "helpful" implementation -- is a
confused-deputy vulnerability: any backend could nominate any other backend's UI and have a
host render it under that backend's name. Writing it this way does not *mitigate* that class
of bug, it removes it. The corresponding risk is that somebody later adds the resolution as
a feature, which is why `test_the_ui_authority_is_never_matched_against_a_server_name`
exists.

## Injection is ours to refuse; permissiveness is not ours to judge

`csp` and `permissions` do nothing in this gateway's own renderer. They matter because we
are a **proxy**: a downstream host turns `csp` into a real `Content-Security-Policy` header
and `permissions` into real browser grants.

CSP source lists are space-separated and directives semicolon-separated, so a backend that
smuggles `example.com; script-src *` into one entry owns that host's entire policy for that
frame. That is a header-injection primitive and a proxy must not forward it. Whether
`*.example.com` is *too permissive* is a different question, and not ours -- it is the
operator's, and a gateway that vetoed it would break working panels over a judgement it has
no standing to make. So a bare `*` passes and a `;` does not.

The same distinction drives `permissions`: a value must be a real boolean, because a host
writing `if perms.get("camera")` reads the string `"false"` as true. That is a genuine grant
manufactured from a fake denial, and it is a shape error rather than a policy one.

## Unknown keys survive

Only keys we can *misread* are validated. Everything else under `ui` is copied through,
because SEP-1865 is pre-GA and a proxy that allowlists fields becomes the reason next year's
field does not work.

The residual risk is stated rather than hidden: a future `ui` member whose value is itself a
header fragment would pass through unexamined. What would change this call is exactly that --
a named field carrying header syntax gets its own row here. An allowlist today would
certainly break every future field, to guard against one that may never exist.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from mcp_gateway import naming

logger = logging.getLogger("mcp_gateway.ui_apps")

#: The deprecated flat spelling of the reference, which SEP-1865 says will be removed before
#: GA. One constant so that removal is one line and one test.
FLAT_RESOURCE_URI_KEY = "ui/resourceUri"

#: A ceiling on a `resourceUri` before percent-encoding, which roughly triples it. Generous
#: for any real URI, small enough that a backend cannot inflate every entry of every tool
#: listing the gateway serves.
MAX_RESOURCE_URI = 2048

#: Ceilings on the CSP block. A ten-thousand-entry domain list is a header bomb aimed at
#: whatever host renders the panel, not at us.
MAX_CSP_ENTRIES = 32
MAX_DOMAIN = 255

#: The CSP directive lists SEP-1865 defines, each mapping to a directive in the host's
#: header. Unknown members of `csp` are held to the same shape (see `_clean_csp`): whatever
#: they are called, they are destined for the same header.
_CSP_LISTS = ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")

#: A CSP source or a `domain`: an optional scheme, an optional leading wildcard label, dotted
#: labels, an optional port. Deliberately no path, query, fragment, quote, space or
#: semicolon -- those are the characters that turn one entry into two directives.
_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?$")

#: Accepted verbatim as a CSP source: syntactically safe, merely permissive.
_CSP_WILDCARD = "*"

#: Anything below 0x20, plus DEL. A URI carrying one of these becomes a header value or a
#: URL on somebody's wire.
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")


def profile_of(mime_type: object) -> str | None:
    """The `profile` parameter of a media type, or `None`.

    `text/html;profile=mcp-app` -> `"mcp-app"`. Spacing and case in the parameter name vary
    between servers, so both are normalised; the value is returned as given, because it is
    an identifier the spec compares exactly.
    """
    if not isinstance(mime_type, str):
        return None
    for part in mime_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "profile":
            return value.strip().strip('"') or None
    return None


def reference_of(meta: object) -> tuple[str | None, bool]:
    """The `ui://` reference a tool's `_meta` carries, and whether the flat key held it.

    Returns `(uri, flat_present)`. The nested `_meta["ui"]["resourceUri"]` wins when both
    spellings are present -- one of them has to, and resolving the ambiguity here means
    every host downstream sees the same answer instead of each picking for itself.
    """
    if not isinstance(meta, dict):
        return None, False
    flat = meta.get(FLAT_RESOURCE_URI_KEY)
    flat = flat if isinstance(flat, str) else None
    nested_block = meta.get("ui")
    nested = nested_block.get("resourceUri") if isinstance(nested_block, dict) else None
    nested = nested if isinstance(nested, str) else None
    return (nested if nested is not None else flat), flat is not None


def _acceptable_reference(uri: str) -> bool:
    """Whether a `resourceUri` is one we will put into the gateway's address space.

    Refusing is not a judgement about the panel: it degrades the tool to a plain tool, which
    every host already knows how to render.
    """
    if not uri or len(uri) > MAX_RESOURCE_URI:
        return False
    if _CONTROL_RE.search(uri):
        return False
    # SEP-1865 says the template *is* a `ui://` resource. An `https://` or `data:` reference
    # asks a host to frame content from outside the resource system entirely, which is a
    # hostile shape rather than a spec field we have not learned yet.
    return urlsplit(uri).scheme == "ui"


def _clean_domain_list(value: object) -> list[str] | None:
    """One CSP directive's sources, keeping only entries that cannot become two directives."""
    if not isinstance(value, list):
        return None
    kept: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        if entry == _CSP_WILDCARD or (len(entry) <= MAX_DOMAIN and _DOMAIN_RE.match(entry)):
            kept.append(entry)
        if len(kept) >= MAX_CSP_ENTRIES:
            break
    return kept


def _clean_csp(block: object) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in block.items():
        # Named and unnamed directives are held to the same shape on purpose: an unknown
        # `csp` member is, by definition, bound for the same header as a known one.
        kept = _clean_domain_list(value)
        if kept is not None:
            cleaned[key] = kept
    return cleaned


def _clean_permissions(block: object) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    # The key set is deliberately open. Permissions are exactly the part of this spec that
    # will grow, and a gateway that strips next year's is the chokepoint we set out not to
    # be. The value check is the whole point: a non-bool is a grant waiting to be misread.
    return {key: value for key, value in block.items() if isinstance(value, bool)}


def sanitize_ui_block(block: object) -> dict[str, Any] | None:
    """A resource-level `_meta.ui`, with the members a host turns into policy made safe."""
    if not isinstance(block, dict):
        return None
    cleaned = dict(block)

    if "csp" in cleaned:
        csp = _clean_csp(cleaned["csp"])
        if csp is None:
            del cleaned["csp"]
        else:
            cleaned["csp"] = csp

    if "permissions" in cleaned:
        permissions = _clean_permissions(cleaned["permissions"])
        if permissions is None:
            del cleaned["permissions"]
        else:
            cleaned["permissions"] = permissions

    domain = cleaned.get("domain")
    if "domain" in cleaned and not (
        isinstance(domain, str) and len(domain) <= MAX_DOMAIN and _DOMAIN_RE.match(domain)
    ):
        del cleaned["domain"]

    if "prefersBorder" in cleaned and not isinstance(cleaned["prefersBorder"], bool):
        del cleaned["prefersBorder"]

    visibility = cleaned.get("visibility")
    if "visibility" in cleaned and not (
        isinstance(visibility, list) and all(isinstance(item, str) for item in visibility)
    ):
        # The members are not constrained -- `["model", "app"]` today, more later.
        del cleaned["visibility"]

    # A `resourceUri` that is not a string is a reference nobody can follow, and
    # `reference_of` reads it as no reference at all -- so without this it would ride
    # through untouched on the "nothing to rewrite" path and reach a host as a number.
    # `rewrite_tool_meta` puts the rewritten one back afterwards.
    if "resourceUri" in cleaned and not isinstance(cleaned["resourceUri"], str):
        del cleaned["resourceUri"]

    return cleaned


def sanitize_resource_meta(meta: object) -> dict[str, Any] | None:
    """A resource's `_meta` with only its `ui` member cleaned. `None` when nothing changed.

    `None` rather than a copy so the caller can leave the backend's own object alone, which
    matters where the caller is holding a cache entry.
    """
    if not isinstance(meta, dict) or "ui" not in meta:
        return None
    cleaned = sanitize_ui_block(meta.get("ui"))
    if cleaned == meta.get("ui"):
        return None
    updated = dict(meta)
    if cleaned is None:
        del updated["ui"]
    else:
        updated["ui"] = cleaned
    return updated


def rewrite_tool_meta(
    meta: object, server: str, *, tool: str = ""
) -> tuple[dict[str, Any] | None, str | None]:
    """A tool's `_meta`, with its UI reference moved into the gateway's address space.

    Returns `(new_meta, referenced_raw_uri)`. `new_meta` is `None` when nothing needed
    changing, so a caller holding a cached entry pays no copy and mutates nothing.
    `referenced_raw_uri` is the backend's own `ui://` -- the caller's to check against what
    that backend publishes -- and is `None` when there was no reference or it was refused.

    **Every dict on the path is rebuilt, never mutated.** `Catalogue.tools()` hands us the
    `_meta` of a *cached* entry, and that cache is the backend's own answer, which health
    and `/admin` must keep seeing as the backend gave it. Mutating in place would also
    rewrite the reference a second time on the next call and double-encode it.
    """
    if not isinstance(meta, dict):
        return None, None

    raw, flat_present = reference_of(meta)
    nested = meta.get("ui")
    cleaned_ui = sanitize_ui_block(nested)

    if raw is None:
        # No reference, but possibly a `ui` block worth cleaning anyway.
        if cleaned_ui == nested:
            return None, None
        updated = dict(meta)
        if cleaned_ui is None:
            updated.pop("ui", None)
        else:
            updated["ui"] = cleaned_ui
        return updated, None

    if not _acceptable_reference(raw):
        logger.warning(
            "backend %r: tool %r declares an unusable ui resourceUri; offering the tool without a panel",
            server,
            tool or "?",
        )
        updated = dict(meta)
        updated.pop(FLAT_RESOURCE_URI_KEY, None)
        if cleaned_ui is not None:
            without = {k: v for k, v in cleaned_ui.items() if k != "resourceUri"}
            if without:
                updated["ui"] = without
            else:
                updated.pop("ui", None)
        return updated, None

    public = naming.encode_resource_uri(server, raw)
    updated = dict(meta)
    updated["ui"] = {**(cleaned_ui or {}), "resourceUri": public}
    # Echoed only when the backend used it. Stripping it would break a host that reads only
    # the deprecated spelling from a backend that sends only the deprecated spelling -- a
    # live pairing -- and inventing it would have the gateway emit a field the spec is
    # deleting. Mirroring is correct now and one line to remove at GA.
    if flat_present:
        updated[FLAT_RESOURCE_URI_KEY] = public
    return updated, raw
