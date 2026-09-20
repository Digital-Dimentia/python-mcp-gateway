"""Serve one backend's HTML panel at an unguessable URL, under its own policy.

SEP-1865's HTML tier -- `text/html;profile=mcp-app` -- is a document a backend wrote, run in
the host's browser. The declarative tier next door never executes anything a backend sent;
this one exists to, so everything here is about the walls around it.

## The lock is the response header, not the iframe attribute

A panel could be handed to the page as a string and framed with `srcdoc`, and that is the
shape that looks simplest. It does not work and could not be made to: a `srcdoc`, `blob:` or
`data:` document **inherits the embedder's policy container**, so under `/ui`'s
`script-src 'self'` a panel's inline script would not run -- and loosening the *admin page's*
CSP to let a backend's script run is not a trade anybody should make.

So a panel needs a real URL, and a real URL on this origin is the dangerous part:
`/ui`'s `localStorage` holds the access key, so a document served from here that reached an
origin-bearing context would be reading it. `sandbox` on the `<iframe>` element does not
settle that, because the element is one caller's choice -- anything that opens the URL
directly, or frames it without the attribute, gets the origin back.

    Content-Security-Policy: sandbox allow-scripts

in the **response** does settle it. The document lands in an opaque origin however it is
reached: typed into the address bar, framed by another page, opened in a new tab. The
`<iframe>` keeps its `sandbox` attribute anyway, belt and braces, but this header is the
security boundary and the attribute is not. **If this header is ever dropped, a panel token
becomes stored XSS on the origin holding the access key.**

## Why this endpoint may be unauthenticated, which is not why `/ui` may be

`webui.py` serves the admin UI to anyone who asks, and the argument is that those files are
*inert*: no configuration, no backend list, no credential, nothing a person can see without
a socket that did check the key. That argument does not transfer here. A panel is a
backend's own document and it is not inert.

This endpoint has its own, and it has to stand on its own four legs:

* **Unguessable.** 256 bits from `secrets.token_urlsafe`, in the path.
* **Short-lived.** Minted for one page to load immediately; expired seconds later.
* **Single-use.** The first GET consumes it. A URL in somebody's history is already dead.
* **Sandboxed.** The header above, so what it serves has no origin to abuse.

And one more that is not a property of the URL: it is minted only over `/admin`, which did
check the key, and dies with that connection. A closed tab leaves nothing behind.

Both arguments end in "this is safe to serve unauthenticated" and they share no premise. The
second must not be allowed to hide behind the first -- which is why it is written out here
rather than cross-referenced.

## The policy a panel runs under

Built from the `csp` block [`ui_apps.py`](ui_apps.md) already sanitized -- a backend asks for
the hosts its panel needs, and the entries that could have become a second directive are
already gone. What this module adds is the frame the request is granted *inside*:
`default-src 'none'`, so an unasked-for host is refused rather than defaulted in.

⚠️ **A known conformance gap, documented rather than discovered.** With `allow-same-origin`
omitted the origin is opaque, so `'self'` matches nothing at all -- and SEP-1865's default
`script-src 'self' 'unsafe-inline'` therefore collapses here to `'unsafe-inline'` alone. A
panel that loads a sibling `.js` works in a host that grants it an origin and silently fails
in this one. `'self'` is kept in the policy anyway, because it says what the directive means
and costs nothing; what it does not do is work. A panel for this gateway inlines its script.
"""

from __future__ import annotations

import logging
import secrets as stdlib_secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from websockets.datastructures import Headers
from websockets.http11 import Response

logger = logging.getLogger(__name__)

#: Where a panel is served. One segment of token follows: `/panel/<token>`.
PANEL_PATH = "/panel"

#: Bytes of entropy behind each token. `token_urlsafe` gives about 1.3 characters per byte,
#: so this is a 43-character path segment -- long enough that guessing is not a threat model
#: and short enough to read in a log line.
TOKEN_BYTES = 32

#: Seconds between minting a URL and the load that consumes it. This is not a session
#: lifetime: the page sets an `<iframe src>` the moment it has the URL, so the whole window
#: is one browser fetch. Generous enough for a machine under load, short enough that a URL
#: in a history file is already dead.
TOKEN_TTL_SECONDS = 30.0

#: A ceiling on live tokens per minting connection, so a page in a loop cannot make the
#: store the process's memory problem. Far above any real page: a panel per result card.
MAX_LIVE_PER_OWNER = 64

#: What a panel is framed by. `'self'` is this gateway's origin, which is where `/ui` is
#: served from -- so the admin page may frame a panel and a page on the internet may not.
#: Matched against the *embedder*, which has a real origin, so unlike `script-src 'self'`
#: above this one means what it says.
_FRAME_ANCESTORS = "'self'"

#: An empty source list, spelled out. See `_DIRECTIVES`.
NONE = "'none'"

#: The directive each `csp` list feeds, and what is granted with no list at all. A directive
#: with no sources is `'none'` rather than absent: absent falls back to `default-src`, and
#: the whole point of the frame below is that the fallback is a refusal.
_DIRECTIVES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (directive, the `csp` member feeding it, sources granted regardless)
    ("script-src", "resourceDomains", ("'self'", "'unsafe-inline'")),
    ("style-src", "resourceDomains", ("'self'", "'unsafe-inline'")),
    ("img-src", "resourceDomains", ("'self'", "data:", "blob:")),
    ("font-src", "resourceDomains", ("'self'", "data:")),
    ("media-src", "resourceDomains", ("'self'", "data:")),
    ("connect-src", "connectDomains", ()),
    ("frame-src", "frameDomains", ()),
    ("base-uri", "baseUriDomains", ()),
)


@dataclass(frozen=True)
class Panel:
    """One minted panel: the document, the policy it runs under, and when it lapses."""

    token: str
    body: bytes
    policy: str
    owner: Any
    expires_at: float

    @property
    def url(self) -> str:
        """The capability URL, relative so it carries whatever origin the page has."""
        return f"{PANEL_PATH}/{self.token}"


def is_panel_path(path: str) -> bool:
    """Whether a request path names this endpoint. `/panel` itself does not."""
    return path.startswith(PANEL_PATH + "/")


def token_of(path: str) -> str | None:
    """The token a request path carries, or `None` when it carries anything else.

    Exactly one segment, like `webui.asset_for`'s equality test and for the same reason:
    nothing derived from a request is ever joined to anything, so there is no traversal to
    get wrong. A deeper path is not a token to search for, it is a request for something
    that does not exist.
    """
    if not is_panel_path(path):
        return None
    token = path[len(PANEL_PATH) + 1 :]
    return token if token and "/" not in token else None


def policy_for(csp: object) -> str:
    """The `Content-Security-Policy` header value a panel is served under.

    `csp` is the sanitized `_meta.ui.csp` block, or anything at all -- a backend's `_meta`
    is a backend's, so this takes what it is given and reads only what it recognises.

    `sandbox allow-scripts` comes first because it is the one directive here that is not
    negotiable: see the module docstring.
    """
    block = csp if isinstance(csp, dict) else {}
    parts = [
        # Scripts, and nothing else. No `allow-same-origin` (that is the whole lock), no
        # `allow-forms`, `allow-popups`, `allow-modals` or `allow-top-navigation`: a panel
        # talks to its host over `postMessage`, and a panel that could navigate the top
        # frame could take the admin page off the screen.
        "sandbox allow-scripts",
        "default-src 'none'",
    ]
    for directive, member, granted in _DIRECTIVES:
        sources = [*granted]
        asked = block.get(member)
        if isinstance(asked, list):
            sources.extend(entry for entry in asked if isinstance(entry, str))
        parts.append(f"{directive} {' '.join(sources) if sources else NONE}")
    parts.append(f"frame-ancestors {_FRAME_ANCESTORS}")
    parts.append("form-action 'none'")
    return "; ".join(parts)


class PanelStore:
    """The live panel URLs, and who minted each.

    One per process, on the `Gateway`, for the same reason the clipboard is: the thing that
    mints is an `/admin` connection and the thing that serves is a `process_request` hook
    inside the WebSocket server, and neither can reach the other except through here.

    Not a cache. An entry exists to be consumed once, and every path out of this class --
    a load, an expiry, a closed connection -- removes it.
    """

    def __init__(self, *, ttl: float = TOKEN_TTL_SECONDS, clock: Any = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._panels: dict[str, Panel] = {}

    def __len__(self) -> int:
        return len(self._panels)

    def mint(self, *, body: str | bytes, csp: object = None, owner: Any) -> Panel:
        """A capability URL for one document, owned by the connection that asked.

        `owner` is whatever the caller uses to identify itself -- the `/admin` connection
        object. It is only ever compared by identity and never sent anywhere.
        """
        self._sweep()
        live = sum(1 for panel in self._panels.values() if panel.owner is owner)
        if live >= MAX_LIVE_PER_OWNER:
            # The oldest of this owner's, rather than a refusal: a page that opened
            # sixty-five panels is a person clicking, not an attack, and the one they are
            # waiting on is the newest.
            oldest = min(
                (p for p in self._panels.values() if p.owner is owner),
                key=lambda p: p.expires_at,
            )
            del self._panels[oldest.token]
        panel = Panel(
            token=stdlib_secrets.token_urlsafe(TOKEN_BYTES),
            body=body.encode("utf-8") if isinstance(body, str) else body,
            policy=policy_for(csp),
            owner=owner,
            expires_at=self._clock() + self._ttl,
        )
        self._panels[panel.token] = panel
        return panel

    def take(self, token: str) -> Panel | None:
        """The panel a token names, consuming it. `None` when it is unknown or lapsed.

        Single-use is checked here rather than in the handler so there is no window between
        the two: the same dictionary operation both reads and spends.
        """
        self._sweep()
        panel = self._panels.pop(token, None)
        if panel is None:
            return None
        if panel.expires_at <= self._clock():  # pragma: no cover - `_sweep` got there first
            return None
        return panel

    def revoke_all(self, owner: Any) -> int:
        """Drop every panel a connection minted, and say how many. Called when it closes."""
        doomed = [token for token, panel in self._panels.items() if panel.owner is owner]
        for token in doomed:
            del self._panels[token]
        return len(doomed)

    def _sweep(self) -> None:
        now = self._clock()
        for token in [t for t, panel in self._panels.items() if panel.expires_at <= now]:
            del self._panels[token]


def response(store: PanelStore, path: str) -> Response:
    """Answer a `/panel` request: the document under its policy, or a 404.

    A 404 for every failure -- unknown token, spent token, lapsed token, malformed path --
    and deliberately the same one. Distinguishing them would tell an unauthenticated caller
    whether a token it guessed ever existed, which is the only thing guessing could learn.
    """
    token = token_of(path)
    panel = store.take(token) if token else None
    if panel is None:
        logger.debug("no live panel at %s", path)
        return _response(
            HTTPStatus.NOT_FOUND,
            b"No such panel. A panel URL is single-use and short-lived.\n",
            "text/plain; charset=utf-8",
            "sandbox; default-src 'none'",
        )
    return _response(HTTPStatus.OK, panel.body, "text/html; charset=utf-8", panel.policy)


def _response(status: HTTPStatus, body: bytes, content_type: str, policy: str) -> Response:
    headers = Headers(
        {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            # A capability URL that a cache could replay is not single-use. `no-store` is
            # the whole of it: this response must exist in exactly one place, the frame
            # that asked for it.
            "Cache-Control": "no-store",
            "Content-Security-Policy": policy,
            "X-Content-Type-Options": "nosniff",
            # Belt and braces with `frame-ancestors` above, for the same reason the
            # `<iframe>` keeps its `sandbox` attribute: two mechanisms, and the older one
            # is understood by things that do not read the newer.
            "X-Frame-Options": "SAMEORIGIN",
            # Nothing a panel does should name the URL it was served from -- that URL is a
            # capability, even a spent one.
            "Referrer-Policy": "no-referrer",
        }
    )
    return Response(int(status), status.phrase, headers, body)
