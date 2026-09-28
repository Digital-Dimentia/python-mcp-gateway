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


def test_a_wildcard_path_variable_survives() -> None:
    """The regression: a backend publishing `git://repositories/{repo*}`.

    `quote(uri, safe="{}")` spared the braces but not the explode modifier, so the client was
    handed `{repo%2A}` -- a variable that does not exist. The URI still decoded, so nothing
    looked wrong from this side; what broke was the client's expansion of a multi-segment
    path, and only against the gateway, never against the backend directly.
    """
    encoded = naming.encode_resource_uri("git", "git://repositories/{repo*}")
    assert "{repo*}" in encoded
    assert "%2A" not in encoded
    assert naming.decode_resource_uri(encoded) == ("git", "git://repositories/{repo*}")


@pytest.mark.parametrize(
    "expression",
    [
        "{path}",  # the one form the old `safe="{}"` happened to get right
        "{repo*}",  # explode modifier
        "{var:3}",  # prefix modifier
        "{owner,repo}",  # more than one variable
        "{+path}",  # reserved expansion -- the usual spelling for a multi-segment path
        "{#frag}",
        "{/a,b}",
        "{;k}",
        "{?q,limit}",
        "{&y}",
        "{.v}",
    ],
)
def test_uri_templates_keep_their_rfc6570_expressions(expression: str) -> None:
    """A `uriTemplate` is expanded client-side, so the expression must arrive byte-identical.

    Every operator and modifier, not just the braces: the client reads the variable name out
    of what we send it, and a percent-encoded modifier names nothing it can bind.
    """
    uri = f"file:///{expression}/rest"
    encoded = naming.encode_resource_uri("fs", uri)
    assert expression in encoded
    assert naming.decode_resource_uri(encoded) == ("fs", uri)


def test_only_the_expressions_are_spared() -> None:
    """The counterpart: outside `{...}` nothing is safe, so a template stays one segment.

    A literal `?` or `#` in a *concrete* URI must still be encoded, or `resources/read` would
    address a different resource than the backend named. A stray brace is not an expression
    and is encoded like anything else.
    """
    encoded = naming.encode_resource_uri("fs", "ui://panel?mode=wide#top")
    assert "?" not in encoded.removeprefix("mcpgw://fs/")
    assert "#" not in encoded
    assert naming.decode_resource_uri(encoded) == ("fs", "ui://panel?mode=wide#top")

    stray = naming.encode_resource_uri("fs", "weird://{unclosed/x")
    assert "{" not in stray
    assert naming.decode_resource_uri(stray) == ("fs", "weird://{unclosed/x")


def test_decode_keeps_what_a_client_made_of_a_query_operator() -> None:
    """`{?q}` is published verbatim, so an expanded read arrives carrying a real `?`.

    The gateway's own resource URIs have no query of their own, so everything after the
    authority is the backend's. Reading `urlsplit(...).path` would truncate it here.
    """
    assert naming.decode_resource_uri("mcpgw://fs/x%3A%2F%2Fs?q=hi") == ("fs", "x://s?q=hi")


@pytest.mark.parametrize(
    "bad", ["file:///x", "mcpgw://", "mcpgw:///x", "mcpgw://srv", "mcpgw:x", "not a uri"]
)
def test_decode_refuses_uris_that_are_not_ours(bad: str) -> None:
    with pytest.raises(naming.NamingError):
        naming.decode_resource_uri(bad)
