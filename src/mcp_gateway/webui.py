"""Serve the admin UI's static assets, on the same port as the two sockets.

The UI is HTML, CSS and ES modules with no build step and no dependencies, so "serving it"
is reading one allowlisted file off disk and answering a GET. That happens inside the WebSocket
server's `process_request` hook, which runs before the opening handshake and may return an
ordinary HTTP response instead of upgrading -- the documented way to put a health check or
a static page on a `websockets` port.

## Why the same port

Same origin. The page needs to open `ws://.../mcp` and `ws://.../admin`, and a page served
from anywhere else is cross-origin to them -- which matters now that `transport_ws` checks
`Origin` on both. A second port, or a separate static server, would mean either widening
that check or explaining to every user why their browser can see the page but not the
gateway.

## No access key on the assets

The two sockets require the key. These files do not, for a reason that is not laziness: a
browser cannot put an `Authorization` header on a navigation, so the only way to gate a page
is a query parameter -- which is how a key ends up in history, in a bookmark, and in the
referrer of every link. The assets are inert. They contain no configuration, no backend
list, and no credential; everything a person can actually *see* arrives over a socket that
did check the key. Gating the shell would trade a real secret-leak channel for no protection
at all.

## An allowlist, not a path join

`asset_for` resolves a request path against a fixed set of names by equality. Nothing
derived from a request is ever joined to a directory, so there is no traversal to get wrong
-- no `..`, no encoded separator, no symlink, no case-insensitive-filesystem surprise. The
cost is that adding a file to `src/mcp_gateway_ui/` means adding its name here, and
`tests/test_webui.py` asserts the two agree, so the failure mode is a red test rather than
a 404 nobody can explain.

Some of those names now carry a `/`: the screens live in `screens/`, and `screens/basics/`
holds the four columns that exist only to serve that screen. That changes nothing about the
above and it is worth saying why, because "no path join" is the sentence this module used to
be able to write and can no longer. `read_asset` walks the segments of a name to reach the
file -- but it walks the segments of a name that is *already a key of `ASSETS`*, not of
anything a client sent. A request for `screens/../webui.py` does not match a key, so it is a
404 before any path exists; equality on the whole name is still the only test there is.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from websockets.datastructures import Headers
from websockets.http11 import Response

logger = logging.getLogger(__name__)

#: Where the UI is mounted. `/ui` and `/ui/` both answer with `INDEX`.
UI_PATH = "/ui"

#: The package the assets live in, as an importable name so `importlib.resources` finds
#: them inside a wheel exactly as it does in a checkout. It is a top-level package beside
#: this one, not a subpackage of it: the UI is not part of the daemon. See
#: `mcp_gateway_ui/__init__.py`.
ASSET_PACKAGE = "mcp_gateway_ui"

INDEX = "index.html"

#: Every file this module will serve, and the content type it serves it as. **This tuple is
#: the security boundary** -- see the module docstring.
ASSETS: dict[str, str] = {
    INDEX: "text/html; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "theme.js": "text/javascript; charset=utf-8",
    "rpc.js": "text/javascript; charset=utf-8",
    # Served here and used only in the desktop shell, where the page is loaded off disk
    # rather than fetched. It is in this list because there is one copy of the admin UI and
    # two hosts for it: `index.html` loads the file unconditionally, and in a browser it
    # finds no Tauri and does nothing. Shipping it costs one small GET; the alternative is a
    # second copy of the assets that drifts. See `desktop/README.md`.
    "tauri-transport.js": "text/javascript; charset=utf-8",
    "schema_form.js": "text/javascript; charset=utf-8",
    # One screen, one directory. The header's `<option>` list is the register of screens and
    # this is the register of their code; `screens/about/screen.js` documents the contract
    # both ends keep, and each screen's directory holds the pieces that exist only to serve
    # it -- About's slide deck, Basics' four columns.
    "screens/about/screen.js": "text/javascript; charset=utf-8",
    # The deck on the About screen: `tour.js` is the mechanism -- slide n of m, and the way
    # to n+1 -- and `slides.js` is what it says about this project, which is the half that
    # goes stale and so is kept in one file with nothing else in it.
    "screens/about/tour.js": "text/javascript; charset=utf-8",
    "screens/about/slides.js": "text/javascript; charset=utf-8",
    # Which gateway the window is driving: the child the desktop host starts, or one on
    # another machine reached through an SSH tunnel it opens. Served here like every other
    # screen -- there is one copy of the admin UI and two hosts for it -- and inert in a
    # browser, which has no host to run `ssh`: `app.js` removes its option when there is no
    # shell behind the page. Same rule as `tauri-transport.js`.
    "screens/connection/screen.js": "text/javascript; charset=utf-8",
    "screens/basics/screen.js": "text/javascript; charset=utf-8",
    # The injectable values column: the vocabularies, the cascade, and what is picked.
    "screens/basics/variables.js": "text/javascript; charset=utf-8",
    # The form a primitive opens into, and the port the column above writes through.
    "screens/basics/detail.js": "text/javascript; charset=utf-8",
    # The left column and the hover tooltip that belongs to its rows.
    "screens/basics/primitives.js": "text/javascript; charset=utf-8",
    # The middle column: the result cards, and what each one was about.
    "screens/basics/results.js": "text/javascript; charset=utf-8",
    # The gateway's namespacing, undone -- `naming.py`'s mirror, in one file now that three
    # modules take a listing apart.
    "naming.js": "text/javascript; charset=utf-8",
    # Display formatting for the fields `admin.status` answers with, shared by the footer and
    # by a screen, so an uptime cannot read two ways in one window.
    "format.js": "text/javascript; charset=utf-8",
    "render.js": "text/javascript; charset=utf-8",
    "clipboard.js": "text/javascript; charset=utf-8",
    "markdown.js": "text/javascript; charset=utf-8",
    # The stock mark, worn by the header and the favicon before any socket has answered --
    # and kept by both when the catalogue configures no `branding.icon`. An asset rather
    # than a `data:` URI so there is one file to swap and one thing to look at; a
    # *configured* icon never comes through here, because the file it names is outside this
    # package and arrives inline over `/admin` instead. See `branding.md`.
    "logo.svg": "image/svg+xml",
}


def is_ui_path(path: str) -> bool:
    """Whether `path` is the UI mount or something under it. `/ui` itself redirects."""
    return path == UI_PATH or path.startswith(UI_PATH + "/")


def asset_for(path: str) -> str | None:
    """The asset a request path names, or `None` when it names nothing we serve.

    `/ui/` means the index (and so does a bare `/ui`, though `response` redirects that
    before asking). Anything deeper must be exactly one allowlisted filename: a nested path
    is not a miss to be searched for, it is a request for something that does not exist.
    """
    if path in (UI_PATH, UI_PATH + "/"):
        return INDEX
    if not path.startswith(UI_PATH + "/"):
        return None
    name = path[len(UI_PATH) + 1 :]
    return name if name in ASSETS else None


def read_asset(name: str) -> bytes:
    """One asset's bytes. Read on every request, deliberately.

    Caching would save a few microseconds and cost the ability to edit `app.js` and press
    reload, which is the entire development loop for a file that no test can cover.
    """
    if name not in ASSETS:  # pragma: no cover - callers resolve through `asset_for`
        raise KeyError(name)
    # Segment by segment, because a name in this dict may be `screens/basics/detail.js`. A
    # `Traversable` is not a `Path`: `/` on one takes a single component by contract, and
    # whether a given implementation happens to accept `a/b` is not something to rely on
    # between a checkout (a real directory) and a wheel (which may be read through zipimport).
    # The loop is the portable spelling, and the guard above is what makes it safe: these
    # segments come from a key of `ASSETS`, never from a request.
    target = resources.files(ASSET_PACKAGE)
    for segment in name.split("/"):
        target = target / segment
    return target.read_bytes()


def response(target: str) -> Response:
    """Answer a `/ui` request: a redirect, the asset, or a 404 naming what is there.

    Takes the whole request target rather than just the path, because the redirect below
    has to carry the query string -- and the query string is where the access key is.

    `no-store` because this is a local development tool whose files change under a running
    daemon, and a cached `app.js` after an edit looks exactly like a bug in the edit.
    """
    split = urlsplit(target)
    path = split.path or "/"

    # `/ui` -> `/ui/`, and it is not cosmetic. A page served at `/ui` has `/` as its base
    # URL, so the browser resolves `style.css` to `/style.css` and gets a 404 -- an
    # unstyled page with no JavaScript, from a URL that returned 200. `/ui` is exactly what
    # the startup banner prints, so this is the first thing anybody types.
    #
    # 308 rather than 301: it is permanent *and* it preserves the method, and a permanent
    # redirect a browser caches for the wrong path is a bad thing to be casual about on a
    # port people rebind.
    if path == UI_PATH:
        location = UI_PATH + "/" + (f"?{split.query}" if split.query else "")
        redirect = _response(
            HTTPStatus.PERMANENT_REDIRECT,
            f"The UI is at {UI_PATH}/.\n".encode(),
            "text/plain; charset=utf-8",
        )
        redirect.headers["Location"] = location
        return redirect

    name = asset_for(path)
    if name is None:
        logger.debug("no such UI asset: %s", path)
        return _response(
            HTTPStatus.NOT_FOUND,
            f"No such UI asset. Served from {UI_PATH}/: "
            f"{', '.join(sorted(ASSETS))}.\n".encode(),
            "text/plain; charset=utf-8",
        )
    try:
        body = read_asset(name)
    except OSError as exc:
        # A packaging fault, not a client one: the allowlist named a file the install does
        # not have. Say so rather than 404ing, which would send someone hunting the URL.
        logger.error("UI asset %r could not be read: %s", name, exc)
        return _response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"UI asset {name!r} is missing from this installation.\n".encode(),
            "text/plain; charset=utf-8",
        )
    return _response(HTTPStatus.OK, body, ASSETS[name])


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers(
        {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            # The page loads nothing it does not ship: no CDN, no font host, no analytics.
            # Saying so in a header means a compromised asset cannot quietly start.
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; media-src 'self' data:; connect-src 'self' ws: wss:; "
                "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        }
    )
    return Response(int(status), status.phrase, headers, body)


def url(host: str, port: int, key: str | None = None) -> str:
    """The URL to print at startup. Carries the key only when there is one.

    A key in a URL is the carrier `transport_ws` already documents as the wrong one on
    principle and accepts anyway, because it is the only one a browser can present on a
    navigation. Printing it here is the same trade, made once, in a banner that goes to
    stderr rather than to a log file.
    """
    display = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    suffix = f"?key={key}" if key else ""
    # The trailing slash is canonical: `/ui` answers with a 308 to here, so printing the
    # redirect target costs one round trip less and does not put the key on the wire twice.
    return f"http://{display}:{port}{UI_PATH}/{suffix}"


def describe() -> dict[str, Any]:
    """What `/admin` reports about the UI. Names only; no bytes, no paths on disk."""
    return {"path": UI_PATH, "assets": sorted(ASSETS)}
