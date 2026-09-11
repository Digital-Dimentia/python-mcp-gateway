"""The `gateway.env` grammar, and the three rules where dotenv implementations disagree."""

from __future__ import annotations

import logging
import stat

import pytest

from mcp_gateway import secrets


def test_parses_the_documented_grammar() -> None:
    parsed = secrets.parse(
        "# a comment\n"
        "\n"
        "export EXPORTED=1\n"
        "PLAIN=value\n"
        '  SPACED  =  trimmed  \n'
        "SINGLE='literal ${NOT_EXPANDED} \\n'\n"
        'DOUBLE="tab\\there"\n'
    )
    assert parsed == {
        "EXPORTED": "1",
        "PLAIN": "value",
        "SPACED": "trimmed",
        "SINGLE": "literal ${NOT_EXPANDED} \\n",
        "DOUBLE": "tab\there",
    }


def test_hash_is_a_legal_character_in_an_unquoted_value() -> None:
    """No inline comment stripping. A `#` in a token is a `#` in a token.

    This is the rule that silently truncates a credential when an implementation gets it
    wrong, and the truncation presents as a 401 from a third party hours later.
    """
    assert secrets.parse("KEY=abc#def\n") == {"KEY": "abc#def"}


def test_a_duplicate_key_is_an_error_naming_both_lines() -> None:
    """Keeping one of the two silently is how a rotated credential gets shadowed."""
    with pytest.raises(secrets.SecretError) as caught:
        secrets.parse("TOKEN=new\nOTHER=x\nTOKEN=stale\n")
    message = str(caught.value)
    assert "duplicate" in message
    assert ":3" in message and "line 1" in message


def test_an_empty_value_is_present_not_missing() -> None:
    store = secrets.SecretStore(_values=secrets.parse("BLANK=\n"))
    assert "BLANK" in store
    assert store.get("BLANK") == ""
    assert store.get("ABSENT") is None


@pytest.mark.parametrize(
    "text",
    ["novalue\n", "1LEADING_DIGIT=x\n", "has space=x\n", 'BAD="\\q"\n'],
)
def test_refuses_malformed_lines_with_a_line_number(text: str) -> None:
    with pytest.raises(secrets.SecretError) as caught:
        secrets.parse(text)
    assert ":1" in str(caught.value)


def test_repr_shows_key_names_and_never_values() -> None:
    """Layer one of three. A traceback is the place nobody thinks to check."""
    store = secrets.SecretStore(_values={"TOKEN": "ghp_supersecret"})
    assert "ghp_supersecret" not in repr(store)
    assert "ghp_supersecret" not in str(store)
    assert "TOKEN" in repr(store)


def test_redactable_values_are_longest_first_and_skip_short_ones() -> None:
    store = secrets.SecretStore(
        _values={"A": "ghp_token", "B": "Bearer ghp_token", "TINY": "xy"}
    )
    values = store.redactable_values()
    assert "xy" not in values
    assert values.index("Bearer ghp_token") < values.index("ghp_token")


def test_a_missing_file_is_an_empty_store(tmp_path) -> None:
    """The daemon must start on a machine nobody has configured yet."""
    store = secrets.load(tmp_path / "gateway.env")
    assert store.keys() == []
    with pytest.raises(secrets.SecretError):
        secrets.load(tmp_path / "gateway.env", required=True)


def test_a_readable_by_others_file_warns_but_loads(tmp_path, caplog) -> None:
    path = tmp_path / "gateway.env"
    path.write_text("TOKEN=x\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    with caplog.at_level(logging.WARNING):
        store = secrets.load(path)
    assert store.get("TOKEN") == "x"
    assert "chmod 600" in caplog.text


def test_loading_never_logs_a_value(tmp_path, caplog) -> None:
    """The window before `install_redaction` runs has to be safe on its own.

    `cli` configures logging, loads the store, then installs the filter. Anything the
    loader logged in between would be unredacted.
    """
    path = tmp_path / "gateway.env"
    path.write_text("TOKEN=ghp_supersecret\n")
    path.chmod(0o644)
    with caplog.at_level(logging.DEBUG):
        secrets.load(path)
    assert "ghp_supersecret" not in caplog.text
