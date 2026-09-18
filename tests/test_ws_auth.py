"""Admission control: the bind guard, the access key, and where the key comes from.

The threat this protects against is concrete. The daemon spawns the commands named in its
config and holds every credential on the machine in one file, so a socket anyone can open
is both arbitrary code execution and a credential oracle.
"""

from __future__ import annotations

import pytest
import websockets

from mcp_gateway import transport_ws as ws
from mcp_gateway.secrets import SecretStore
from tests.fixtures.ws_client import daemon

KEY = "s3cret-access-key"


# --- the bind guard --------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binds_without_a_key(host: str) -> None:
    ws.refuse_unauthenticated_bind(host, None, False)


@pytest.mark.parametrize("host", ["0.0.0.0", "", None, "10.0.0.5", "example.com"])
def test_anything_not_provably_loopback_is_refused_without_a_key(host) -> None:
    """`is_loopback` fails closed: an unresolvable name counts as exposed."""
    with pytest.raises(ws.UnauthenticatedBindError):
        ws.refuse_unauthenticated_bind(host, None, False)


def test_a_key_permits_a_public_bind() -> None:
    ws.refuse_unauthenticated_bind("0.0.0.0", KEY, False)


def test_the_opt_out_permits_a_public_bind() -> None:
    ws.refuse_unauthenticated_bind("0.0.0.0", None, True)


def test_the_refusal_says_how_to_fix_it() -> None:
    with pytest.raises(ws.UnauthenticatedBindError) as caught:
        ws.refuse_unauthenticated_bind("0.0.0.0", None, False)
    message = str(caught.value)
    assert ws.ACCESS_KEY_ENV in message
    assert ws.ACCESS_KEY_SECRET_NAME in message
    assert ws.ALLOW_UNAUTHENTICATED_ENV in message


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_the_opt_out_is_read_permissively_for_affirmatives(value: str) -> None:
    assert ws.unauthenticated_bind_allowed({ws.ALLOW_UNAUTHENTICATED_ENV: value})


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_the_opt_out_is_read_strictly_for_everything_else(value: str) -> None:
    """A permissive reading would turn `=0` -- which says the opposite -- into consent."""
    assert not ws.unauthenticated_bind_allowed({ws.ALLOW_UNAUTHENTICATED_ENV: value})


# --- where the key comes from -----------------------------------------------------------


def test_an_empty_environment_value_reads_as_unset() -> None:
    """`MCP_GATEWAY_WS_KEY=` is how someone spells "I turned this off"."""
    assert ws.access_key_from_env({ws.ACCESS_KEY_ENV: ""}) is None


def test_the_key_falls_back_to_the_credential_store() -> None:
    """The daemon's own credential belongs in the file that is already gitignored."""
    store = SecretStore(_values={ws.ACCESS_KEY_SECRET_NAME: KEY})
    assert ws.resolve_access_key(store, environ={}) == KEY


def test_the_environment_wins_over_the_credential_store() -> None:
    """So a container or unit file can override a checked-out file without editing it."""
    store = SecretStore(_values={ws.ACCESS_KEY_SECRET_NAME: "from-file"})
    assert ws.resolve_access_key(store, environ={ws.ACCESS_KEY_ENV: "from-env"}) == "from-env"


def test_neither_source_means_no_key() -> None:
    assert ws.resolve_access_key(SecretStore(), environ={}) is None


# --- the handshake check -----------------------------------------------------------------


async def test_the_right_key_in_the_query_is_accepted(tmp_path) -> None:
    harness = await daemon(tmp_path, access_key=KEY)
    try:
        client = await harness.connect()
        assert await client.call("ping") == {}
    finally:
        await harness.close()


async def test_a_missing_key_is_401_at_the_handshake(tmp_path) -> None:
    """Rejected before it can become a connection, so it never reaches `initialize`."""
    harness = await daemon(tmp_path, access_key=KEY)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await websockets.connect(harness.url(key=None))
        assert caught.value.response.status_code == 401
    finally:
        await harness.close()


async def test_a_wrong_key_is_401(tmp_path) -> None:
    harness = await daemon(tmp_path, access_key=KEY)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await websockets.connect(harness.url(key="wrong"))
        assert caught.value.response.status_code == 401
    finally:
        await harness.close()


async def test_a_duplicated_key_cannot_be_smuggled_past(tmp_path) -> None:
    """`?key=wrong&key=right` must fail: a check scanning for any match would pass it."""
    harness = await daemon(tmp_path, access_key=KEY)
    try:
        url = f"ws://127.0.0.1:{harness.port}/mcp?key=wrong&key={KEY}"
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await websockets.connect(url)
        assert caught.value.response.status_code == 401
    finally:
        await harness.close()


async def test_an_authorization_bearer_header_is_accepted(tmp_path) -> None:
    """The carrier that keeps the secret out of access logs; the bridge sends this."""
    harness = await daemon(tmp_path, access_key=KEY)
    try:
        async with websockets.connect(
            harness.url(key=None), additional_headers={"Authorization": f"Bearer {KEY}"}
        ) as websocket:
            await websocket.send('{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}')
            assert '"result"' in await websocket.recv()
    finally:
        await harness.close()


async def test_the_admin_path_requires_the_same_key(tmp_path) -> None:
    harness = await daemon(tmp_path, access_key=KEY)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await websockets.connect(harness.url(path="/admin", key=None))
        assert caught.value.response.status_code == 401
    finally:
        await harness.close()


async def test_the_key_value_is_never_logged(tmp_path, caplog) -> None:
    """The whole reason a query parameter is the wrong carrier: URLs get written down."""
    import logging

    with caplog.at_level(logging.DEBUG):
        harness = await daemon(tmp_path, access_key=KEY)
        try:
            client = await harness.connect()
            await client.call("ping")
        finally:
            await harness.close()
    assert KEY not in caplog.text
    assert "characters" in caplog.text  # the length, which distinguishes the two mistakes


# --- the keyless loopback deployment ------------------------------------------------------


async def test_a_keyless_loopback_daemon_accepts_a_client_with_no_headers_at_all(
    tmp_path,
) -> None:
    """The remote-backend deployment, stated once as a proposition.

    A gateway on another machine, reached through an SSH port forward, binds `127.0.0.1` and
    is started with no access key: SSH access *is* the authentication, and there is no
    credential on the client's machine at all. What arrives through that forward is a client
    with **no `Origin` and no `Authorization`** -- and both of those have to be accepted
    together for the deployment to work.

    Every half of that is already covered: `test_loopback_binds_without_a_key`,
    `test_neither_source_means_no_key`, `tests/test_ws_origin.py::test_no_origin_is_allowed`.
    None of them says the whole thing, which until now passed by construction rather than by
    intent. It is intent now -- see `src/desktop/README.md` and `src/desktop/src-tauri/src/
    tunnel.rs` -- so tightening `_access_check` should fail a Python test here rather than
    somebody's window in a month.

    Both paths, because `/admin` is where the config editing that makes remote mode useful
    actually happens.
    """
    harness = await daemon(tmp_path)
    try:
        for path in ("/mcp", "/admin"):
            socket = await websockets.connect(f"ws://127.0.0.1:{harness.port}{path}")
            await socket.close()
    finally:
        await harness.close()


def test_the_only_keyless_door_is_loopback() -> None:
    """And the reason the advice is "bind loopback and tunnel" rather than "bind the LAN".

    A forward's far end is always `127.0.0.1` on the machine the daemon runs on, so remote
    mode never needs -- and must never encourage -- the exposed bind this refuses.
    """
    ws.refuse_unauthenticated_bind("127.0.0.1", None, False)
    with pytest.raises(ws.UnauthenticatedBindError):
        ws.refuse_unauthenticated_bind("0.0.0.0", None, False)
