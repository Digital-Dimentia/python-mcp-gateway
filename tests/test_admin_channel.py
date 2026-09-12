"""The `/admin` path: a plain method table, the same key, and no MCP handshake."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp_gateway import errors
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable
SECRET = "ghp_admin_channel_secret"

SERVERS = f"""
servers:
  alpha:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "alpha"
      MY_TOKEN: "${{ALPHA_TOKEN}}"
    description: "The alpha backend"
  parked:
    command: /usr/bin/true
    enabled: false
"""
ENV = f"ALPHA_TOKEN={SECRET}\n"


async def admin(harness):
    return await harness.connect("/admin", initialize=False)


async def test_a_method_works_with_no_handshake(tmp_path) -> None:
    """There is nothing to negotiate: the method table is fixed."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        assert await client.call("ping") == {}
    finally:
        await harness.close()


async def test_status_describes_the_daemon_and_its_connections(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        await harness.connect()  # an /mcp client to be described
        client = await admin(harness)
        status = await client.call("admin.status")
        assert status["uptime_seconds"] >= 0
        assert status["bind"].startswith("127.0.0.1:")
        paths = sorted(c["path"] for c in status["connections"])
        assert paths == ["/admin", "/mcp"]
    finally:
        await harness.close()


async def test_backends_lists_everything_configured(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        backends = (await client.call("admin.backends"))["backends"]
        by_name = {b["name"]: b for b in backends}
        assert set(by_name) == {"alpha", "parked"}
        assert by_name["parked"]["status"] == "disabled"
        assert by_name["alpha"]["description"] == "The alpha backend"
    finally:
        await harness.close()


async def test_health_includes_a_live_ping(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        health = await client.call("admin.health", {"name": "alpha"})
        assert health["ping_ms"] is not None
    finally:
        await harness.close()


async def test_config_get_returns_references_not_values(tmp_path) -> None:
    """A UI editing the config must write back `${VAR}`, not the token."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        config = await client.call("admin.config.get")
        assert config["servers"]["alpha"]["env"]["MY_TOKEN"] == "${ALPHA_TOKEN}"
        assert SECRET not in str(config)
    finally:
        await harness.close()


async def test_config_get_reports_the_defaults_block_beside_the_folded_specs(tmp_path) -> None:
    """The specs say what each backend runs with; only the block says what omitting a key
    means, which is what an editor adding a server has to show. See admin_channel.md."""
    servers = f"""
defaults:
  timeout: 45
servers:
  alpha:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "alpha"
  brisk:
    command: /usr/bin/true
    enabled: false
    timeout: 5
"""
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await admin(harness)
        config = await client.call("admin.config.get")
        assert config["defaults"] == {"timeout": 45}
        # Folded, too: the block reached the spec that omitted the key, and not the one
        # that set it. Reporting the block is in addition to that, never instead of it.
        assert config["servers"]["alpha"]["timeout"] == 45.0
        assert config["servers"]["brisk"]["timeout"] == 5.0
    finally:
        await harness.close()


async def test_config_get_reports_an_empty_defaults_block_when_the_file_has_none(tmp_path) -> None:
    """Present and empty, so a reader never has to tell "no block" from "silent block"."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        assert (await client.call("admin.config.get"))["defaults"] == {}
    finally:
        await harness.close()


async def test_secrets_keys_returns_names_only(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        result = await client.call("admin.secrets.keys")
        assert result["keys"] == ["ALPHA_TOKEN"]
        assert SECRET not in str(result)
    finally:
        await harness.close()


async def test_no_admin_method_returns_a_secret_value(tmp_path) -> None:
    """The promise, asserted across the whole surface rather than method by method."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        for method, params in [
            ("admin.status", {}),
            ("admin.backends", {}),
            ("admin.health", {}),
            ("admin.config.get", {}),
            ("admin.secrets.keys", {}),
        ]:
            assert SECRET not in str(await client.call(method, params)), method
    finally:
        await harness.close()


async def test_no_verb_anywhere_writes_a_credential(tmp_path) -> None:
    """The line that did not move when config writing landed.

    `admin.config.*` and `admin.backend.*` now edit servers.yaml on this path. There is
    still no method on any path that writes a value into gateway.env, and this is the test
    that says so -- see admin.md.
    """
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        for method in ("admin.secrets.set", "admin.secrets.get", "admin.env.set"):
            response = await client.request(method, {})
            assert response["error"]["code"] == errors.METHOD_NOT_FOUND, method
    finally:
        await harness.close()


async def test_the_config_verbs_reject_a_request_without_a_target(tmp_path) -> None:
    """Present, and still refusing a call that names nothing to write."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        for method in ("admin.config.set", "admin.backend.add", "admin.backend.remove"):
            response = await client.request(method, {})
            assert response["error"]["code"] == errors.INVALID_PARAMS, method
    finally:
        await harness.close()


async def test_admin_verbs_are_absent_from_the_models_tool_list(tmp_path) -> None:
    """The whole reason for a second path."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert not any(n.startswith("admin.") for n in names)
        assert not any("secrets" in n for n in names)
    finally:
        await harness.close()


async def test_reload_works_from_the_admin_path(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        plan = await client.call("admin.reload")
        assert plan["unchanged"] == ["alpha", "parked"] or "alpha" in plan["unchanged"]
    finally:
        await harness.close()


async def test_a_broken_reload_is_an_error_not_a_silent_success(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        (tmp_path / "servers.yaml").write_text("servers:\n  alpha:\n    typo: 1\n")
        response = await client.request("admin.reload")
        assert "error" in response
        assert "nothing changed" in response["error"]["message"]
    finally:
        await harness.close()


async def test_restart_works_from_the_admin_path(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        before = harness.gateway.backend("alpha").pid
        result = await client.call("admin.backend.restart", {"name": "alpha"})
        assert result["running"] is True
        assert harness.gateway.backend("alpha").pid != before
    finally:
        await harness.close()


async def test_restarting_an_unknown_backend_is_invalid_params(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        response = await client.request("admin.backend.restart", {"name": "nope"})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()


async def test_an_unknown_method_is_method_not_found(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        response = await client.request("admin.invented")
        assert response["error"]["code"] == errors.METHOD_NOT_FOUND
    finally:
        await harness.close()


async def test_the_admin_path_needs_the_same_key(tmp_path) -> None:
    import websockets

    harness = await daemon(tmp_path, servers=SERVERS, env=ENV, access_key="k")
    try:
        try:
            await websockets.connect(harness.url(path="/admin", key=None))
            raise AssertionError("the admin path must require the key")
        except websockets.exceptions.InvalidStatus as caught:
            assert caught.response.status_code == 401
    finally:
        await harness.close()


async def test_log_tailing_streams_redacted_records(tmp_path) -> None:
    """The stream sits after the redaction filter, which is the whole safety argument."""
    import logging

    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        started = await client.call("admin.logs.tail", {"backlog": False})
        assert started["streaming"] is True

        logging.getLogger("probe").warning("leaking %s here", SECRET)
        for _ in range(100):
            if [n for n in client.notifications if n["method"] == "admin.logs"]:
                break
            await asyncio.sleep(0.02)

        entries = [n["params"] for n in client.notifications if n["method"] == "admin.logs"]
        assert entries, "the record should have been streamed"
        assert not any(SECRET in e["message"] for e in entries)
        assert any("***" in e["message"] for e in entries)
    finally:
        await harness.close()


async def test_a_second_tail_on_one_connection_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await admin(harness)
        await client.call("admin.logs.tail", {"backlog": False})
        response = await client.request("admin.logs.tail", {})
        assert response["error"]["code"] == errors.INVALID_PARAMS
        assert (await client.call("admin.logs.stop"))["streaming"] is False
    finally:
        await harness.close()


async def test_the_backlog_explains_what_just_happened(tmp_path) -> None:
    """A UI that attaches after a failed reload should still be able to see why."""
    import logging

    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        logging.getLogger("probe").warning("something worth explaining happened")
        client = await admin(harness)
        started = await client.call("admin.logs.tail")
        assert any(
            "something worth explaining" in e["message"] for e in started["backlog"]
        )

        # And the opposite: a client that asks for none gets none.
        await client.call("admin.logs.stop")
        second = await admin(harness)
        assert (await second.call("admin.logs.tail", {"backlog": False}))["backlog"] == []
    finally:
        await harness.close()
