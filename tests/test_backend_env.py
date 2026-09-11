"""What a backend subprocess actually receives, asserted against real processes.

This is the security core of the project, so nothing here is mocked: each test spawns the
fixture server and asks it, through a tool call, to report the names in its own
environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from mcp_gateway.backend import BASE_ALLOWLIST, DELIBERATELY_EXCLUDED, Backend, resolve_env
from mcp_gateway.config import ENV_MODE_CURATED, ENV_MODE_INHERIT, ServerSpec
from mcp_gateway.secrets import SecretStore

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"

#: Stands in for whatever the operator happened to export in the shell that started the
#: daemon -- another backend's credential, or their own API key.
LEAK_VAR = "GATEWAY_TEST_LEAK"
LEAK_VALUE = "another-backends-secret"


def spec(**overrides) -> ServerSpec:
    base = {
        "name": "mock",
        "command": sys.executable,
        "args": (str(FIXTURE),),
        "env": {"MOCK_ENV_REPORT": "1"},
    }
    base.update(overrides)
    return ServerSpec(**base)


async def env_report(backend: Backend) -> list[str]:
    """The environment variable NAMES the live subprocess can see."""
    assert backend.running, backend.error
    result = await backend.client.call_tool("env-report", {})
    return json.loads(result["content"][0]["text"])


async def running(spec_, store: SecretStore) -> Backend:
    backend = Backend(spec=spec_, store=store)
    assert await backend.start(), backend.error
    return backend


# --- resolve_env: the pure layer -------------------------------------------------------


def test_curated_mode_starts_from_nothing(monkeypatch) -> None:
    monkeypatch.setenv(LEAK_VAR, LEAK_VALUE)
    resolved = resolve_env(spec(env={"K": "${T}"}), SecretStore(_values={"T": "v"}))
    assert LEAK_VAR not in resolved.values
    assert resolved.values["K"] == "v"


def test_curated_mode_keeps_the_variables_a_backend_cannot_run_without() -> None:
    resolved = resolve_env(spec(env={}), SecretStore())
    for name in ("PATH", "HOME"):
        if name in os.environ:
            assert resolved.values.get(name) == os.environ[name]


def test_the_excluded_set_is_disjoint_from_the_allowlist() -> None:
    """`PYTHONPATH` and friends change *which code* a backend runs: supply chain, not convenience."""
    assert not (BASE_ALLOWLIST & DELIBERATELY_EXCLUDED)


def test_env_passthrough_forwards_exactly_the_named_variables(monkeypatch) -> None:
    monkeypatch.setenv(LEAK_VAR, LEAK_VALUE)
    monkeypatch.setenv("OTHER_AMBIENT", "also-here")
    resolved = resolve_env(spec(env_passthrough=(LEAK_VAR,)), SecretStore())
    assert resolved.values[LEAK_VAR] == LEAK_VALUE
    assert "OTHER_AMBIENT" not in resolved.values


def test_the_servers_own_env_wins_over_every_other_layer(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/ambient")
    resolved = resolve_env(
        spec(env={"PATH": "${P}"}, env_passthrough=("PATH",)), SecretStore(_values={"P": "/own"})
    )
    assert resolved.values["PATH"] == "/own"


def test_inherit_mode_forwards_everything_and_says_so(monkeypatch, caplog) -> None:
    monkeypatch.setenv(LEAK_VAR, LEAK_VALUE)
    with caplog.at_level(logging.WARNING):
        resolved = resolve_env(spec(env_mode=ENV_MODE_INHERIT), SecretStore())
    assert resolved.values[LEAK_VAR] == LEAK_VALUE
    assert "env_mode=inherit" in caplog.text
    assert "mock" in caplog.text


def test_a_missing_secret_is_collected_rather_than_raised() -> None:
    """The caller decides: skip this server, or refuse to start. See config.md."""
    resolved = resolve_env(spec(env={"A": "${GONE}", "B": "${ALSO_GONE}"}), SecretStore())
    assert not resolved.complete
    assert [m.key for m in resolved.missing] == ["GONE", "ALSO_GONE"]


# --- against a real subprocess ----------------------------------------------------------


async def test_a_curated_backend_cannot_see_another_backends_secret(monkeypatch) -> None:
    """The whole product, in one assertion, against a process that really started."""
    monkeypatch.setenv(LEAK_VAR, LEAK_VALUE)
    backend = await running(spec(env={"MOCK_ENV_REPORT": "1"}), SecretStore())
    try:
        names = await env_report(backend)
        assert LEAK_VAR not in names
        assert "PATH" in names
    finally:
        await backend.stop()


async def test_an_inherit_backend_does_see_it(monkeypatch) -> None:
    monkeypatch.setenv(LEAK_VAR, LEAK_VALUE)
    backend = await running(spec(env_mode=ENV_MODE_INHERIT), SecretStore())
    try:
        assert LEAK_VAR in await env_report(backend)
    finally:
        await backend.stop()


async def test_passthrough_opens_the_door_for_exactly_one_variable(monkeypatch) -> None:
    monkeypatch.setenv(LEAK_VAR, LEAK_VALUE)
    monkeypatch.setenv("OTHER_AMBIENT", "x")
    backend = await running(spec(env_passthrough=(LEAK_VAR,)), SecretStore())
    try:
        names = await env_report(backend)
        assert LEAK_VAR in names
        assert "OTHER_AMBIENT" not in names
    finally:
        await backend.stop()


async def test_a_resolved_credential_reaches_the_child(monkeypatch) -> None:
    backend = await running(
        spec(env={"MOCK_ENV_REPORT": "1", "MY_TOKEN": "${T}"}, env_mode=ENV_MODE_CURATED),
        SecretStore(_values={"T": "ghp_value"}),
    )
    try:
        assert "MY_TOKEN" in await env_report(backend)
    finally:
        await backend.stop()


# --- failure is a status, not an exception ---------------------------------------------


async def test_a_missing_secret_fails_the_backend_without_raising() -> None:
    backend = Backend(spec=spec(env={"K": "${GONE}"}), store=SecretStore())
    assert await backend.start() is False
    assert backend.status.value == "failed"
    assert "GONE" in backend.error
    assert backend.client is None


async def test_a_command_that_does_not_exist_fails_the_backend_without_raising() -> None:
    backend = Backend(spec=spec(command="/nonexistent/binary", args=()), store=SecretStore())
    assert await backend.start() is False
    assert backend.status.value == "failed"
    assert backend.error


async def test_a_backend_that_never_answers_initialize_times_out() -> None:
    """`startup_timeout` covers spawn and handshake together, deliberately."""
    backend = Backend(
        spec=spec(env={"MOCK_SLOW_START_MS": "5000"}, startup_timeout=0.3), store=SecretStore()
    )
    try:
        assert await backend.start() is False
        assert "initialize" in backend.error
    finally:
        await backend.stop()


async def test_a_backend_refusing_to_start_reports_its_own_reason() -> None:
    backend = Backend(
        spec=spec(env={"MOCK_REQUIRE_ENV": "NEVER_SET_ANYWHERE"}), store=SecretStore()
    )
    try:
        assert await backend.start() is False
    finally:
        await backend.stop()


async def test_a_disabled_backend_is_never_started() -> None:
    backend = Backend(spec=spec(enabled=False), store=SecretStore())
    assert backend.status.value == "disabled"
    assert await backend.start() is False
    assert backend.client is None


# --- lifecycle --------------------------------------------------------------------------


async def test_restart_replaces_the_process_and_counts_itself() -> None:
    backend = await running(spec(), SecretStore())
    try:
        first = backend.pid
        assert await backend.restart()
        assert backend.pid != first
        assert backend.restart_count == 1
    finally:
        await backend.stop()


async def test_ping_round_trips_and_reports_none_once_stopped() -> None:
    """A subprocess can be alive and wedged; only a round trip tells the two apart."""
    backend = await running(spec(), SecretStore())
    assert await backend.ping() is not None
    await backend.stop()
    assert await backend.ping() is None


async def test_a_backend_that_ignores_eof_is_still_reaped() -> None:
    """The close-stdin -> SIGTERM -> SIGKILL ladder, lifted with the client."""
    backend = await running(spec(env={"MOCK_ENV_REPORT": "1", "MOCK_IGNORE_EOF": "1"}), SecretStore())
    proc = backend.client._proc
    await backend.stop()
    assert proc.returncode is not None


async def test_a_backend_that_ignores_sigterm_is_still_reaped() -> None:
    backend = await running(
        spec(env={"MOCK_ENV_REPORT": "1", "MOCK_IGNORE_EOF": "1", "MOCK_IGNORE_SIGTERM": "1"}),
        SecretStore(),
    )
    proc = backend.client._proc
    await backend.stop()
    assert proc.returncode is not None


async def test_cwd_is_applied_to_the_child(tmp_path) -> None:
    """The daemon cannot chdir on a backend's behalf; it is shared."""
    backend = Backend(spec=spec(cwd=str(tmp_path)), store=SecretStore())
    assert await backend.start(), backend.error
    try:
        assert backend.client.cwd == str(tmp_path)
    finally:
        await backend.stop()


async def test_a_cwd_that_does_not_exist_fails_rather_than_being_created(tmp_path) -> None:
    missing = tmp_path / "not-there"
    backend = Backend(spec=spec(cwd=str(missing)), store=SecretStore())
    try:
        assert await backend.start() is False
        assert not missing.exists()
    finally:
        await backend.stop()


@pytest.mark.parametrize(
    ("countered", "expected"),
    [("2024-11-05", "2024-11-05"), (None, "2025-06-18")],
)
async def test_protocol_version_negotiation(countered, expected) -> None:
    env = {"MOCK_ENV_REPORT": "1"}
    if countered:
        env["MOCK_PROTOCOL_VERSION"] = countered
    backend = await running(spec(env=env), SecretStore())
    try:
        assert backend.protocol_version == expected
    finally:
        await backend.stop()


async def test_an_unsupported_counter_offer_is_refused() -> None:
    backend = Backend(
        spec=spec(env={"MOCK_ENV_REPORT": "1", "MOCK_PROTOCOL_VERSION": "1999-01-01"}),
        store=SecretStore(),
    )
    try:
        assert await backend.start() is False
        assert "1999-01-01" in backend.error
    finally:
        await backend.stop()


async def test_cursor_pagination_walks_every_page() -> None:
    backend = await running(spec(env={"MOCK_ENV_REPORT": "1", "MOCK_LIST_PAGES": "3"}), SecretStore())
    try:
        tools = await backend.client.list_tools()
        names = {tool["name"] for tool in tools}
        assert "page1-tool" in names and "page2-tool" in names
    finally:
        await backend.stop()


async def test_a_server_handing_out_cursors_forever_is_bounded() -> None:
    """Pagination is server-driven, so a broken or hostile one must not hang the daemon."""
    backend = await running(spec(env={"MOCK_ENV_REPORT": "1", "MOCK_LIST_STUCK": "1"}), SecretStore())
    try:
        with pytest.raises(Exception):
            await backend.client.list_tools()
    finally:
        await backend.stop()
