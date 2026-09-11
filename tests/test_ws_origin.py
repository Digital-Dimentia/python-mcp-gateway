"""The `Origin` check: a browser is now a first-class client, so a hole became real.

WebSocket has no same-origin policy. Any page on the internet can open a socket to
`127.0.0.1:8765`, and this daemon spawns the commands in its config and holds every
credential on the machine. Before there was a UI that was a note in a threat model; the
moment a browser is a supported client it is the thing to close.

The rule: a request that carries an `Origin` must name this server. One that carries none
proceeds, because only browsers send it and every other client here -- the bridge, Claude
Desktop, these tests -- sends none.
"""

from __future__ import annotations

import pytest
import websockets

from mcp_gateway import transport_ws
from tests.fixtures.ws_client import daemon


async def connect_with_origin(harness, origin: str | None, path: str = "/mcp"):
    headers = {"Origin": origin} if origin is not None else {}
    return await websockets.connect(harness.url(path), additional_headers=headers)


async def test_no_origin_is_allowed(tmp_path) -> None:
    """Every non-browser client sends none. Refusing that would break all of them."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        assert await client.call("ping") == {}
    finally:
        await harness.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
async def test_our_own_origin_is_allowed(tmp_path, host) -> None:
    """All three loopback spellings: a user who types `localhost` has done nothing wrong."""
    harness = await daemon(tmp_path)
    try:
        socket = await connect_with_origin(harness, f"http://{host}:{harness.port}")
        await socket.close()
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://127.0.0.1:1",          # right host, wrong port
        "null",                        # a sandboxed iframe
        "http://localhost.evil.example",
    ],
)
async def test_a_foreign_origin_is_refused(tmp_path, origin) -> None:
    harness = await daemon(tmp_path)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await connect_with_origin(harness, origin)
        assert caught.value.response.status_code == 403
    finally:
        await harness.close()


async def test_the_admin_path_is_checked_too(tmp_path) -> None:
    """The path that can write servers.yaml is not the one to leave open."""
    harness = await daemon(tmp_path)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await connect_with_origin(harness, "https://evil.example", "/admin")
        assert caught.value.response.status_code == 403
    finally:
        await harness.close()


async def test_the_check_happens_before_the_key(tmp_path) -> None:
    """A foreign page holding a stolen key still does not get a socket."""
    harness = await daemon(tmp_path, access_key="s3cret")
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await connect_with_origin(harness, "https://evil.example")
        assert caught.value.response.status_code == 403
    finally:
        await harness.close()


async def test_the_environment_can_widen_the_set(tmp_path, monkeypatch) -> None:
    """For a UI served from a dev server on another port, and for nothing else."""
    monkeypatch.setenv(transport_ws.ALLOWED_ORIGINS_ENV, "http://localhost:5173")
    harness = await daemon(tmp_path)
    try:
        socket = await connect_with_origin(harness, "http://localhost:5173")
        await socket.close()
    finally:
        await harness.close()


async def test_a_wildcard_turns_the_check_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(transport_ws.ALLOWED_ORIGINS_ENV, "*")
    harness = await daemon(tmp_path)
    try:
        socket = await connect_with_origin(harness, "https://evil.example")
        await socket.close()
    finally:
        await harness.close()


def test_configured_origins_reads_a_list_strictly() -> None:
    read = transport_ws.configured_origins
    assert read({}) == frozenset()
    assert read({transport_ws.ALLOWED_ORIGINS_ENV: ""}) == frozenset()
    assert read({transport_ws.ALLOWED_ORIGINS_ENV: "  ,  "}) == frozenset()
    assert read({transport_ws.ALLOWED_ORIGINS_ENV: "a, b ,c"}) == frozenset({"a", "b", "c"})


def test_own_origins_covers_both_schemes_and_every_loopback_spelling() -> None:
    origins = transport_ws.own_origins("127.0.0.1", 8765)
    assert "http://127.0.0.1:8765" in origins
    assert "http://localhost:8765" in origins
    assert "http://[::1]:8765" in origins
    # https so that `wss://` (python-mcp-gateway-4rw) needs no second change here.
    assert "https://127.0.0.1:8765" in origins
    # A non-loopback bind gets only itself: the loopback spellings are not its addresses.
    assert transport_ws.own_origins("10.0.0.5", 80) == frozenset(
        {"http://10.0.0.5:80", "https://10.0.0.5:80"}
    )


def test_origin_permitted_is_the_whole_rule_in_one_function() -> None:
    allowed = frozenset({"http://127.0.0.1:8765"})
    assert transport_ws.origin_permitted(None, allowed) is True
    assert transport_ws.origin_permitted("http://127.0.0.1:8765", allowed) is True
    assert transport_ws.origin_permitted("https://evil.example", allowed) is False
    assert transport_ws.origin_permitted("https://evil.example", frozenset({"*"})) is True
