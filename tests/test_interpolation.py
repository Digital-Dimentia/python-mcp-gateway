"""`${VAR}` resolution: the syntax, and the refusal to read `os.environ`."""

from __future__ import annotations

import pytest

from mcp_gateway import secrets

STORE = secrets.SecretStore(_values={"TOKEN": "ghp_x", "EMPTY": ""})


def test_substitutes_a_reference_anywhere_in_the_value() -> None:
    assert secrets.interpolate("Bearer ${TOKEN}!", STORE, where="t") == "Bearer ghp_x!"
    assert secrets.interpolate("${TOKEN}${TOKEN}", STORE, where="t") == "ghp_xghp_x"


def test_bare_dollar_name_is_not_interpolated() -> None:
    """`$` is common inside generated tokens; a bare-name rule would eat part of one."""
    assert secrets.interpolate("$TOKEN and $$x", STORE, where="t") == "$TOKEN and $$x"


def test_double_dollar_escapes_to_a_literal() -> None:
    assert secrets.interpolate("$${TOKEN}", STORE, where="t") == "${TOKEN}"


def test_an_empty_stored_value_resolves_rather_than_being_missing() -> None:
    assert secrets.interpolate("[${EMPTY}]", STORE, where="t") == "[]"


def test_never_falls_back_to_the_process_environment(monkeypatch) -> None:
    """The security thesis in one assertion.

    Reading `os.environ` would make a backend's credential depend on whichever shell
    launched the daemon, and would defeat curated env mode by the back door.
    """
    monkeypatch.setenv("AMBIENT", "leaked")
    with pytest.raises(secrets.SecretError) as caught:
        secrets.interpolate("${AMBIENT}", STORE, where="servers.x.env.Y")
    assert "AMBIENT" in str(caught.value)
    assert "servers.x.env.Y" in str(caught.value)


def test_collecting_mode_reports_every_miss_rather_than_the_first() -> None:
    """What `--check` needs: one run, every problem."""
    missing: list[secrets.MissingSecret] = []
    result = secrets.interpolate("${A}/${B}", STORE, where="w", missing=missing)
    assert result == "/"
    assert [m.key for m in missing] == ["A", "B"]
    assert all(m.where == "w" for m in missing)


def test_expand_path_expands_tilde_without_reading_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    assert secrets.expand_path("~/src", STORE, where="w") == "/home/someone/src"
    # $HOME is NOT expanded: expandvars would reintroduce the ambient fallback.
    assert secrets.expand_path("$HOME/src", STORE, where="w") == "$HOME/src"
