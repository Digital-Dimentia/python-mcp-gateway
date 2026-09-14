"""What this gateway calls itself, and the picture it wears.

A `branding:` block in `servers.yaml` carries three things: the `title` a person reads, the
`name` an MCP client receives in `serverInfo`, and a path to an `icon`. Everything here is
display; nothing in it changes what any backend is or how it is spawned.

## Two names, not one

`title` and `name` are separate because their readers are. `title` is prose for a human --
"Acme Internal Tools", spaces and capitals and all -- and it lands in the tab, the header,
the desktop window and the CLI output. `name` is what goes over the wire in `serverInfo`,
where clients use it in config files, log lines and lock keys, so it keeps the shape of an
identifier. Folding them into one field would mean either a window titled `acme-tools` or a
client config key called `Acme Internal Tools`; both are somebody's bug.

## The icon is read, not served

There are two icons and only one of them is configuration. The **stock mark** is
`ui/logo.svg`, a file this package ships: it is the favicon and the header image in
`index.html`, it is served by `webui.py` like the stylesheet, and it is what the page wears
when `icon` is unset. Swapping that file is the shortest rebrand there is, and it is the one
a checkout reaches for.

A **configured** `icon` names a file outside this package; this module reads it and hands out
a `data:` URI. That looks wasteful next to a
URL until you count the hosts. The desktop shell loads the page off disk under a
CSP of `img-src 'self' data:` with `connect-src ipc:` -- a URL pointing at the daemon is
simply not fetchable there, and widening that CSP to let a picture through would open the
window to the network for the sake of a logo. A `data:` URI arrives inside the `/admin`
payload the window is already receiving, renders identically in both hosts, and adds no
unauthenticated endpoint to `webui.py`, whose allowlist stays a list of files this package
ships. The cost is that the bytes ride along on `admin.status`, which is why they are capped.

## Loud about a bad icon

An unreadable path, an extension outside `MEDIA_TYPES`, or a file over `MAX_ICON_BYTES` is
a `ConfigError` that names the file -- the same stance `config.py` takes on an unknown key.
A branding block that half-applies is worse than one that refuses: nobody looks at a header
and concludes their logo failed to load, they conclude they edited the wrong file.

The bytes are re-read on every request rather than cached at parse time, matching
`webui.read_asset`: replacing the logo and pressing reload is the entire edit loop, and a
cache would make that loop require a restart. Parse time validates; serve time reads.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: What an unbranded gateway calls itself. `DEFAULT.name` is also the `serverInfo` name every
#: released version has reported, so leaving the block out changes nothing on the wire.
DEFAULT_TITLE = "MCP Gateway"
DEFAULT_NAME = "mcp-gateway"

#: Icon formats, by suffix. Vector first because it is the right answer: one file that is
#: sharp in a 16px favicon and a 512px app icon, and small enough that the cap below never
#: comes up. The raster formats are here because a company's logo usually arrives as a PNG.
MEDIA_TYPES: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

#: The ceiling on an icon, before base64 inflates it by a third. Generous for its purpose --
#: an SVG logo is a couple of kilobytes and a 512px PNG rarely clears 60 -- and low enough
#: that nobody accidentally puts a megabyte of photograph on every `admin.status`.
MAX_ICON_BYTES = 128 * 1024

_KEYS = frozenset({"title", "name", "icon"})

#: `serverInfo.name` is read by machines: it ends up in client config files and log lines.
#: Not the strict server-name grammar from `naming.py` -- that one also forbids `__`, which
#: matters only for the namespace separator and has nothing to do with this field.
_MAX_NAME = 64
_MAX_TITLE = 96


class BrandingError(ValueError):
    """A branding block that cannot be honoured. Wrapped into `ConfigError` by `config`."""


@dataclass(frozen=True)
class Branding:
    """A gateway's display identity. Frozen, and holds a path rather than bytes."""

    title: str = DEFAULT_TITLE
    name: str = DEFAULT_NAME
    #: Absolute, already resolved against the catalogue's directory, and known to exist and
    #: to be within the cap at the moment the config was parsed.
    icon_path: Path | None = None

    @property
    def customised(self) -> bool:
        """Whether anything here differs from an unbranded gateway. Used by the CLI, which
        prints a branding line only when there is one to print."""
        return (
            self.title != DEFAULT_TITLE
            or self.name != DEFAULT_NAME
            or self.icon_path is not None
        )

    @property
    def media_type(self) -> str | None:
        if self.icon_path is None:
            return None
        return MEDIA_TYPES[self.icon_path.suffix.lower()]

    def icon_data_uri(self) -> str | None:
        """The icon as `data:<type>;base64,<bytes>`, or `None` when none is configured.

        Reads the file. A file that has become unreadable since startup logs a warning and
        yields `None` rather than raising: this is called while answering `admin.status`,
        and `None` is a state the page already handles -- it wears the stock `ui/logo.svg`.
        A logo that disappears must cost the header its picture, never take the whole
        footer down with a status call that failed.
        """
        if self.icon_path is None:
            return None
        try:
            raw = self.icon_path.read_bytes()
        except OSError as exc:
            logger.warning("branding icon %s cannot be read: %s", self.icon_path, exc)
            return None
        if len(raw) > MAX_ICON_BYTES:
            # It was within the cap when the config was parsed, so someone has replaced it
            # under the running daemon with something larger.
            logger.warning(
                "branding icon %s is now %d bytes, over the %d-byte cap; not serving it",
                self.icon_path,
                len(raw),
                MAX_ICON_BYTES,
            )
            return None
        return f"data:{self.media_type};base64,{base64.b64encode(raw).decode('ascii')}"

    def describe(self) -> dict[str, Any]:
        """What `/admin` reports: the two names, and the icon inline. See the module doc."""
        return {"title": self.title, "name": self.name, "icon": self.icon_data_uri()}


DEFAULT = Branding()


def _as_display_str(value: Any, *, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise BrandingError(f"{where}: expected a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise BrandingError(f"{where}: must not be empty")
    if len(text) > limit:
        raise BrandingError(f"{where}: {len(text)} characters; the limit is {limit}")
    # Control characters in a title reach a window title bar, a log line and a terminal
    # banner -- three places where an escape sequence is not merely ugly.
    if any(ch < " " or ch == "\x7f" for ch in text):
        raise BrandingError(f"{where}: must not contain control characters")
    return text


def parse(raw: Any, *, base_dir: Path | None, where: str) -> Branding:
    """Validate a `branding:` block. `raw` may be `None`, meaning an unbranded gateway.

    `base_dir` is the catalogue's directory: a relative `icon` resolves against the file
    that named it, not against whatever directory the daemon happens to have been started
    in. A daemon is started by launchd, by a container entrypoint and by hand, and those are
    three different working directories for one unchanged config.
    """
    if raw is None:
        return DEFAULT
    if not isinstance(raw, dict):
        raise BrandingError(f"{where}: must be a mapping, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise BrandingError(f"{where}: unknown key(s) {unknown}. Allowed here: {sorted(_KEYS)}.")

    title = (
        _as_display_str(raw["title"], where=f"{where}.title", limit=_MAX_TITLE)
        if "title" in raw
        else DEFAULT_TITLE
    )
    name = (
        _as_display_str(raw["name"], where=f"{where}.name", limit=_MAX_NAME)
        if "name" in raw
        else DEFAULT_NAME
    )
    if "name" in raw and any(ch.isspace() for ch in name):
        raise BrandingError(
            f"{where}.name: {name!r} contains whitespace. This is the identifier MCP "
            f"clients receive in serverInfo; put the prose in 'title'."
        )
    icon = _parse_icon(raw.get("icon"), base_dir=base_dir, where=f"{where}.icon")
    return Branding(title=title, name=name, icon_path=icon)


def _parse_icon(value: Any, *, base_dir: Path | None, where: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BrandingError(f"{where}: expected a path to an image file")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    path = path.resolve()

    suffix = path.suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise BrandingError(
            f"{where}: {path} has no icon extension this gateway serves "
            f"(allowed: {', '.join(sorted(MEDIA_TYPES))})"
        )
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise BrandingError(f"{where}: no such file: {path}") from None
    except OSError as exc:
        raise BrandingError(f"{where}: cannot read {path}: {exc}") from exc
    if size > MAX_ICON_BYTES:
        raise BrandingError(
            f"{where}: {path} is {size} bytes; the limit is {MAX_ICON_BYTES}. "
            f"This file is base64'd into every admin.status payload -- use an SVG, or a "
            f"PNG sized for an icon rather than a page."
        )
    return path
