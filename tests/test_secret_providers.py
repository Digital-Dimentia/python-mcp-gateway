"""The `secrets:` block: parsing, loading somebody's Python, and chain order.

The load-bearing assertions here are the ones about *order* and about *failure*. A chain
whose order is wrong reads correct and quietly prefers a stale credential; a provider whose
outage is swallowed hands every backend an empty token. Both are pinned below.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from mcp_gateway import secret_providers as sp
from mcp_gateway.config import ConfigError
from mcp_gateway.config import parse as parse_config
from mcp_gateway.secrets import SecretError
from mcp_gateway.transport_ws import ACCESS_KEY_SECRET_NAME

PROVIDER_FILE = textwrap.dedent(
    """
    class Fixed:
        def __init__(self, **values):
            self._values = values

        def load(self, request):
            return dict(self._values)


    class Broken:
        def load(self, request):
            raise RuntimeError("vault is down")


    class SawRequest:
        seen = None

        def __init__(self, **options):
            pass

        def load(self, request):
            SawRequest.seen = request
            return {}


    class NotAProvider:
        pass


    SINGLETON = Fixed(FROM_SINGLETON="s")
    """
)


@pytest.fixture
def providers_py(tmp_path: Path) -> Path:
    path = tmp_path / "providers.py"
    path.write_text(PROVIDER_FILE)
    return path


def _config(tmp_path: Path, block: str, servers: str = "  a: {command: /bin/true}\n"):
    source = tmp_path / "servers.yaml"
    text = f"{block}servers:\n{servers}"
    source.write_text(text)
    return parse_config(text, source=source)


# --- the block ------------------------------------------------------------------------


def test_absent_block_means_the_file_alone(tmp_path: Path) -> None:
    assert _config(tmp_path, "").secret_providers == ()


def test_short_form_and_long_form_parse(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "secrets:\n"
        "  providers:\n"
        '    - "pkg.mod:Attr"\n'
        '    - provider: "./p.py:Other"\n'
        "      options: {mount: kv}\n",
    )
    assert [s.ref for s in config.secret_providers] == ["pkg.mod:Attr", "./p.py:Other"]
    assert config.secret_providers[0].options == {}
    assert config.secret_providers[1].options == {"mount": "kv"}


def test_path_and_module_refs_are_told_apart() -> None:
    assert sp.ProviderSpec(ref="./providers/vault.py:P").is_path
    assert sp.ProviderSpec(ref="/etc/mcp/vault.py:P").is_path
    assert not sp.ProviderSpec(ref="mypkg.vault:P").is_path
    assert sp.ProviderSpec(ref="mypkg.vault:P").target == "mypkg.vault"
    assert sp.ProviderSpec(ref="mypkg.vault:P").attribute == "P"


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("secrets:\n  provdier: x\n", r"unknown key\(s\) \['provdier'\]"),
        ("secrets:\n  providers: {a: b}\n", "must be a list"),
        ("secrets:\n  providers:\n    - noattribute\n", "missing the ':Attribute' half"),
        ("secrets:\n  providers:\n    - 'mod:not-an-identifier'\n", "not a valid Python identifier"),
        ("secrets:\n  providers:\n    - {opts: 1}\n", r"unknown key\(s\) \['opts'\]"),
        ("secrets:\n  providers:\n    - {options: {a: 1}}\n", "'provider' is required"),
        ("secrets:\n  providers:\n    - {provider: 'a:B', options: 3}\n", "must be a mapping"),
        ("secrets: 7\n", "must be a mapping"),
    ],
)
def test_a_malformed_block_is_refused_naming_the_site(
    tmp_path: Path, block: str, expected: str
) -> None:
    with pytest.raises(ConfigError, match=expected):
        _config(tmp_path, block)


def test_interpolation_in_the_block_is_refused_with_its_own_message(tmp_path: Path) -> None:
    """The store does not exist yet, so a `${VAR}` here could only read `os.environ`."""
    with pytest.raises(ConfigError, match=r"contains a \$\{VAR\} reference"):
        _config(tmp_path, "secrets:\n  providers:\n    - '${DIR}/vault.py:P'\n")


def test_parsing_a_catalogue_never_imports_the_provider(tmp_path: Path) -> None:
    """A ref pointing at nothing at all still parses. Import happens in `build_store`."""
    config = _config(tmp_path, "secrets:\n  providers:\n    - 'no.such.module:Nope'\n")
    assert config.secret_providers[0].ref == "no.such.module:Nope"
    with pytest.raises(SecretError, match="cannot import 'no.such.module'"):
        sp.build_store(config, tmp_path / "gateway.env")


# --- loading somebody's Python ----------------------------------------------------------


def test_loads_a_provider_from_a_file(providers_py: Path) -> None:
    spec = sp.ProviderSpec(ref=f"{providers_py}:Fixed", options={"K": "v"})
    provider = sp.load_provider(spec)
    assert provider.load(sp.SecretRequest()) == {"K": "v"}


def test_a_relative_ref_resolves_against_the_catalogue(tmp_path: Path, providers_py: Path) -> None:
    """Not against the daemon's cwd, which is launchd's or a container entrypoint's."""
    config = _config(tmp_path, "secrets:\n  providers:\n    - './providers.py:SINGLETON'\n")
    store = sp.build_store(config, tmp_path / "gateway.env")
    assert store.get("FROM_SINGLETON") == "s"


def test_an_attribute_that_is_already_an_instance_is_taken_as_is(providers_py: Path) -> None:
    provider = sp.load_provider(sp.ProviderSpec(ref=f"{providers_py}:SINGLETON"))
    assert provider.load(sp.SecretRequest()) == {"FROM_SINGLETON": "s"}


def test_a_missing_attribute_names_the_file(providers_py: Path) -> None:
    with pytest.raises(SecretError, match="has no attribute 'Absent'"):
        sp.load_provider(sp.ProviderSpec(ref=f"{providers_py}:Absent"))


def test_an_object_without_load_is_refused(providers_py: Path) -> None:
    with pytest.raises(SecretError, match="has no callable 'load'"):
        sp.load_provider(sp.ProviderSpec(ref=f"{providers_py}:NotAProvider"))


def test_a_missing_provider_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match="no such provider file"):
        sp.load_provider(sp.ProviderSpec(ref=f"{tmp_path / 'gone.py'}:P"))


def test_options_are_passed_as_keyword_arguments(providers_py: Path) -> None:
    spec = sp.ProviderSpec(ref=f"{providers_py}:Broken", options={"unexpected": 1})
    with pytest.raises(SecretError, match="rejected its options"):
        sp.load_provider(spec)


def test_a_file_backed_module_is_cached_across_loads(providers_py: Path) -> None:
    """So a reload does not rebuild a provider holding a pooled connection."""
    spec = sp.ProviderSpec(ref=f"{providers_py}:Fixed")
    first = type(sp.load_provider(spec))
    second = type(sp.load_provider(spec))
    assert first is second


# --- the request ------------------------------------------------------------------------


def test_the_request_carries_every_reference_and_the_access_key(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "",
        servers=(
            "  a:\n"
            "    command: /bin/true\n"
            "    env: {T: '${GH_TOKEN}', U: '$${LITERAL}'}\n"
            "    cwd: '${WORKDIR}/sub'\n"
            "  parked:\n"
            "    command: /bin/true\n"
            "    enabled: false\n"
            "    env: {P: '${PARKED_TOKEN}'}\n"
        ),
    )
    keys = sp.referenced_keys(config)
    assert "GH_TOKEN" in keys
    assert "WORKDIR" in keys
    assert ACCESS_KEY_SECRET_NAME in keys
    # A disabled server's key is included: enabling it must not need a second reload.
    assert "PARKED_TOKEN" in keys
    # `$${NAME}` is an escape, not a reference.
    assert "LITERAL" not in keys


def test_the_provider_sees_the_keys_and_its_own_options(tmp_path: Path, providers_py: Path) -> None:
    config = _config(
        tmp_path,
        "secrets:\n"
        "  providers:\n"
        "    - provider: './providers.py:SawRequest'\n"
        "      options: {mount: kv/mcp}\n",
        servers="  a: {command: /bin/true, env: {T: '${GH_TOKEN}'}}\n",
    )
    sp.build_store(config, tmp_path / "gateway.env")
    seen = sp.load_provider(sp.ProviderSpec(ref=f"{providers_py}:SawRequest")).__class__.seen
    assert seen is not None
    assert "GH_TOKEN" in seen.keys
    assert seen.options == {"mount": "kv/mcp"}
    assert seen.config_path == tmp_path / "servers.yaml"
    assert seen.base_dir == tmp_path


# --- the chain --------------------------------------------------------------------------


def _chain_config(tmp_path: Path, refs: list[str]) -> object:
    block = "secrets:\n  providers:\n" + "".join(f"    - '{ref}'\n" for ref in refs)
    return _config(tmp_path, block)


def test_first_answer_wins_and_the_file_is_last(tmp_path: Path) -> None:
    """The ordering the whole feature turns on: a live value beats a stale local copy."""
    (tmp_path / "providers.py").write_text(
        "class First:\n"
        "    def load(self, request):\n"
        "        return {'SHARED': 'from-first', 'ONLY_FIRST': '1'}\n"
        "\n"
        "class Second:\n"
        "    def load(self, request):\n"
        "        return {'SHARED': 'from-second', 'ONLY_SECOND': '2'}\n"
    )
    env_path = tmp_path / "gateway.env"
    env_path.write_text("SHARED=from-file\nONLY_FILE=3\n")

    config = _chain_config(tmp_path, ["./providers.py:First", "./providers.py:Second"])
    store = sp.build_store(config, env_path)

    assert store.get("SHARED") == "from-first"
    assert store.get("ONLY_FIRST") == "1"
    assert store.get("ONLY_SECOND") == "2"
    assert store.get("ONLY_FILE") == "3"


def test_each_key_reports_where_it_came_from(tmp_path: Path) -> None:
    (tmp_path / "providers.py").write_text(
        "class P:\n    def load(self, request):\n        return {'REMOTE': 'r'}\n"
    )
    env_path = tmp_path / "gateway.env"
    env_path.write_text("LOCAL=l\n")
    store = sp.build_store(_chain_config(tmp_path, ["./providers.py:P"]), env_path)

    assert store.origin("REMOTE") == "./providers.py:P"
    assert store.origin("LOCAL") == sp.FILE_ORIGIN
    assert store.origin("ABSENT") is None


def test_a_store_built_from_the_file_alone_still_names_the_file(tmp_path: Path) -> None:
    """One rule, not two: `build_store` always attributes, so `origin` always answers."""
    env_path = tmp_path / "gateway.env"
    env_path.write_text("LOCAL=l\n")
    store = sp.build_store(_config(tmp_path, ""), env_path)
    assert store.origins == {"LOCAL": sp.FILE_ORIGIN}
    assert store.get("LOCAL") == "l"


def test_a_store_built_by_secrets_load_alone_has_no_origins() -> None:
    """The plain parser is untouched: provenance is a thing the chain adds."""
    from mcp_gateway.secrets import SecretStore

    assert SecretStore(_values={"K": "v"}).origin("K") is None


def test_a_missing_env_file_still_leaves_the_providers(tmp_path: Path) -> None:
    (tmp_path / "providers.py").write_text(
        "class P:\n    def load(self, request):\n        return {'REMOTE': 'r'}\n"
    )
    store = sp.build_store(_chain_config(tmp_path, ["./providers.py:P"]), tmp_path / "absent.env")
    assert store.get("REMOTE") == "r"


# --- failure --------------------------------------------------------------------------


def test_a_provider_that_raises_is_fatal_and_names_itself(tmp_path: Path, providers_py: Path) -> None:
    config = _chain_config(tmp_path, ["./providers.py:Broken"])
    with pytest.raises(SecretError, match="vault is down"):
        sp.build_store(config, tmp_path / "gateway.env")


def test_a_non_string_value_is_refused_rather_than_stored(tmp_path: Path) -> None:
    """Otherwise it surfaces as a TypeError from Popen, three modules from the cause."""
    (tmp_path / "providers.py").write_text(
        "class P:\n    def load(self, request):\n        return {'T': None}\n"
    )
    with pytest.raises(SecretError, match="non-string value for 'T'"):
        sp.build_store(_chain_config(tmp_path, ["./providers.py:P"]), tmp_path / "gateway.env")


def test_a_non_mapping_return_is_refused(tmp_path: Path) -> None:
    (tmp_path / "providers.py").write_text(
        "class P:\n    def load(self, request):\n        return 'nope'\n"
    )
    with pytest.raises(SecretError, match="expected a mapping"):
        sp.build_store(_chain_config(tmp_path, ["./providers.py:P"]), tmp_path / "gateway.env")


def test_a_provider_file_that_fails_to_import_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "providers.py").write_text("import a_module_that_is_not_installed\n")
    with pytest.raises(SecretError, match="failed to import"):
        sp.build_store(_chain_config(tmp_path, ["./providers.py:P"]), tmp_path / "gateway.env")


# --- values still behave like secrets ---------------------------------------------------


def test_provider_values_are_redactable_and_never_repr(tmp_path: Path) -> None:
    """The reason the interface is a snapshot: the filter must see every value up front."""
    (tmp_path / "providers.py").write_text(
        "class P:\n"
        "    def load(self, request):\n"
        "        return {'TOKEN': 'ghp_from_the_provider'}\n"
    )
    store = sp.build_store(_chain_config(tmp_path, ["./providers.py:P"]), tmp_path / "gateway.env")
    assert "ghp_from_the_provider" in store.redactable_values()
    assert "ghp_from_the_provider" not in repr(store)
    assert "TOKEN" in repr(store)


# --- the shipped example ----------------------------------------------------------------


def test_the_example_provider_works_as_documented(tmp_path: Path) -> None:
    document = tmp_path / "secrets.json"
    document.write_text(json.dumps({"GH_TOKEN": "ghp_x", "UNASKED": "y", "NOT_A_STRING": 1}))
    example = Path(__file__).resolve().parents[1] / "examples" / "vault_provider.py"

    source = tmp_path / "servers.yaml"
    text = (
        "secrets:\n"
        "  providers:\n"
        f"    - provider: '{example}:JsonFileProvider'\n"
        f"      options: {{path: '{document}'}}\n"
        "servers:\n"
        "  a: {command: /bin/true, env: {T: '${GH_TOKEN}'}}\n"
    )
    source.write_text(text)
    store = sp.build_store(parse_config(text, source=source), tmp_path / "gateway.env")

    assert store.get("GH_TOKEN") == "ghp_x"
    # Filtered to what the catalogue asked for.
    assert store.get("UNASKED") is None


def test_the_example_provider_raises_when_its_document_is_gone(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "vault_provider.py"
    spec = sp.ProviderSpec(
        ref=f"{example}:JsonFileProvider", options={"path": str(tmp_path / "gone.json")}
    )
    source = tmp_path / "servers.yaml"
    text = (
        "secrets:\n"
        "  providers:\n"
        f"    - provider: '{example}:JsonFileProvider'\n"
        f"      options: {{path: '{tmp_path / 'gone.json'}'}}\n"
        "servers:\n  a: {command: /bin/true}\n"
    )
    source.write_text(text)
    with pytest.raises(SecretError):
        sp.build_store(parse_config(text, source=source), tmp_path / "gateway.env")

    # ...unless it was declared optional, which is the documented escape hatch.
    optional = sp.load_provider(
        sp.ProviderSpec(ref=spec.ref, options={**spec.options, "required": False})
    )
    assert optional.load(sp.SecretRequest()) == {}


# --- the running daemon -----------------------------------------------------------------


async def test_a_reload_re_asks_the_provider(tmp_path: Path) -> None:
    """A rotation in a remote store lands on reload, exactly as one in the file does.

    This is what the `to_thread` call in `Gateway.reload` is for, and the reason a provider
    is re-asked rather than cached: rotating a credential and reloading is the single most
    common reason anyone reloads at all.
    """
    from tests.fixtures.ws_client import daemon

    rotating = tmp_path / "rotating.py"
    counter = tmp_path / "count"
    counter.write_text("0")
    rotating.write_text(
        "from pathlib import Path\n"
        f"COUNTER = Path({str(counter)!r})\n"
        "\n"
        "class Rotating:\n"
        "    def load(self, request):\n"
        "        n = int(COUNTER.read_text()) + 1\n"
        "        COUNTER.write_text(str(n))\n"
        "        return {'ROTATED': f'value-{n}'}\n"
    )
    servers = (
        "secrets:\n"
        "  providers:\n"
        "    - './rotating.py:Rotating'\n"
        "servers:\n"
        "  alpha:\n"
        "    command: /bin/echo\n"
        "    enabled: false\n"
        "    env: {T: '${ROTATED}'}\n"
    )
    harness = await daemon(tmp_path, servers=servers, env="LOCAL=l\n")
    try:
        assert harness.gateway.store.get("ROTATED") == "value-1"
        assert harness.gateway.store.origin("ROTATED") == "./rotating.py:Rotating"
        assert harness.gateway.store.get("LOCAL") == "l"

        await harness.gateway.reload()

        assert harness.gateway.store.get("ROTATED") == "value-2"
        # The file is still the last link after a reload, not dropped by it.
        assert harness.gateway.store.get("LOCAL") == "l"
    finally:
        await harness.close()


async def test_a_reload_whose_provider_fails_changes_nothing(tmp_path: Path) -> None:
    """The load-then-replace guarantee, extended over third-party code."""
    from tests.fixtures.ws_client import daemon

    provider = tmp_path / "flaky.py"
    provider.write_text(
        "from pathlib import Path\n"
        f"FLAG = Path({str(tmp_path / 'fail')!r})\n"
        "\n"
        "class Flaky:\n"
        "    def load(self, request):\n"
        "        if FLAG.exists():\n"
        "            raise RuntimeError('vault is down')\n"
        "        return {'REMOTE': 'good'}\n"
    )
    servers = (
        "secrets:\n  providers:\n    - './flaky.py:Flaky'\n"
        "servers:\n  alpha: {command: /bin/echo, enabled: false}\n"
    )
    harness = await daemon(tmp_path, servers=servers, env="LOCAL=l\n")
    try:
        assert harness.gateway.store.get("REMOTE") == "good"
        before = harness.gateway.store

        (tmp_path / "fail").write_text("")
        result = await harness.gateway.reload()

        assert "vault is down" in result["content"][0]["text"]
        assert harness.gateway.store is before, "a refused reload must replace nothing"
        assert harness.gateway.store.get("REMOTE") == "good"
    finally:
        await harness.close()
