"""The zoo's cascading vocabularies, driven through a real gateway.

`examples/zoo_server.py` cannot be imported -- its module scope ends in the serve loop --
so every assertion here goes over the wire, which is the right place for them anyway: what
matters is that a *client* can walk continent -> country -> animal using nothing but the
bodies the server hands it, through the gateway's URI rewriting rather than around it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp_gateway.naming import decode_resource_uri, encode_resource_uri
from tests.fixtures.ws_client import daemon

ZOO = Path(__file__).resolve().parents[1] / "examples" / "zoo_server.py"
PY = sys.executable

# Mirrors `servers.dev.yaml`: the schema-zoo flag is what publishes any of this, and a
# curated environment means PATH has to be named for `python3` itself to resolve.
SERVERS = f"""
servers:
  zoo:
    command: {PY}
    args: ["{ZOO}"]
    env_mode: curated
    env:
      MOCK_MCP_SCHEMA_ZOO: "1"
    env_passthrough:
      - "PATH"
"""

ANIMAL_TEMPLATE = "zoo://animals/{id}"
COUNTRIES_TEMPLATE = "zoo://continents/{continent}/countries"
COUNTRY_ANIMALS_TEMPLATE = "zoo://continents/{continent}/countries/{country}/animals"


def public(uri: str) -> str:
    """A backend URI in the gateway's address space, the way the UI mints one."""
    return encode_resource_uri("zoo", uri)


async def read_json(client, uri: str) -> dict:
    """Read one zoo resource by its *backend* URI and parse the body."""
    result = await client.call("resources/read", {"uri": public(uri)})
    return json.loads(result["contents"][0]["text"])


async def error_for(client, uri: str) -> dict:
    response = await client.request("resources/read", {"uri": public(uri)})
    assert "error" in response, response
    return response["error"]


async def test_the_cascades_listings_are_published(tmp_path) -> None:
    """The head is a resource; the two that take a parameter are templates.

    The braces are the thing to watch: `encode_resource_uri` percent-encodes with
    `safe="{}"` precisely so a client can still expand what it is given, and a *two*-variable
    template is the case that had never been published before this.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        resources = (await client.call("resources/list"))["resources"]
        assert public("zoo://continents") in [r["uri"] for r in resources]

        templates = (await client.call("resources/templates/list"))["resourceTemplates"]
        expressions = [decode_resource_uri(t["uriTemplate"])[1] for t in templates]
        assert COUNTRIES_TEMPLATE in expressions
        assert COUNTRY_ANIMALS_TEMPLATE in expressions
        for template in templates:
            assert template["uriTemplate"].startswith("mcpgw://")
        # Both variables survive namespacing, unescaped, in the one template that has two.
        # Everything around them is percent-encoded -- `quote` escapes the separators too --
        # so the braces standing out untouched is precisely the `safe="{}"` guarantee.
        two = [
            t["uriTemplate"]
            for t in templates
            if decode_resource_uri(t["uriTemplate"])[1] == COUNTRY_ANIMALS_TEMPLATE
        ]
        assert len(two) == 1, templates
        assert "{continent}" in two[0]
        assert "{country}" in two[0]
    finally:
        await harness.close()


async def test_each_body_names_what_one_of_its_values_buys(tmp_path) -> None:
    """`narrows` for a listing, `readOne` for a member -- the whole discovery contract."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        continents = await read_json(client, "zoo://continents")
        assert continents["narrows"] == COUNTRIES_TEMPLATE
        assert len(continents["enum"]) == len(continents["enumNames"])
        assert "readOne" not in continents

        countries = await read_json(client, "zoo://continents/africa/countries")
        assert countries["narrows"] == COUNTRY_ANIMALS_TEMPLATE
        assert len(countries["enum"]) == len(countries["enumNames"])

        animals = await read_json(
            client, "zoo://continents/africa/countries/congo/animals"
        )
        # The leaf spends its values on the template the zoo already had, which is what
        # lets the existing detail read and its fan-out work unchanged.
        assert animals["readOne"] == ANIMAL_TEMPLATE
        assert "narrows" not in animals
        assert len(animals["enum"]) == len(animals["enumNames"])
    finally:
        await harness.close()


async def test_walking_the_whole_cascade_reaches_a_readable_animal_every_time(
    tmp_path,
) -> None:
    """The assertion the data exists for.

    Every continent has countries, every country has animals, and every animal a leaf names
    can actually be read. A country with no animals, an animal filed under a country that
    does not exist, and any drift between an id and its label are all one failure here.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        continents = await read_json(client, "zoo://continents")
        assert continents["enum"]

        seen = set()
        for continent in continents["enum"]:
            countries = await read_json(
                client, continents["narrows"].replace("{continent}", continent)
            )
            assert countries["enum"], continent
            for country in countries["enum"]:
                leaf_uri = (
                    countries["narrows"]
                    .replace("{continent}", continent)
                    .replace("{country}", country)
                )
                leaf = await read_json(client, leaf_uri)
                assert leaf["enum"], leaf_uri
                for animal_id in leaf["enum"]:
                    animal = await read_json(client, "zoo://animals/" + animal_id)
                    assert animal["id"] == animal_id
                    # The record agrees with the path that led to it, in both directions.
                    assert country in animal["countries"], (animal_id, country)
                    assert continent in animal["continents"], (animal_id, continent)
                    seen.add(animal_id)

        flat = await read_json(client, "zoo://animals")
        assert seen == set(flat["enum"])
    finally:
        await harness.close()


async def test_an_animal_may_belong_to_two_countries(tmp_path) -> None:
    """`red-panda` is in both, so a client that assumes one parent per child is caught."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        china = await read_json(
            client, "zoo://continents/asia/countries/china/animals"
        )
        nepal = await read_json(
            client, "zoo://continents/asia/countries/nepal/animals"
        )
        assert "red-panda" in china["enum"]
        assert "red-panda" in nepal["enum"]
    finally:
        await harness.close()


async def test_a_uri_the_server_never_handed_over_is_a_miss(tmp_path) -> None:
    """Including the one that looks plausible: a real country, under the wrong continent.

    That is exactly what a client picking from two listings *out of step* sends, and an
    empty listing would read as a country with no animals rather than as a mistake.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        for uri in (
            "zoo://continents/atlantis/countries",
            "zoo://continents/africa/countries/narnia/animals",
            "zoo://continents/asia/countries/brazil/animals",
            "zoo://continents/africa/bogus",
        ):
            error = await error_for(client, uri)
            assert error["code"] == -32002, (uri, error)
    finally:
        await harness.close()


async def test_the_flat_vocabulary_is_not_regressed(tmp_path) -> None:
    """The cascade is an addition. `zoo://animals` still publishes every animal at once."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        flat = await read_json(client, "zoo://animals")
        assert flat["readOne"] == ANIMAL_TEMPLATE
        assert len(flat["enum"]) == len(flat["enumNames"])
        assert "red-panda" in flat["enum"]
    finally:
        await harness.close()
