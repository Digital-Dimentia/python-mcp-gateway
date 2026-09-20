"""`/panel/<token>`: the one endpoint that serves a document a backend wrote.

Two halves. The store is pure and is tested directly; the endpoint is driven over real HTTP
against a real daemon, for the reason `test_webui.py` gives -- the thing worth proving is
that `websockets`' `process_request` hook answers a plain GET at all, and a unit test of
`panels.response` would pass with the hook unwired.

The header assertions here are deliberately verbatim rather than "a CSP is present". The
sandbox directive in the *response* is what puts a panel in an opaque origin however it is
reached, and this origin is the one holding the access key in `localStorage`. A test that
accepted any policy would go green on the change that breaks that. See `panels.md`.
"""

from __future__ import annotations

import asyncio
from http.client import HTTPConnection

import pytest

from mcp_gateway import panels
from tests.fixtures.ws_client import daemon


async def get(port: int, path: str):
    """One plain HTTP GET. Threaded, because `http.client` is blocking."""

    def fetch():
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    return await asyncio.to_thread(fetch)


class Clock:
    """A hand-wound monotonic clock, so an expiry test takes no wall time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- the store ------------------------------------------------------------------------


def test_a_token_is_spent_by_the_first_load() -> None:
    store = panels.PanelStore()
    panel = store.mint(body="<p>hi</p>", owner=object())

    assert store.take(panel.token) is not None
    assert store.take(panel.token) is None
    assert len(store) == 0


def test_a_token_lapses_without_being_loaded() -> None:
    clock = Clock()
    store = panels.PanelStore(ttl=30.0, clock=clock)
    panel = store.mint(body="<p>hi</p>", owner=object())

    clock.now += 29.0
    assert store.take(panel.token) is not None

    panel = store.mint(body="<p>hi</p>", owner=object())
    clock.now += 31.0
    assert store.take(panel.token) is None
    assert len(store) == 0


def test_closing_the_connection_that_minted_revokes_what_it_minted() -> None:
    """The fifth leg of the argument in `panels.md`: a closed tab leaves nothing behind."""
    store = panels.PanelStore()
    tab, other_tab = object(), object()
    mine = [store.mint(body="<p>a</p>", owner=tab) for _ in range(3)]
    theirs = store.mint(body="<p>b</p>", owner=other_tab)

    assert store.revoke_all(tab) == 3
    assert all(store.take(panel.token) is None for panel in mine)
    assert store.take(theirs.token) is not None


def test_one_connection_cannot_grow_the_store_without_limit() -> None:
    store = panels.PanelStore()
    owner = object()
    minted = [store.mint(body="<p>x</p>", owner=owner) for _ in range(panels.MAX_LIVE_PER_OWNER + 5)]

    assert len(store) == panels.MAX_LIVE_PER_OWNER
    # The newest survive: the one a person is waiting on is the one they just opened.
    assert store.take(minted[-1].token) is not None


def test_an_unparseable_path_names_no_token() -> None:
    assert panels.token_of("/panel/abc") == "abc"
    assert panels.token_of("/panel/") is None
    assert panels.token_of("/panel") is None
    assert panels.token_of("/panel/a/b") is None
    assert panels.token_of("/ui/index.html") is None


# --- the policy -----------------------------------------------------------------------


def test_the_sandbox_directive_comes_first_and_grants_only_scripts() -> None:
    """If this ever stops being true, a panel token is stored XSS on the key's origin."""
    policy = panels.policy_for(None)

    assert policy.startswith("sandbox allow-scripts;")
    assert "allow-same-origin" not in policy
    assert "allow-popups" not in policy
    assert "allow-top-navigation" not in policy


def test_a_host_the_backend_did_not_name_is_refused_rather_than_defaulted_in() -> None:
    policy = panels.policy_for({"connectDomains": ["https://api.example.com"]})

    assert "default-src 'none'" in policy
    assert "connect-src https://api.example.com" in policy
    # Asked for nothing, so granted nothing -- not left absent to fall back on `default-src`.
    assert "frame-src 'none'" in policy


def test_the_admin_page_may_frame_a_panel_and_the_internet_may_not() -> None:
    assert "frame-ancestors 'self'" in panels.policy_for({})


def test_a_csp_block_of_the_wrong_shape_is_read_as_asking_for_nothing() -> None:
    """`_meta` is a backend's, so every branch here has to survive a backend being odd."""
    for block in (None, "nonsense", [], {"connectDomains": "https://x.example"}, {"x": 1}):
        policy = panels.policy_for(block)
        assert policy.startswith("sandbox allow-scripts;")
        assert "connect-src 'none'" in policy


# --- the endpoint ---------------------------------------------------------------------


async def test_a_minted_panel_is_served_once_under_its_own_policy(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        panel = harness.gateway.panels.mint(
            body="<!doctype html><p>panel</p>",
            csp={"connectDomains": ["https://api.example.com"]},
            owner=object(),
        )

        status, headers, body = await get(harness.port, panel.url)
        assert status == 200
        assert body == b"<!doctype html><p>panel</p>"
        assert headers["Content-Type"].startswith("text/html")
        assert headers["Content-Security-Policy"] == panel.policy
        assert headers["Content-Security-Policy"].startswith("sandbox allow-scripts;")
        assert "connect-src https://api.example.com" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"

        # Single-use, and the second answer says nothing about the first having existed.
        spent_status, _, _ = await get(harness.port, panel.url)
        assert spent_status == 404
    finally:
        await harness.close()


@pytest.mark.parametrize("path", ["/panel/nope", "/panel/", "/panel/a/b"])
async def test_every_way_of_missing_answers_the_same_404(tmp_path, path) -> None:
    """Guessing must not be able to learn whether a token was ever real."""
    harness = await daemon(tmp_path)
    try:
        status, _, body = await get(harness.port, path)
        assert status == 404
        assert b"single-use" in body
    finally:
        await harness.close()


async def test_a_panel_needs_no_access_key_and_the_sockets_still_do(tmp_path) -> None:
    """The endpoint's own argument, end to end: the token in the path is the credential."""
    harness = await daemon(tmp_path, access_key="s3cret")
    try:
        panel = harness.gateway.panels.mint(body="<p>panel</p>", owner=object())

        status, _, body = await get(harness.port, panel.url)
        assert status == 200
        assert body == b"<p>panel</p>"

        # And nothing else on this port went soft.
        refused, _, _ = await get(harness.port, "/admin")
        assert refused == 401
    finally:
        await harness.close()


async def test_the_admin_page_is_allowed_to_frame_one(tmp_path) -> None:
    """`default-src 'none'` blocks every iframe, so this entry is load-bearing."""
    harness = await daemon(tmp_path)
    try:
        _, headers, _ = await get(harness.port, "/ui/")
        assert "frame-src 'self'" in headers["Content-Security-Policy"]
    finally:
        await harness.close()
