"""TLS on the one port: `wss://`, `https://`, and the refusals around them.

What matters is that turning TLS on changes the socket and nothing above it -- the MCP
handshake, the Streamable HTTP divert that sniffs request heads, and the UI's static assets
all work exactly as they do in plaintext -- and that a certificate the daemon cannot use
stops it rather than letting it fall back to sending the access key in the clear.

The certificates are minted per session by the `openssl` binary. `cryptography` would do it
in-process, but it is a Rust extension the project deliberately does not depend on, and
pulling it in for a test fixture would be the one place it leaked in.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest
import websockets

from mcp_gateway import bridge, cli, webui
from mcp_gateway import transport_ws as ws
from tests.fixtures.http_client import HttpClient
from tests.fixtures.ws_client import Client, daemon

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def _mint(directory: Path, name: str) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and localhost, and its key."""
    cert, key = directory / f"{name}.pem", directory / f"{name}-key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture(scope="module")
def minted(tmp_path_factory) -> tuple[Path, Path]:
    return _mint(tmp_path_factory.mktemp("tls"), "gateway")


@pytest.fixture(scope="module")
def other(tmp_path_factory) -> tuple[Path, Path]:
    return _mint(tmp_path_factory.mktemp("tls-other"), "other")


def _trusting(cert: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cert))


# --- building the context ------------------------------------------------------------------


def test_no_certificate_means_plaintext() -> None:
    assert ws.tls_context(None, None) is None


def test_a_key_without_a_certificate_is_refused() -> None:
    """Almost certainly a typo in the variable name, and plaintext is the wrong guess."""
    with pytest.raises(ws.TlsError):
        ws.tls_context(None, "/etc/ssl/private/gateway.key")


def test_a_missing_certificate_names_the_path(tmp_path) -> None:
    with pytest.raises(ws.TlsError) as caught:
        ws.tls_context(tmp_path / "nope.pem", None)
    assert "nope.pem" in str(caught.value)


def test_a_key_that_does_not_match_its_certificate_is_refused(minted, other) -> None:
    """The mistake that actually happens at renewal time, and only loading catches it."""
    with pytest.raises(ws.TlsError):
        ws.tls_context(minted[0], other[1])


def test_one_file_can_carry_both_halves(minted, tmp_path) -> None:
    combined = tmp_path / "combined.pem"
    combined.write_bytes(minted[0].read_bytes() + minted[1].read_bytes())
    assert isinstance(ws.tls_context(combined), ssl.SSLContext)


# --- the wire ------------------------------------------------------------------------------


async def test_mcp_over_wss(tmp_path, minted) -> None:
    harness = await daemon(tmp_path, tls=ws.tls_context(*minted))
    try:
        websocket = await websockets.connect(
            f"wss://127.0.0.1:{harness.port}/mcp", ssl=_trusting(minted[0])
        )
        client = Client(websocket)
        try:
            result = await client.initialize()
            assert result["serverInfo"]["name"]
        finally:
            await client.close()
    finally:
        await harness.close()


async def test_a_plaintext_client_gets_no_session(tmp_path, minted) -> None:
    """`ws://` against a TLS port fails the handshake; nothing is served in the clear."""
    harness = await daemon(tmp_path, tls=ws.tls_context(*minted))
    try:
        with pytest.raises(Exception):
            async with websockets.connect(f"ws://127.0.0.1:{harness.port}/mcp", open_timeout=3):
                pass
    finally:
        await harness.close()


async def test_a_client_that_does_not_trust_the_certificate_is_not_let_in(tmp_path, minted, other) -> None:
    harness = await daemon(tmp_path, tls=ws.tls_context(*minted))
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            await websockets.connect(f"wss://127.0.0.1:{harness.port}/mcp", ssl=_trusting(other[0]))
    finally:
        await harness.close()


async def test_streamable_http_over_https(tmp_path, minted) -> None:
    """The divert reads request heads, and under TLS it must be reading plaintext ones."""
    harness = await daemon(tmp_path, tls=ws.tls_context(*minted))
    try:
        client = HttpClient(harness.port, ssl=_trusting(minted[0]))
        response = await client.initialize()
        assert response.status == 200
        assert response.session_id
    finally:
        await harness.close()


async def test_the_ui_is_served_over_https(tmp_path, minted) -> None:
    harness = await daemon(tmp_path, tls=ws.tls_context(*minted))
    try:
        client = HttpClient(harness.port, ssl=_trusting(minted[0]))
        response = await client.send("GET", path="/ui/", accept=None, session=None)
        assert response.status == 200
        assert b"<html" in response.body.lower()
    finally:
        await harness.close()


def test_a_page_served_over_https_may_open_its_sockets() -> None:
    """The UI builds `wss://` from `location`, and the browser reports an https origin."""
    assert ws.origin_permitted("https://127.0.0.1:8765", ws.own_origins("127.0.0.1", 8765))


# --- saying so -----------------------------------------------------------------------------


def test_plaintext_off_loopback_is_warned_about(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        ws.warn_plaintext_off_loopback("0.0.0.0", tls=False)
    assert ws.TLS_CERT_ENV in caplog.text


@pytest.mark.parametrize(("host", "tls"), [("127.0.0.1", False), ("0.0.0.0", True)])
def test_loopback_or_tls_is_not(caplog, host, tls) -> None:
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        ws.warn_plaintext_off_loopback(host, tls=tls)
    assert not caplog.records


def test_the_banner_url_says_https() -> None:
    assert webui.url("127.0.0.1", 8765, tls=True).startswith("https://127.0.0.1:8765/")
    assert webui.url("127.0.0.1", 8765).startswith("http://")


def test_check_refuses_a_certificate_it_cannot_load(tmp_path, capsys) -> None:
    (tmp_path / "servers.yaml").write_text("servers: {}\n")
    (tmp_path / "gateway.env").write_text("")
    (tmp_path / "gateway.env").chmod(0o600)
    code = cli.check(
        tmp_path / "servers.yaml", tmp_path / "gateway.env", tls_cert=tmp_path / "nope.pem"
    )
    assert code == cli.EXIT_REFUSED
    assert "nope.pem" in capsys.readouterr().err


def test_check_names_the_certificate_it_would_serve(tmp_path, minted, capsys) -> None:
    (tmp_path / "servers.yaml").write_text("servers: {}\n")
    (tmp_path / "gateway.env").write_text("")
    (tmp_path / "gateway.env").chmod(0o600)
    code = cli.check(
        tmp_path / "servers.yaml", tmp_path / "gateway.env", tls_cert=minted[0], tls_key=minted[1]
    )
    assert code == 0
    assert f"tls: {minted[0]}" in capsys.readouterr().err


# --- the bridge ----------------------------------------------------------------------------


def test_the_bridge_leaves_plaintext_urls_alone(minted) -> None:
    """`websockets` refuses an `ssl` argument on `ws://`, so none is passed."""
    assert bridge.client_tls("ws://127.0.0.1:8765/mcp", minted[0]) is None
    assert "ssl" not in bridge.connect_kwargs(None, None)


def test_the_bridge_trusts_the_ca_file_it_is_given(minted) -> None:
    context = bridge.client_tls("wss://gateway.example:8765/mcp", minted[0])
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert bridge.connect_kwargs("k", context)["ssl"] is context


def test_the_bridge_reads_its_ca_file_from_the_environment() -> None:
    args = argparse.Namespace(ca_file=None)
    assert bridge.resolve_ca_file(args, {bridge.CA_FILE_ENV: "/etc/ca.pem"}) == Path("/etc/ca.pem")
    assert bridge.resolve_ca_file(args, {}) is None
