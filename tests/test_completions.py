"""`completion/complete`: the one method whose whole job is to be asked mid-typing.

The cases worth pinning are about *fidelity*. A completion is a hint, so nothing here may
turn one into a failed call -- and the values are argument values in the backend's own
vocabulary, so nothing here may rewrite them on the way back.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_gateway import errors
from mcp_gateway.naming import encode_resource_uri
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
ZOO = Path(__file__).resolve().parents[1] / "examples" / "zoo_server.py"
PY = sys.executable

SERVERS = f"""
servers:
  alpha:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "alpha"
      MOCK_PROMPTS: "greet"
      MOCK_TEMPLATES: "file:///docs/{{path}}"
      MOCK_COMPLETIONS: "1"
  old:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "old"
      MOCK_PROMPTS: "greet"
      MOCK_COMPLETIONS: "1"
      MOCK_PROTOCOL_VERSION: "2024-11-05"
  silent:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "silent"
      MOCK_PROMPTS: "greet"
  broken:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "broken"
      MOCK_PROMPTS: "greet"
      MOCK_COMPLETIONS_ERROR: "1"
  many:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "many"
      MOCK_PROMPTS: "greet"
      MOCK_COMPLETIONS_MANY: "1"
"""

ZOO_SERVERS = f"""
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


def prompt_ref(name: str) -> dict:
    return {"type": "ref/prompt", "name": name}


async def complete(client, ref: dict, name: str, value: str = "", context: dict | None = None):
    params: dict = {"ref": ref, "argument": {"name": name, "value": value}}
    if context is not None:
        params["context"] = {"arguments": context}
    return await client.call("completion/complete", params)


async def test_the_capability_is_advertised(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        result = await client.initialize()
        assert result["capabilities"]["completions"] == {}
    finally:
        await harness.close()


async def test_a_prompt_ref_reaches_its_backend_unnamespaced(tmp_path) -> None:
    """The namespace is the routing key and is spent doing the routing."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        values = (await complete(client, prompt_ref("alpha__greet"), "who", "a"))["completion"]["values"]
        assert "named:greet" in values
        assert "ref:ref/prompt" in values
        assert "arg:who=a" in values
    finally:
        await harness.close()


async def test_a_resource_ref_is_decoded_with_its_braces_intact(tmp_path) -> None:
    """A completion ref carries the *template*, so the expression has to survive the trip."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        ref = {"type": "ref/resource", "uri": encode_resource_uri("alpha", "file:///docs/{path}")}
        values = (await complete(client, ref, "path"))["completion"]["values"]
        assert "named:file:///docs/{path}" in values
    finally:
        await harness.close()


async def test_context_arguments_are_forwarded(tmp_path) -> None:
    """The cascade, at the protocol level: what is already filled in narrows what is next."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        result = await complete(
            client, prompt_ref("alpha__greet"), "country", "", {"continent": "africa"}
        )
        assert "ctx:continent=africa" in result["completion"]["values"]
    finally:
        await harness.close()


async def test_a_2024_backend_is_sent_no_context_and_still_answers(tmp_path) -> None:
    """`context` postdates that revision. Sending it risks a `-32602` over a *hint*."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        result = await complete(
            client, prompt_ref("old__greet"), "country", "", {"continent": "africa"}
        )
        values = result["completion"]["values"]
        assert not [v for v in values if v.startswith("ctx:")]
        assert "named:greet" in values
    finally:
        await harness.close()


async def test_a_backend_with_no_completions_answers_empty_rather_than_failing(tmp_path) -> None:
    """An empty list is what a server with nothing to suggest returns. A `-32601` here would
    be the gateway denying a method it advertises."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        result = await complete(client, prompt_ref("silent__greet"), "who")
        assert result["completion"]["values"] == []
    finally:
        await harness.close()


async def test_an_empty_gateway_answers_a_completion_rather_than_refusing(tmp_path) -> None:
    """The proof that advertising unconditionally strands nobody."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        response = await client.request(
            "completion/complete",
            {"ref": prompt_ref("nobody__greet"), "argument": {"name": "who", "value": ""}},
        )
        # Nothing to route to is a caller mistake, not a missing method.
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()


async def test_values_and_their_counts_pass_through_untouched(tmp_path) -> None:
    """`values` are argument values in the backend's vocabulary, never ours to rewrite."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        completion = (await complete(client, prompt_ref("many__greet"), "who"))["completion"]
        assert len(completion["values"]) == 100
        assert completion["total"] == 120
        assert completion["hasMore"] is True
        assert completion["values"][0] == "many-000"
    finally:
        await harness.close()


async def test_a_backends_own_error_is_forwarded_and_attributed(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request(
            "completion/complete",
            {"ref": prompt_ref("broken__greet"), "argument": {"name": "who", "value": ""}},
        )
        assert response["error"]["code"] == errors.METHOD_NOT_FOUND
        assert response["error"]["data"]["source"] == "mcp"
        assert response["error"]["data"]["backend"] == "broken"
    finally:
        await harness.close()


async def test_an_unknown_name_or_uri_is_told_apart_from_a_broken_one(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        unknown_prompt = await client.request(
            "completion/complete",
            {"ref": prompt_ref("nosuch__greet"), "argument": {"name": "who", "value": ""}},
        )
        assert unknown_prompt["error"]["code"] == errors.INVALID_PARAMS

        unknown_resource = await client.request(
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "mcpgw://nosuch/file%3A%2F%2F%2Fx"},
                "argument": {"name": "path", "value": ""},
            },
        )
        assert unknown_resource["error"]["code"] == errors.RESOURCE_NOT_FOUND

        bad_type = await client.request(
            "completion/complete",
            {"ref": {"type": "ref/nonsense"}, "argument": {"name": "who", "value": ""}},
        )
        assert bad_type["error"]["code"] == errors.INVALID_PARAMS
        # Ours, not a backend's: nothing was ever asked.
        assert "source" not in bad_type["error"].get("data", {})
    finally:
        await harness.close()


async def test_a_missing_argument_is_refused_but_an_empty_value_is_not(tmp_path) -> None:
    """Asking on an empty box is the commonest case there is, not a mistake."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        missing = await client.request(
            "completion/complete", {"ref": prompt_ref("alpha__greet")}
        )
        assert missing["error"]["code"] == errors.INVALID_PARAMS

        result = await client.call(
            "completion/complete",
            {"ref": prompt_ref("alpha__greet"), "argument": {"name": "who"}},
        )
        assert "arg:who=" in result["completion"]["values"]
    finally:
        await harness.close()


async def test_the_zoo_cascades_a_templates_two_variables(tmp_path) -> None:
    """A template with two variables, where the first decides what the second may be."""
    harness = await daemon(tmp_path, servers=ZOO_SERVERS)
    try:
        client = await harness.connect()
        ref = {
            "type": "ref/resource",
            "uri": encode_resource_uri(
                "zoo", "zoo://continents/{continent}/countries/{country}/animals"
            ),
        }
        everywhere = (await complete(client, ref, "country"))["completion"]["values"]
        assert len(everywhere) == 8

        narrowed = await complete(client, ref, "country", "", {"continent": "asia"})
        assert set(narrowed["completion"]["values"]) < set(everywhere)
        assert narrowed["completion"]["values"] == ["china", "nepal"]

        # A prefix narrows further, and matches the label as well as the id.
        typed = await complete(client, ref, "country", "ne", {"continent": "asia"})
        assert typed["completion"]["values"] == ["nepal"]

        # A variable this template does not name is not this template's to suggest, however
        # familiar the name is elsewhere in the server.
        assert (await complete(client, ref, "id"))["completion"]["values"] == []
    finally:
        await harness.close()


async def test_the_zoo_cascades_a_prompts_three_arguments(tmp_path) -> None:
    """The whole cascade in one form: continent, then country, then the animal itself."""
    harness = await daemon(tmp_path, servers=ZOO_SERVERS)
    try:
        client = await harness.connect()
        ref = prompt_ref("zoo__zoo-prompt-animal")

        continents = (await complete(client, ref, "continent"))["completion"]["values"]
        assert continents == ["africa", "americas", "asia", "oceania"]

        countries = await complete(client, ref, "country", "", {"continent": "asia"})
        assert countries["completion"]["values"] == ["china", "nepal"]

        animals = await complete(
            client, ref, "id", "", {"continent": "asia", "country": "nepal"}
        )
        assert animals["completion"]["values"] == ["red-panda"]

        # Unnarrowed is every animal, not none: a client that sends no context at all, or a
        # revision with nowhere to put it, still gets a usable list.
        assert len((await complete(client, ref, "id"))["completion"]["values"]) == 8

        # And a label matches as readily as an id, because a person types the name they see.
        assert (await complete(client, ref, "id", "Red"))["completion"]["values"] == ["red-panda"]
    finally:
        await harness.close()


async def test_the_zoo_refuses_a_combination_the_cascade_could_not_have_produced(
    tmp_path,
) -> None:
    """Filling the form top to bottom cannot send this; changing one field afterwards can."""
    harness = await daemon(tmp_path, servers=ZOO_SERVERS)
    try:
        client = await harness.connect()
        good = await client.call(
            "prompts/get",
            {
                "name": "zoo__zoo-prompt-animal",
                "arguments": {"continent": "asia", "country": "nepal", "id": "red-panda"},
            },
        )
        assert "Red Panda" in good["messages"][0]["content"]["text"]

        clash = await client.request(
            "prompts/get",
            {
                "name": "zoo__zoo-prompt-animal",
                "arguments": {"continent": "africa", "id": "red-panda"},
            },
        )
        assert clash["error"]["code"] == errors.INVALID_PARAMS
        # The backend's own `data` rides under `mcpData`, tagged with whose error it was,
        # so the animal's real countries reach the client that has to correct the form.
        assert clash["error"]["data"]["source"] == "mcp"
        assert clash["error"]["data"]["mcpData"]["countries"] == ["china", "nepal"]
    finally:
        await harness.close()
