"""`servers.yaml` parsing: the defaults, and every refusal that has a reason behind it."""

from __future__ import annotations

import pytest

from mcp_gateway import config

MINIMAL = """
servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
"""


def test_parses_a_minimal_catalogue_with_the_documented_defaults() -> None:
    parsed = config.parse(MINIMAL)
    spec = parsed.servers["github"]
    assert spec.command == "npx"
    assert spec.args == ("-y", "@modelcontextprotocol/server-github")
    assert spec.env_mode == config.ENV_MODE_CURATED
    assert spec.timeout == config.DEFAULT_TIMEOUT
    assert spec.startup_timeout == config.DEFAULT_STARTUP_TIMEOUT
    assert spec.enabled is True
    assert spec.required is False


def test_a_spec_holds_templates_and_never_a_value() -> None:
    """What makes a `ServerSpec` safe to log, to serve over /admin, and to diff on reload."""
    spec = config.parse(MINIMAL).servers["github"]
    assert spec.env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_TOKEN}"
    assert spec.env_keys == ["GITHUB_PERSONAL_ACCESS_TOKEN"]


def test_defaults_apply_and_an_entry_overrides_them() -> None:
    parsed = config.parse(
        """
defaults:
  timeout: 45
  env_mode: inherit
  cwd: /tmp
servers:
  a:
    command: x
  b:
    command: y
    timeout: 5
    env_mode: curated
"""
    )
    assert parsed.servers["a"].timeout == 45.0
    assert parsed.servers["a"].env_mode == config.ENV_MODE_INHERIT
    assert parsed.servers["a"].cwd == "/tmp"
    assert parsed.servers["b"].timeout == 5.0
    assert parsed.servers["b"].env_mode == config.ENV_MODE_CURATED


def test_the_defaults_block_is_kept_as_written() -> None:
    """Folded into the specs *and* retained, because only the block answers "what if I omit
    this?" -- the question an editor adding a server has to answer. See config.md."""
    parsed = config.parse(
        """
defaults:
  timeout: 45
servers:
  a:
    command: x
    timeout: 5
"""
    )
    assert parsed.defaults == {"timeout": 45}
    # Folding still happened, and an entry still wins over the block.
    assert parsed.servers["a"].timeout == 5.0


def test_a_catalogue_without_defaults_reports_an_empty_block() -> None:
    """Empty, not absent: a reader asking what a key falls back to should not have to
    distinguish "no block" from "block that is silent about this key"."""
    parsed = config.parse("servers:\n  a:\n    command: x\n")
    assert parsed.defaults == {}
    assert parsed.servers["a"].timeout == config.DEFAULT_TIMEOUT


def test_json_parses_through_the_identical_path() -> None:
    """YAML 1.2 is a superset of JSON, so a pasted Claude Desktop config needs no branch."""
    parsed = config.parse(
        '{"servers": {"fs": {"command": "npx", "args": ["-y", "server-filesystem"]}}}'
    )
    assert parsed.servers["fs"].args == ("-y", "server-filesystem")


def test_enabled_partitions_the_catalogue() -> None:
    parsed = config.parse(
        "servers:\n  a:\n    command: x\n  b:\n    command: y\n    enabled: false\n"
    )
    assert list(parsed.servers) == ["a", "b"]
    assert list(parsed.enabled) == ["a"]


def test_order_is_the_files_order() -> None:
    """`--list` prints it, and an operator reads the plan against the file they wrote."""
    parsed = config.parse("servers:\n  z:\n    command: x\n  a:\n    command: y\n")
    assert list(parsed.servers) == ["z", "a"]


@pytest.mark.parametrize(
    ("text", "expect_in_message"),
    [
        ("servers:\n  a:\n    command: x\n    typo: 1\n", "unknown key"),
        ("defaults:\n  typo: 1\nservers:\n  a:\n    command: x\n", "unknown key"),
        ("typo: 1\nservers: {}\n", "unknown key"),
        ("servers:\n  a:\n    args: []\n", "'command' or 'url' is required"),
        ("defaults: {}\n", "'servers' is required"),
        ("", "empty catalogue"),
        ("version: 2\nservers: {}\n", "not supported"),
        ("servers:\n  a:\n    command: x\n    timeout: 0\n", "greater than zero"),
        ("servers:\n  a:\n    command: x\n    timeout: nope\n", "expected a number"),
        ("servers:\n  a:\n    command: x\n    env_mode: weird\n", "expected one of"),
        ("servers:\n  a:\n    command: x\n    args: 'str'\n", "expected a list"),
        ("servers:\n  gateway:\n    command: x\n", "reserved"),
        ("servers:\n  a__b:\n    command: x\n", "ambiguous"),
        ("servers: 'not a map'\n", "must be a mapping"),
    ],
)
def test_refuses_with_a_message_that_says_what_to_fix(text: str, expect_in_message: str) -> None:
    with pytest.raises(config.ConfigError) as caught:
        config.parse(text)
    assert expect_in_message in str(caught.value)


def test_a_stringy_boolean_is_refused_rather_than_read_as_true() -> None:
    """YAML 1.2 reads `yes` as a string; a lenient rule would disable nothing."""
    with pytest.raises(config.ConfigError) as caught:
        config.parse("servers:\n  a:\n    command: x\n    enabled: 'yes'\n")
    assert "expected true or false" in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        'servers:\n  a:\n    command: "npx ${TOKEN}"\n',
        'servers:\n  a:\n    command: npx\n    args: ["--token=${TOKEN}"]\n',
    ],
)
def test_refuses_a_secret_reference_in_command_or_args(text: str) -> None:
    """argv is world-readable through `ps`; a flag would publish the secret it protects."""
    with pytest.raises(config.ConfigError) as caught:
        config.parse(text)
    assert "world-readable" in str(caught.value)
    assert "'env' block" in str(caught.value)


def test_interpolation_is_allowed_in_env_and_cwd() -> None:
    parsed = config.parse(
        'servers:\n  a:\n    command: x\n    cwd: "${ROOT}/sub"\n    env:\n      K: "${V}"\n'
    )
    assert parsed.servers["a"].cwd == "${ROOT}/sub"
    assert parsed.servers["a"].env == {"K": "${V}"}


def test_load_reports_a_missing_file_by_path(tmp_path) -> None:
    with pytest.raises(config.ConfigError) as caught:
        config.load(tmp_path / "servers.yaml")
    assert "no such catalogue" in str(caught.value)


def test_load_never_mutates_and_returns_a_fresh_object(tmp_path) -> None:
    """The whole guarantee behind reload: a bad parse leaves the running config untouched."""
    path = tmp_path / "servers.yaml"
    path.write_text(MINIMAL)
    first = config.load(path)
    path.write_text("servers:\n  a:\n    BROKEN\n")
    with pytest.raises(config.ConfigError):
        config.load(path)
    assert list(first.servers) == ["github"]
