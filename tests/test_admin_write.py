"""Writing `servers.yaml` from `/admin`, and the four rules `config_writer.md` states.

Split between the writer called directly -- where the file on disk is the assertion -- and
the `/admin` verbs driven over the wire, where what matters is that a refusal is a JSON-RPC
error and a success reloads.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from mcp_gateway import config, config_writer, errors
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

COMMENTED = f"""# The catalogue. This comment is load-bearing in these tests.
version: 1

defaults:
  # Why the timeout is what it is.
  timeout: 30.0

servers:
  alpha:
    # Alpha's own note.
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MY_TOKEN: "${{ALPHA_TOKEN}}"
    description: "The alpha backend"
"""
ENV = "ALPHA_TOKEN=ghp_admin_write_secret\n"


@pytest.fixture
def catalogue(tmp_path: Path) -> Path:
    path = tmp_path / "servers.yaml"
    path.write_text(COMMENTED)
    return path


# --- rule 1: round-trip, so the comments survive ----------------------------------------


def test_an_edit_keeps_every_comment(catalogue: Path) -> None:
    """The whole reason the writer is `typ="rt"`. A UI that ate these is used once."""
    config_writer.update_server(catalogue, "alpha", {"description": "edited"})
    text = catalogue.read_text()
    assert "# The catalogue. This comment is load-bearing in these tests." in text
    assert "# Why the timeout is what it is." in text
    assert "# Alpha's own note." in text
    assert 'description: edited' in text or 'description: "edited"' in text


def test_adding_a_server_keeps_the_existing_one_intact(catalogue: Path) -> None:
    config_writer.add_server(catalogue, "beta", {"command": "/usr/bin/true"})
    parsed = config.load(catalogue)
    assert set(parsed.servers) == {"alpha", "beta"}
    assert parsed.servers["alpha"].env == {"MY_TOKEN": "${ALPHA_TOKEN}"}
    assert "# Alpha's own note." in catalogue.read_text()


def test_a_reference_is_written_back_as_a_reference(catalogue: Path) -> None:
    """Never resolved. A UI that wrote the value would publish it to the repository."""
    config_writer.add_server(
        catalogue, "beta", {"command": "/usr/bin/true", "env": {"T": "${BETA_TOKEN}"}}
    )
    assert "${BETA_TOKEN}" in catalogue.read_text()


# --- rule 2: validate before writing -----------------------------------------------------


@pytest.mark.parametrize(
    "name, spec",
    [
        ("nocmd", {"description": "no command"}),
        ("bad__name", {"command": "/usr/bin/true"}),
        ("badkey", {"command": "/usr/bin/true", "nonsense": 1}),
        ("interp", {"command": "${SECRET}"}),
        ("badtype", {"command": "/usr/bin/true", "timeout": "soon"}),
    ],
)
def test_an_invalid_edit_writes_nothing_at_all(catalogue: Path, name, spec) -> None:
    """Not 'writes and then errors'. The file is byte-identical afterwards."""
    before = catalogue.read_bytes()
    with pytest.raises(config.ConfigError):
        config_writer.add_server(catalogue, name, spec)
    assert catalogue.read_bytes() == before


def test_the_validator_is_the_readers_own(catalogue: Path) -> None:
    """`ConfigWriteError` is a `ConfigError`, so a refused write maps to -32602 like a read."""
    assert issubclass(config_writer.ConfigWriteError, config.ConfigError)
    with pytest.raises(config_writer.ConfigWriteError) as caught:
        config_writer.add_server(catalogue, "x", {"command": "/usr/bin/true", "bogus": True})
    assert "bogus" in str(caught.value)


def test_updating_a_server_that_is_not_there_is_refused(catalogue: Path) -> None:
    for operation in (
        lambda: config_writer.update_server(catalogue, "ghost", {"enabled": False}),
        lambda: config_writer.remove_server(catalogue, "ghost"),
    ):
        with pytest.raises(config_writer.ConfigWriteError):
            operation()


def test_adding_a_name_that_exists_is_refused(catalogue: Path) -> None:
    with pytest.raises(config_writer.ConfigWriteError):
        config_writer.add_server(catalogue, "alpha", {"command": "/usr/bin/true"})


# --- rule 3: a literal env value warns, and does not refuse ------------------------------


def test_a_literal_env_value_is_a_warning(catalogue: Path) -> None:
    warnings, _text = config_writer.add_server(
        catalogue, "beta", {"command": "/usr/bin/true", "env": {"FLAG": "1"}}
    )
    assert any("servers.beta.env.FLAG" in w for w in warnings)
    assert "gateway.env" in " ".join(warnings)
    # Written anyway: `MOCK_MCP_SCHEMA_ZOO: "1"` in servers.dev.yaml is a legitimate literal.
    assert config.load(catalogue).servers["beta"].env == {"FLAG": "1"}


def test_a_reference_is_not_warned_about(catalogue: Path) -> None:
    warnings, _text = config_writer.add_server(
        catalogue, "beta", {"command": "/usr/bin/true", "env": {"T": "${BETA}"}}
    )
    assert not any("servers.beta" in w for w in warnings)


# --- rule 4: atomic, with a backup -------------------------------------------------------


def test_a_write_leaves_a_backup(catalogue: Path) -> None:
    original = catalogue.read_text()
    config_writer.update_server(catalogue, "alpha", {"description": "edited"})
    backup = catalogue.with_suffix(".yaml.bak")
    assert backup.exists()
    assert backup.read_text() == original


def test_a_write_preserves_the_file_mode(catalogue: Path) -> None:
    catalogue.chmod(0o600)
    config_writer.update_server(catalogue, "alpha", {"description": "edited"})
    assert stat.S_IMODE(catalogue.stat().st_mode) == 0o600


def test_no_temporary_file_is_left_behind(catalogue: Path) -> None:
    config_writer.update_server(catalogue, "alpha", {"description": "edited"})
    assert not list(catalogue.parent.glob(".*.tmp"))


def test_a_missing_catalogue_is_created(tmp_path: Path) -> None:
    """Adding the first server to a checkout with no servers.yaml must just work."""
    path = tmp_path / "servers.yaml"
    config_writer.add_server(path, "alpha", {"command": "/usr/bin/true"})
    assert config.load(path).servers["alpha"].command == "/usr/bin/true"


# --- merge semantics ---------------------------------------------------------------------


def test_update_merges_rather_than_replaces(catalogue: Path) -> None:
    """An edit that toggles `enabled` must not drop the env block it did not mention."""
    config_writer.update_server(catalogue, "alpha", {"enabled": False})
    spec = config.load(catalogue).servers["alpha"]
    assert spec.enabled is False
    assert spec.env == {"MY_TOKEN": "${ALPHA_TOKEN}"}
    assert spec.description == "The alpha backend"


def test_an_emptied_key_is_removed_rather_than_written(catalogue: Path) -> None:
    """`args: []` means what its absence means, and is noise in a file people read."""
    config_writer.update_server(catalogue, "alpha", {"args": []})
    assert "args:" not in catalogue.read_text()


def test_set_servers_replaces_the_mapping_and_keeps_the_rest(catalogue: Path) -> None:
    config_writer.set_servers(catalogue, {"beta": {"command": "/usr/bin/true"}})
    text = catalogue.read_text()
    parsed = config.load(catalogue)
    assert set(parsed.servers) == {"beta"}
    assert "# Why the timeout is what it is." in text
    assert parsed.servers["beta"].timeout == 30.0  # defaults survived


# --- over the wire -----------------------------------------------------------------------


async def test_add_update_and_remove_round_trip_over_admin(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=COMMENTED, env=ENV)
    path = tmp_path / "servers.yaml"
    try:
        client = await harness.connect("/admin", initialize=False)

        result = await client.call(
            "admin.backend.add",
            {"name": "beta", "spec": {"command": "/usr/bin/true", "enabled": False}},
        )
        assert result["written"] is True
        assert "reload" in result, "a write reloads by default"
        assert harness.gateway.backend("beta") is not None

        await client.call("admin.backend.update", {"name": "beta", "changes": {"required": True}})
        assert config.load(path).servers["beta"].required is True

        await client.call("admin.backend.remove", {"name": "beta"})
        assert set(config.load(path).servers) == {"alpha"}
        assert harness.gateway.backend("beta") is None
    finally:
        await harness.close()


async def test_a_write_can_decline_to_reload(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=COMMENTED, env=ENV)
    try:
        client = await harness.connect("/admin", initialize=False)
        result = await client.call(
            "admin.backend.add",
            {"name": "beta", "spec": {"command": "/usr/bin/true"}, "reload": False},
        )
        assert "reload" not in result
        assert harness.gateway.backend("beta") is None, "the file changed; the daemon did not"
    finally:
        await harness.close()


async def test_a_refused_write_is_an_error_and_changes_nothing(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=COMMENTED, env=ENV)
    path = tmp_path / "servers.yaml"
    try:
        client = await harness.connect("/admin", initialize=False)
        before = path.read_bytes()
        response = await client.request("admin.backend.add", {"name": "beta", "spec": {}})
        assert response["error"]["code"] == errors.INVALID_PARAMS
        assert "command" in response["error"]["message"]
        assert path.read_bytes() == before
    finally:
        await harness.close()


async def test_the_warnings_reach_the_ui(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=COMMENTED, env=ENV)
    try:
        client = await harness.connect("/admin", initialize=False)
        result = await client.call(
            "admin.backend.add",
            {"name": "beta", "spec": {"command": "/usr/bin/true", "env": {"FLAG": "1"}}},
        )
        assert any("servers.beta.env.FLAG" in w for w in result["warnings"])
    finally:
        await harness.close()


async def test_no_admin_method_takes_a_path(tmp_path) -> None:
    """The catalogue this daemon was started with, not "any YAML this process can write"."""
    harness = await daemon(tmp_path, servers=COMMENTED, env=ENV)
    elsewhere = tmp_path / "elsewhere.yaml"
    try:
        client = await harness.connect("/admin", initialize=False)
        await client.call(
            "admin.backend.add",
            {
                "name": "beta",
                "spec": {"command": "/usr/bin/true"},
                # Ignored: `_write` passes `gateway.config_path` and reads nothing from here.
                "path": str(elsewhere),
                "config_path": str(elsewhere),
            },
        )
        assert not elsewhere.exists()
        assert "beta" in config.load(tmp_path / "servers.yaml").servers
    finally:
        await harness.close()


async def test_secrets_missing_names_the_key_and_never_its_value(tmp_path) -> None:
    """The answer to "why is this backend down" that does not require writing a credential."""
    harness = await daemon(tmp_path, servers=COMMENTED, env="")
    try:
        client = await harness.connect("/admin", initialize=False)
        result = await client.call("admin.secrets.missing")
        assert result["servers"] == {"alpha": ["ALPHA_TOKEN"]}
        assert "ghp_" not in str(result)
    finally:
        await harness.close()


async def test_a_disabled_server_is_not_reported_as_missing_a_secret(tmp_path) -> None:
    """Parking a backend is how an operator defers dealing with its credential."""
    servers = COMMENTED + "    enabled: false\n"
    harness = await daemon(tmp_path, servers=servers, env="")
    try:
        client = await harness.connect("/admin", initialize=False)
        assert (await client.call("admin.secrets.missing"))["servers"] == {}
    finally:
        await harness.close()
