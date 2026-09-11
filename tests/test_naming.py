"""The two rules that make routing a pure string split, asserted together."""

from __future__ import annotations

import pytest

from mcp_gateway import naming


@pytest.mark.parametrize("name", ["github", "fs", "a", "my-server", "srv_1", "A9"])
def test_accepts_plausible_server_names(name: str) -> None:
    naming.validate_server_name(name)


@pytest.mark.parametrize(
    ("name", "because"),
    [
        ("", "empty"),
        ("a" * 33, "too long"),
        ("a__b", "contains the separator"),
        ("__x", "contains the separator"),
        ("a/b", "would break the URI authority"),
        ("a:b", "would read as a port"),
        ("-lead", "must start with a letter or digit"),
        ("has space", "not an identifier"),
        ("gateway", "reserved for the meta-tools"),
    ],
)
def test_refuses_unusable_server_names(name: str, because: str) -> None:
    with pytest.raises(naming.NamingError):
        naming.validate_server_name(name)


def test_round_trip_survives_a_separator_inside_the_tool_name() -> None:
    """The coupling in one assertion.

    `create__issue` is a legal MCP tool name. It needs no escaping *because* no server name
    may contain `__`, which makes the first occurrence always the separator. Break
    `validate_server_name` and this silently starts routing to a backend called `github`
    that does not exist.
    """
    public = naming.compose("github", "create__issue")
    assert public == "github__create__issue"
    assert naming.split(public) == ("github", "create__issue")


def test_split_refuses_a_name_that_is_not_namespaced() -> None:
    for bad in ["plain", "__x", "x__", ""]:
        with pytest.raises(naming.NamingError):
            naming.split(bad)


def test_publishable_rejects_names_real_clients_reject() -> None:
    assert naming.is_publishable("github__create_issue")
    assert not naming.is_publishable("github__" + "x" * 60)  # over 64 chars
    assert not naming.is_publishable("github__has space")
    assert not naming.is_publishable("github__dots.are.out")


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/hosts",
        "file:///a b/c?d=e#f",
        "https://example.com/x%20y",
        "custom+scheme://host/path",
        "file:///README.md",
    ],
)
def test_resource_uris_round_trip(uri: str) -> None:
    encoded = naming.encode_resource_uri("fs", uri)
    assert encoded.startswith("mcpgw://fs/")
    assert naming.decode_resource_uri(encoded) == ("fs", uri)


def test_two_backends_publishing_the_same_uri_get_distinct_addresses() -> None:
    """The reason resource URIs are rewritten at all.

    Two filesystem servers rooted differently both publish `file:///README.md`. Passing the
    original through and searching for it at read time would let dict ordering decide which
    one answers.
    """
    one = naming.encode_resource_uri("docs", "file:///README.md")
    two = naming.encode_resource_uri("code", "file:///README.md")
    assert one != two
    assert naming.decode_resource_uri(one)[0] == "docs"
    assert naming.decode_resource_uri(two)[0] == "code"


def test_uri_templates_keep_their_rfc6570_expressions() -> None:
    """`safe="{}"` is what lets a client still expand a forwarded `uriTemplate`."""
    encoded = naming.encode_resource_uri("fs", "file:///{path}")
    assert "{path}" in encoded
    assert naming.decode_resource_uri(encoded) == ("fs", "file:///{path}")


@pytest.mark.parametrize("bad", ["file:///x", "mcpgw://", "mcpgw:///x", "not a uri"])
def test_decode_refuses_uris_that_are_not_ours(bad: str) -> None:
    with pytest.raises(naming.NamingError):
        naming.decode_resource_uri(bad)
