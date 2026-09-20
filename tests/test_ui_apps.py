"""`ui_apps.py`: the `_meta.ui` rewrite, and the shapes it refuses.

The load-bearing test in this file is
`test_the_ui_authority_is_never_matched_against_a_server_name`. Everything else is a
validation rule; that one is the reason the module is written the way it is, and it fails
the moment somebody "fixes" the rewrite to resolve `ui://zoo/...` against the backend named
`zoo`. See `ui_apps.md`.
"""

from __future__ import annotations

import pytest

from mcp_gateway import naming, protocol, ui_apps


def ref(uri, *, flat=False, extra=None):
    """A tool `_meta` carrying `uri`, in whichever spelling."""
    meta: dict = {}
    if flat:
        meta[ui_apps.FLAT_RESOURCE_URI_KEY] = uri
    else:
        meta["ui"] = {"resourceUri": uri}
    if extra:
        meta.update(extra)
    return meta


def rewritten(meta, server="zoo"):
    new, _ = ui_apps.rewrite_tool_meta(meta, server)
    return new


def uri_of(meta, server="zoo"):
    new = rewritten(meta, server)
    return None if new is None else new.get("ui", {}).get("resourceUri")


# --- the rewrite ------------------------------------------------------------------


def test_a_nested_resource_uri_is_rewritten_into_the_gateways_address_space():
    assert uri_of(ref("ui://zoo/panel")) == naming.encode_resource_uri("zoo", "ui://zoo/panel")


def test_a_rewritten_reference_decodes_back_to_the_backends_own_uri():
    public = uri_of(ref("ui://zoo/panel"))
    assert naming.decode_resource_uri(public) == ("zoo", "ui://zoo/panel")


def test_a_ui_uri_with_rfc6570_braces_survives_as_a_template():
    public = uri_of(ref("ui://zoo/panel/{id}"))
    assert "{id}" in public
    assert naming.decode_resource_uri(public) == ("zoo", "ui://zoo/panel/{id}")


def test_a_ui_uri_containing_a_question_mark_round_trips():
    # `decode_resource_uri` refuses a public URI carrying `?`, so this only works because
    # `encode_resource_uri` percent-encodes it. Pinned because a later `safe=` change would
    # break it silently.
    public = uri_of(ref("ui://zoo/panel?mode=wide"))
    assert naming.decode_resource_uri(public) == ("zoo", "ui://zoo/panel?mode=wide")


def test_meta_without_a_ui_block_is_returned_unchanged():
    assert ui_apps.rewrite_tool_meta({"progressToken": 7}, "zoo") == (None, None)


def test_a_non_dict_meta_is_returned_unchanged():
    assert ui_apps.rewrite_tool_meta(None, "zoo") == (None, None)
    assert ui_apps.rewrite_tool_meta("nonsense", "zoo") == (None, None)


def test_keys_outside_meta_ui_are_never_touched():
    new = rewritten(ref("ui://zoo/panel", extra={"progressToken": 7, "vendor/x": {"a": 1}}))
    assert new["progressToken"] == 7
    assert new["vendor/x"] == {"a": 1}


def test_the_reference_is_reported_so_the_caller_can_check_publication():
    _, raw = ui_apps.rewrite_tool_meta(ref("ui://zoo/panel"), "zoo")
    assert raw == "ui://zoo/panel"


# --- the security property --------------------------------------------------------


def test_the_ui_authority_is_never_matched_against_a_server_name():
    """The whole argument of the module, in one assertion.

    `evil` publishes a tool pointing at `ui://zoo/panel`. The rewrite must address it to
    *evil*, so a read reaches evil and never zoo.
    """
    public = uri_of(ref("ui://zoo/panel"), server="evil")
    server, original = naming.decode_resource_uri(public)
    assert server == "evil"
    assert original == "ui://zoo/panel"


def test_a_cross_backend_reference_resolves_to_the_referrer_not_the_referent():
    honest = uri_of(ref("ui://zoo/panel"), server="zoo")
    forged = uri_of(ref("ui://zoo/panel"), server="evil")
    assert honest != forged
    assert naming.decode_resource_uri(forged)[0] == "evil"


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/panel",
        "http://evil.example/panel",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "panel",  # no scheme at all
    ],
)
def test_a_resource_uri_outside_the_ui_scheme_is_stripped(uri):
    new = rewritten(ref(uri))
    assert "resourceUri" not in new.get("ui", {})


def test_a_non_string_resource_uri_is_stripped():
    assert "resourceUri" not in rewritten({"ui": {"resourceUri": 42}}).get("ui", {})


def test_an_over_long_resource_uri_is_stripped():
    long = "ui://zoo/" + "a" * ui_apps.MAX_RESOURCE_URI
    assert "resourceUri" not in rewritten(ref(long)).get("ui", {})


@pytest.mark.parametrize("bad", ["ui://zoo/pa nel", "ui://zoo/pa\nnel", "ui://zoo/pa\x00nel"])
def test_a_resource_uri_with_whitespace_or_a_control_character_is_stripped(bad):
    assert "resourceUri" not in rewritten(ref(bad)).get("ui", {})


def test_a_refused_reference_leaves_the_rest_of_the_tool_intact():
    new = rewritten(ref("https://evil.example/x", extra={"progressToken": 7}))
    assert new["progressToken"] == 7


def test_a_refused_reference_keeps_the_other_ui_members():
    new = rewritten({"ui": {"resourceUri": "javascript:alert(1)", "prefersBorder": True}})
    assert new["ui"] == {"prefersBorder": True}


def test_a_refused_reference_drops_the_ui_block_when_nothing_else_is_in_it():
    assert "ui" not in rewritten(ref("javascript:alert(1)"))


def test_a_refused_flat_reference_is_dropped_too():
    assert ui_apps.FLAT_RESOURCE_URI_KEY not in rewritten(ref("javascript:alert(1)", flat=True))


# --- the deprecated flat spelling -------------------------------------------------


def test_the_deprecated_flat_key_is_rewritten_too():
    new = rewritten(ref("ui://zoo/panel", flat=True))
    expected = naming.encode_resource_uri("zoo", "ui://zoo/panel")
    assert new[ui_apps.FLAT_RESOURCE_URI_KEY] == expected


def test_a_backend_sending_only_the_flat_key_also_gets_the_nested_form():
    new = rewritten(ref("ui://zoo/panel", flat=True))
    assert new["ui"]["resourceUri"] == new[ui_apps.FLAT_RESOURCE_URI_KEY]


def test_the_gateway_does_not_invent_the_flat_key_for_a_backend_that_did_not_send_it():
    assert ui_apps.FLAT_RESOURCE_URI_KEY not in rewritten(ref("ui://zoo/panel"))


def test_nested_wins_when_the_two_spellings_disagree_and_the_flat_one_is_made_to_agree():
    meta = {
        "ui": {"resourceUri": "ui://zoo/nested"},
        ui_apps.FLAT_RESOURCE_URI_KEY: "ui://zoo/flat",
    }
    new = rewritten(meta)
    assert naming.decode_resource_uri(new["ui"]["resourceUri"])[1] == "ui://zoo/nested"
    assert new[ui_apps.FLAT_RESOURCE_URI_KEY] == new["ui"]["resourceUri"]


# --- no mutation ------------------------------------------------------------------


def test_the_callers_meta_is_never_mutated():
    meta = ref("ui://zoo/panel")
    before = meta["ui"]["resourceUri"]
    ui_apps.rewrite_tool_meta(meta, "zoo")
    assert meta["ui"]["resourceUri"] == before


def test_rewriting_twice_does_not_double_encode():
    """A cached `_meta` rewritten on every `tools/list` must not drift."""
    meta = ref("ui://zoo/panel")
    once = rewritten(meta)
    twice = rewritten(meta)
    assert once == twice


def test_the_nested_ui_dict_is_rebuilt_not_shared():
    meta = ref("ui://zoo/panel")
    new = rewritten(meta)
    assert new["ui"] is not meta["ui"]


# --- csp: injection, not permissiveness -------------------------------------------


def block(csp):
    return {"ui": {"csp": csp}}


def csp_of(meta):
    return ui_apps.sanitize_ui_block(meta["ui"])["csp"]


def test_a_wellformed_csp_domain_survives():
    assert csp_of(block({"connectDomains": ["https://api.example.com"]})) == {
        "connectDomains": ["https://api.example.com"]
    }


def test_a_wildcard_csp_entry_survives_because_permissiveness_is_not_injection():
    assert csp_of(block({"connectDomains": ["*"]})) == {"connectDomains": ["*"]}


def test_a_wildcard_subdomain_survives():
    assert csp_of(block({"resourceDomains": ["*.example.com"]}))["resourceDomains"] == [
        "*.example.com"
    ]


@pytest.mark.parametrize(
    "entry",
    [
        "example.com; script-src *",
        "example.com 'unsafe-inline'",
        "'unsafe-inline'",
        'example.com"',
        "example.com/path",
        "example.com\nscript-src *",
        "example.com,evil.com",
    ],
)
def test_a_csp_entry_that_could_become_a_second_directive_is_dropped(entry):
    assert csp_of(block({"connectDomains": [entry]}))["connectDomains"] == []


def test_a_csp_list_is_clamped_to_its_ceiling():
    many = [f"host{n}.example.com" for n in range(ui_apps.MAX_CSP_ENTRIES + 20)]
    kept = csp_of(block({"connectDomains": many}))["connectDomains"]
    assert len(kept) == ui_apps.MAX_CSP_ENTRIES


def test_an_over_long_csp_entry_is_dropped():
    assert csp_of(block({"connectDomains": ["a" * 300 + ".example.com"]}))["connectDomains"] == []


def test_a_non_list_csp_directive_is_dropped():
    assert "connectDomains" not in csp_of(block({"connectDomains": "example.com"}))


def test_a_non_dict_csp_is_dropped_entirely():
    assert "csp" not in ui_apps.sanitize_ui_block({"csp": "everything"})


def test_an_unknown_csp_directive_is_held_to_the_same_shape():
    """Whatever it is called, it is bound for the same header."""
    cleaned = csp_of(block({"futureDomains": ["ok.example.com", "bad.example.com; x y"]}))
    assert cleaned["futureDomains"] == ["ok.example.com"]


# --- permissions ------------------------------------------------------------------


def test_a_boolean_permission_survives():
    assert ui_apps.sanitize_ui_block({"permissions": {"camera": True}})["permissions"] == {
        "camera": True
    }


def test_a_string_valued_permission_is_dropped_because_it_is_truthy():
    """`if perms.get("camera")` reads `"false"` as true: a real grant from a fake denial."""
    cleaned = ui_apps.sanitize_ui_block({"permissions": {"camera": "false"}})
    assert cleaned["permissions"] == {}


def test_an_unrecognised_permission_key_with_a_bool_survives():
    cleaned = ui_apps.sanitize_ui_block({"permissions": {"notifications": True}})
    assert cleaned["permissions"] == {"notifications": True}


def test_a_non_dict_permissions_block_is_dropped():
    assert "permissions" not in ui_apps.sanitize_ui_block({"permissions": ["camera"]})


# --- the remaining ui members -----------------------------------------------------


def test_a_domain_with_a_path_is_dropped():
    assert "domain" not in ui_apps.sanitize_ui_block({"domain": "app.example.com/x"})


def test_a_wellformed_domain_survives():
    assert ui_apps.sanitize_ui_block({"domain": "app.example.com"})["domain"] == "app.example.com"


def test_prefers_border_must_be_a_bool():
    assert "prefersBorder" not in ui_apps.sanitize_ui_block({"prefersBorder": "yes"})
    assert ui_apps.sanitize_ui_block({"prefersBorder": True})["prefersBorder"] is True


def test_a_malformed_visibility_is_dropped_but_unknown_members_survive():
    assert "visibility" not in ui_apps.sanitize_ui_block({"visibility": "model"})
    assert ui_apps.sanitize_ui_block({"visibility": ["model", "future"]})["visibility"] == [
        "model",
        "future",
    ]


def test_unknown_keys_under_meta_ui_survive_untouched():
    cleaned = ui_apps.sanitize_ui_block({"somethingNew": {"deep": [1, 2]}})
    assert cleaned["somethingNew"] == {"deep": [1, 2]}


def test_sanitize_resource_meta_reports_no_change_when_there_is_none():
    assert ui_apps.sanitize_resource_meta({"ui": {"prefersBorder": True}}) is None
    assert ui_apps.sanitize_resource_meta({"other": 1}) is None


def test_sanitize_resource_meta_cleans_only_the_ui_member():
    meta = {"ui": {"permissions": {"camera": "false"}}, "other": 1}
    cleaned = ui_apps.sanitize_resource_meta(meta)
    assert cleaned["ui"]["permissions"] == {}
    assert cleaned["other"] == 1
    assert meta["ui"]["permissions"] == {"camera": "false"}


# --- profile parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    "mime,expected",
    [
        (protocol.UI_APP_DECLARATIVE_MIME, "mcp-app-declarative"),
        (protocol.UI_APP_HTML_MIME, "mcp-app"),
        ("text/html; profile=mcp-app", "mcp-app"),
        ('text/html;PROFILE="mcp-app"', "mcp-app"),
        ("text/html", None),
        ("text/html;charset=utf-8", None),
        (None, None),
        (42, None),
    ],
)
def test_profile_of(mime, expected):
    assert ui_apps.profile_of(mime) == expected


def test_reference_of_reports_which_spelling_carried_it():
    assert ui_apps.reference_of(ref("ui://zoo/p")) == ("ui://zoo/p", False)
    assert ui_apps.reference_of(ref("ui://zoo/p", flat=True)) == ("ui://zoo/p", True)
    assert ui_apps.reference_of({"other": 1}) == (None, False)
