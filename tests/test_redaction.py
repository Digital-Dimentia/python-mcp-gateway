"""The filter that stands between backend stderr and a log file full of credentials."""

from __future__ import annotations

import io
import logging

from mcp_gateway.logging_redaction import PLACEHOLDER, RedactingFilter, install_redaction
from mcp_gateway.secrets import SecretStore

SECRET = "ghp_supersecretvalue"


def _capture(store: SecretStore, emit) -> str:
    """Log through a real root handler with the filter installed, and return the output."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        install_redaction(store)
        emit()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
    return buffer.getvalue()


def test_redacts_a_value_passed_as_an_argument() -> None:
    store = SecretStore(_values={"TOKEN": SECRET})
    out = _capture(store, lambda: logging.getLogger("x").info("token is %s", SECRET))
    assert SECRET not in out
    assert PLACEHOLDER in out


def test_redacts_a_value_interpolated_into_the_message_by_the_caller() -> None:
    store = SecretStore(_values={"TOKEN": SECRET})
    out = _capture(store, lambda: logging.getLogger("x").warning(f"auth: {SECRET}"))
    assert SECRET not in out


def test_redacts_a_backends_stderr_passed_through_verbatim() -> None:
    """The case the root-logger placement exists for.

    `MCPStdioClient._drain_stderr` logs a child's stderr through a logger that never saw
    the value and could not know to hide it. Some servers echo the credential they tried
    on a 401.
    """
    store = SecretStore(_values={"TOKEN": SECRET})
    out = _capture(
        store,
        lambda: logging.getLogger("mcp_gateway.mcp_stdio").debug(
            "[github] %s", f"401 unauthorized for token {SECRET}"
        ),
    )
    assert SECRET not in out


def test_longer_values_are_replaced_before_the_shorter_ones_they_contain() -> None:
    """Otherwise `Bearer <token>` leaves `Bearer ***` plus a surviving fragment."""
    store = SecretStore(_values={"A": SECRET, "B": f"Bearer {SECRET}"})
    out = _capture(store, lambda: logging.getLogger("x").info("h: Bearer %s", SECRET))
    assert SECRET not in out
    assert out.count(PLACEHOLDER) == 1


def test_short_values_are_left_alone() -> None:
    """A two-character secret occurs inside ordinary words; scrubbing it mangles everything."""
    store = SecretStore(_values={"TINY": "ab"})
    out = _capture(store, lambda: logging.getLogger("x").info("a cab and a lab"))
    assert out.strip() == "a cab and a lab"


def test_reinstalling_swaps_the_old_value_for_the_new_one() -> None:
    """What a credential rotation needs, in both directions."""
    old, new = "old_secret_value", "new_secret_value"
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        install_redaction(SecretStore(_values={"T": old}))
        install_redaction(SecretStore(_values={"T": new}))
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
        logging.getLogger("x").info("%s then %s", old, new)
    finally:
        root.removeHandler(handler)
    out = buffer.getvalue()
    assert new not in out
    assert old in out  # no longer a secret; a line containing it must stay readable


def test_dict_style_args_are_scrubbed_too() -> None:
    store = SecretStore(_values={"TOKEN": SECRET})
    out = _capture(
        store, lambda: logging.getLogger("x").info("%(k)s", {"k": SECRET})
    )
    assert SECRET not in out


def test_an_empty_store_leaves_records_untouched() -> None:
    out = _capture(SecretStore(), lambda: logging.getLogger("x").info("nothing to hide"))
    assert out.strip() == "nothing to hide"
