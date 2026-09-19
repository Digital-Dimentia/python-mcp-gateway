"""The container build script, and the invariants the shipped artefacts have to hold."""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_container_build_skips_cleanly_with_no_engine(tmp_path) -> None:
    """A machine without docker or podman must still pass `make package`.

    `REQUIRE_CONTAINER=1` is what turns a skip into a failure, and the release workflow sets
    it -- a release that ships without its image should not be quiet about it.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "container_image.py"),
            "--tag",
            "python-mcp-gateway:test",
            "--containerfile",
            str(REPO_ROOT / "Containerfile"),
            "--context",
            str(REPO_ROOT),
            "--output",
            str(tmp_path / "image.tar"),
        ],
        capture_output=True,
        text=True,
    )
    # Either it built, or it said clearly why it could not. Never a silent success.
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() or result.stderr.strip()


def test_the_containerfile_base_image_matches_the_python_floor() -> None:
    """requires-python, the classifiers, the CI matrix and the base image move together."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    containerfile = (REPO_ROOT / "Containerfile").read_text()
    classified = set(re.findall(r"Programming Language :: Python :: (3\.\d+)", pyproject))
    floor = min(classified, key=lambda v: int(v.split(".")[1]))
    assert f"FROM python:{floor}-slim" in containerfile


def test_the_containerfile_binds_all_interfaces_which_forces_a_key() -> None:
    """Inside a container, loopback is reachable only from the container itself.

    Binding 0.0.0.0 is therefore right, and the daemon's own guard then refuses to start
    without an access key -- which is the intended outcome, not an accident.
    """
    containerfile = (REPO_ROOT / "Containerfile").read_text()
    assert "MCP_GATEWAY_HOST=0.0.0.0" in containerfile
    # Set by environment, not baked into the entrypoint, so remote mode can undo it.
    assert 'ENTRYPOINT ["mcp-gateway"]' in containerfile


def test_the_image_reads_its_config_from_a_mounted_directory() -> None:
    """A directory, because the UI's save renames a new file over servers.yaml, which a
    single bind-mounted file cannot take."""
    containerfile = (REPO_ROOT / "Containerfile").read_text()
    assert "MCP_GATEWAY_CONFIG=/config/servers.yaml" in containerfile
    assert "VOLUME /config" in containerfile


def test_the_remote_compose_example_is_keyless_loopback_on_the_host_network() -> None:
    """The contract remote mode depends on: the desktop app sends no key, so the container
    must bind the box's own loopback -- which only host networking makes it."""
    from ruamel.yaml import YAML

    from mcp_gateway.config import load

    example = REPO_ROOT / "examples" / "remote-compose"
    compose = YAML(typ="safe", pure=True).load((example / "compose.yaml").read_text())
    gateway = compose["services"]["gateway"]
    assert gateway["network_mode"] == "host"
    assert gateway["environment"]["MCP_GATEWAY_HOST"] == "127.0.0.1"
    assert "ports" not in gateway
    assert "MCP_GATEWAY_WS_KEY" not in gateway["environment"]
    assert any(v.endswith(":/config") for v in gateway["volumes"])

    catalogue = load(example / "config" / "servers.yaml")
    assert all(spec.url for spec in catalogue.servers.values())
    env_example = (example / "config" / "gateway.env.example").read_text()
    assert not any(
        line.startswith("WS_ACCESS_KEY") for line in env_example.splitlines()
    ), "a key would make the desktop app's keyless remote connection a 401"


def test_the_launchd_job_is_a_valid_plist_and_holds_no_secret() -> None:
    """A plist in ~/Library/LaunchAgents is world-readable, and `launchctl print` shows
    EnvironmentVariables to anyone who asks."""
    path = REPO_ROOT / "scripts" / "com.dbuschman7.mcp-gateway.plist"
    with path.open("rb") as handle:
        job = plistlib.load(handle)
    assert job["Label"] == "com.dbuschman7.mcp-gateway"
    assert job["KeepAlive"]["SuccessfulExit"] is False
    assert "WS_ACCESS_KEY" not in str(job)
    assert "MCP_GATEWAY_WS_KEY" not in str(job)
    # npx will not resolve on launchd's default PATH.
    assert "/opt/homebrew/bin" in job["EnvironmentVariables"]["PATH"]


def test_the_shipped_catalogue_parses_and_names_no_secret_value() -> None:
    """servers.yaml is committed, so a value in it would be a value in git forever."""
    from mcp_gateway.config import load

    config = load(REPO_ROOT / "servers.yaml")
    assert config.servers
    for spec in config.servers.values():
        for key, template in spec.env.items():
            assert template.startswith("${") and template.endswith("}"), (spec.name, key)


def test_the_env_example_covers_every_reference_in_the_catalogue() -> None:
    """Otherwise a first run fails on a key the operator was never told to set."""
    from mcp_gateway.config import load
    from mcp_gateway.secrets import parse

    config = load(REPO_ROOT / "servers.yaml")
    example = parse((REPO_ROOT / "gateway.env.example").read_text())

    referenced = {
        template[2:-1]
        for spec in config.servers.values()
        for template in spec.env.values()
        if template.startswith("${")
    }
    missing = referenced - set(example)
    assert not missing, f"gateway.env.example does not mention: {sorted(missing)}"
    assert "WS_ACCESS_KEY" in example


def test_the_env_example_holds_no_values() -> None:
    from mcp_gateway.secrets import parse

    example = parse((REPO_ROOT / "gateway.env.example").read_text())
    assert all(value == "" for value in example.values()), example


def test_gateway_env_is_gitignored_and_the_example_is_not() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text()
    assert "\ngateway.env\n" in ignored
    assert "!gateway.env.example" in ignored
