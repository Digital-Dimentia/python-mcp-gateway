"""The `source` discriminator, and the corollary that is easy to get wrong."""

from __future__ import annotations

import asyncio

import pytest

from mcp_gateway import errors


class FakeBackendError(Exception):
    """Stands in for `MCPProtocolError`, which lands with the subprocess client.

    Duck-typed on purpose: `errors.py` recognises a backend exception by the marker
    attribute, not by importing the transport layer.
    """

    mcp_error = True

    def __init__(self, message: str, *, code: int | None, backend: str, data=None):
        super().__init__(message)
        self.code = code
        self.backend = backend
        self.data = data


def test_data_is_omitted_when_empty_rather_than_emitted_as_null() -> None:
    """Presence is meaningful: a client checks `data.get("source")`."""
    assert errors.error_object(-32603, "boom") == {"code": -32603, "message": "boom"}
    assert "data" not in errors.error_object(-32603, "boom", {})


def test_a_forwarded_backend_error_keeps_its_code_and_message_and_is_tagged() -> None:
    mapped = errors.to_error_object(
        FakeBackendError("no such issue", code=-32042, backend="github", data={"id": 7})
    )
    assert mapped["code"] == -32042
    assert mapped["message"] == "no such issue"
    assert mapped["data"]["source"] == errors.BACKEND_SOURCE
    assert mapped["data"]["backend"] == "github"
    assert mapped["data"]["mcpCode"] == -32042
    assert mapped["data"]["mcpData"] == {"id": 7}


def test_an_error_the_gateway_originates_is_never_tagged() -> None:
    """The discriminator only works if we never claim an error we made is the backend's."""
    for exc in [
        errors.MethodNotFound("tools/nope"),
        errors.InvalidParams("bad"),
        errors.ResourceNotFound("gone"),
        ValueError("malformed"),
        RuntimeError("oops"),
    ]:
        mapped = errors.to_error_object(exc)
        assert "source" not in mapped.get("data", {}), exc


def test_a_codeless_backend_failure_is_ours_not_the_backends() -> None:
    """A timeout or a dead transport produced no code, so tagging it would invent one.

    This is the corollary the module exists to protect: claiming the backend emitted a code
    it never sent is the same fidelity loss as rewriting its message, arriving by the back
    door.
    """
    mapped = errors.to_error_object(
        FakeBackendError("timed out after 30s", code=None, backend="github")
    )
    assert mapped["code"] == errors.INTERNAL_ERROR
    assert "source" not in mapped["data"]
    assert mapped["data"]["backend"] == "github"
    assert mapped["data"]["reason"] == "timed out after 30s"


def test_value_errors_map_to_invalid_params() -> None:
    """Which is why NamingError and ConfigError are both ValueErrors."""
    from mcp_gateway.config import ConfigError
    from mcp_gateway.naming import NamingError

    for exc in [NamingError("bad name"), ConfigError("bad config")]:
        assert errors.to_error_object(exc)["code"] == errors.INVALID_PARAMS


def test_cancellation_is_re_raised_never_mapped() -> None:
    """Mapping it would turn a cancelled task into a completed one."""
    with pytest.raises(asyncio.CancelledError):
        errors.to_error_object(asyncio.CancelledError())


async def test_the_decorator_maps_and_marks_itself() -> None:
    @errors.as_error_object
    async def handler():
        raise errors.InvalidParams("nope")

    assert handler.maps_errors is True
    with pytest.raises(errors.GatewayErrorObject) as caught:
        await handler()
    assert caught.value.error["code"] == errors.INVALID_PARAMS


async def test_the_decorator_lets_cancellation_through() -> None:
    @errors.as_error_object
    async def handler():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await handler()


async def test_the_decorator_returns_a_successful_result_untouched() -> None:
    @errors.as_error_object
    async def handler():
        return {"ok": True}

    assert await handler() == {"ok": True}
