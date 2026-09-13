"""Lazy spawn and idle teardown: a backend that is not running and is not broken.

The feature is a memory and process-count trade, and the thing it must not trade away is
the client's view. A slept backend keeps advertising, wakes on the next call, and shows up
as `idle` rather than as `stopped` or `failed` -- three states an operator has to be able to
tell apart, because only one of them is a reason to go and look at something.

The design turns on one property: **a listing does not change by the process going away.**
The same command with the same config publishes the same tools, so the catalogue can keep
answering from what the backend last said. That is what makes an idle teardown invisible,
and `test_a_slept_backend_still_advertises_its_tools` is the assertion the whole thing rests
on.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from mcp_gateway.backend import BackendStatus
from mcp_gateway.config import ConfigError, parse
from mcp_gateway.naming import encode_resource_uri
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable


#: `napper`'s ttl, named so the backdating helper can be sure it clears it.
IDLE_TTL = 3600


def idle_since_long_ago(supervisor, *, seconds: float = 2 * IDLE_TTL) -> None:
    """Backdate every backend's last call far enough that the sweep must act on it.

    **Relative to `time.monotonic()`, never an absolute `0.0`.** That clock counts from boot
    on Linux, so on a CI runner that has been up for two minutes `0.0` does not mean "long
    ago", it means "two minutes ago" -- which is inside a 3600-second ttl, and the sweep
    correctly does nothing. `Backend.idle_for` documents exactly this hazard; a test that
    fabricates a zero walks into it, and does so only on a freshly booted machine.
    """
    now = time.monotonic()
    for backend in supervisor.all:
        backend.last_call_monotonic = now - seconds


def entry(name: str, *, extra: str = "", **env: str) -> str:
    lines = [f"  {name}:", f"    command: {PY}", f'    args: ["{FIXTURE}"]']
    if extra:
        lines.append(f"    {extra}")
    if env:
        lines.append("    env:")
        lines += [f'      {k}: "{v}"' for k, v in env.items()]
    return "\n".join(lines) + "\n"


SERVERS = "servers:\n" + entry("eager", MOCK_NAME="eager") + entry(
    "napper", extra=f"idle_ttl: {IDLE_TTL}", MOCK_NAME="napper", MOCK_RESOURCES="file:///a.md",
    MOCK_SUBSCRIBE="1",
) + entry("sleepy", extra="lazy: true", MOCK_NAME="sleepy")


# --- config ---------------------------------------------------------------------------------


def test_lazy_and_idle_ttl_parse_and_inherit_from_defaults() -> None:
    parsed = parse(
        "defaults:\n  idle_ttl: 600\n  lazy: true\n"
        "servers:\n  a:\n    command: x\n  b:\n    command: y\n    idle_ttl: 60\n    lazy: false\n"
    )
    assert (parsed.servers["a"].idle_ttl, parsed.servers["a"].lazy) == (600.0, True)
    assert (parsed.servers["b"].idle_ttl, parsed.servers["b"].lazy) == (60.0, False)


def test_omitting_idle_ttl_means_never_rather_than_immediately() -> None:
    """`None` and `0` are different facts, so a zero is refused rather than quietly meaning
    "tear it down the instant it goes idle", which is a thrash and not a policy."""
    assert parse("servers:\n  a:\n    command: x\n").servers["a"].idle_ttl is None
    for bad in ("0", "-5"):
        try:
            parse(f"servers:\n  a:\n    command: x\n    idle_ttl: {bad}\n")
        except ConfigError as exc:
            assert "greater than zero" in str(exc)
        else:  # pragma: no cover - the assertion above is the test
            raise AssertionError(f"idle_ttl: {bad} was accepted")


# --- lazy -------------------------------------------------------------------------------------


async def test_a_lazy_backend_is_not_spawned_at_startup(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        by_name = {b.name: b for b in harness.gateway.supervisor.all}
        assert by_name["eager"].running
        assert by_name["sleepy"].status is BackendStatus.IDLE
        assert by_name["sleepy"].pid is None
    finally:
        await harness.close()


async def test_listing_wakes_a_lazy_backend_that_has_never_run(tmp_path) -> None:
    """There is no cached answer to give for one nobody has used, so waking is the cost of
    `lazy`, paid once."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert "sleepy__echo" in await client.tool_names()
        assert harness.gateway.supervisor.get("sleepy").running
    finally:
        await harness.close()


async def test_a_call_wakes_a_lazy_backend(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert await client.tool_text("sleepy__echo", {"text": "up"}) == "sleepy:echo:up"
    finally:
        await harness.close()


# --- sleeping ------------------------------------------------------------------------------------


async def test_a_slept_backend_still_advertises_its_tools(tmp_path) -> None:
    """The property the whole feature rests on. A listing does not change by the process
    going away, so tearing it down must not make tools vanish from a client's list."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert "napper__echo" in await client.tool_names()

        await harness.gateway.supervisor.get("napper").sleep()
        assert harness.gateway.supervisor.get("napper").status is BackendStatus.IDLE
        assert "napper__echo" in await client.tool_names()
    finally:
        await harness.close()


async def test_a_call_on_a_slept_backend_wakes_it_rather_than_failing(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        await client.tool_text("napper__echo", {"text": "one"})
        await harness.gateway.supervisor.get("napper").sleep()
        assert await client.tool_text("napper__echo", {"text": "two"}) == "napper:echo:two"
        assert harness.gateway.supervisor.get("napper").running
    finally:
        await harness.close()


async def test_sleeping_is_reported_as_idle_and_not_as_stopped_or_failed(tmp_path) -> None:
    """An operator glancing at the list has to be able to tell "the daemon reclaimed this"
    from "somebody turned it off" and from "this is broken"."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        await harness.gateway.supervisor.get("napper").sleep()
        client = await harness.connect()
        listed = await client.tool_text("gateway__list_backends")
        assert '"idle"' in listed or "idle" in listed
        napper = harness.gateway.supervisor.get("napper")
        assert napper.status is BackendStatus.IDLE
        assert napper.error is None
    finally:
        await harness.close()


async def test_sleeping_does_not_count_as_a_failure(tmp_path) -> None:
    """Letting it feed the restart backoff would make a quiet backend progressively slower
    to wake, which is the opposite of what the feature is for."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        napper = harness.gateway.supervisor.get("napper")
        for _ in range(5):
            await napper.sleep()
            await harness.gateway.supervisor.wake("napper")
        assert napper.consecutive_failures == 0
        assert napper.running
    finally:
        await harness.close()


# --- the sweep ---------------------------------------------------------------------------------


async def test_the_sweep_sleeps_a_backend_past_its_ttl_and_leaves_the_others(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        supervisor = harness.gateway.supervisor
        await supervisor.wake("sleepy")
        # `eager` and `sleepy` set no ttl, so no amount of idleness applies to them.
        idle_since_long_ago(supervisor)
        slept = await supervisor.sweep_idle()
        assert slept == ["napper"]
        assert supervisor.get("eager").running
        assert supervisor.get("sleepy").running
    finally:
        await harness.close()


async def test_a_backend_inside_its_ttl_is_left_alone(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        supervisor = harness.gateway.supervisor
        assert await supervisor.sweep_idle() == []
        assert supervisor.get("napper").running
    finally:
        await harness.close()


async def test_a_subscribed_backend_is_never_slept(tmp_path) -> None:
    """A sleeping process cannot send `resources/updated`, and a client watching a resource
    cannot tell silence from nothing having changed. Reclaiming a process at the cost of
    quietly breaking a feature the client asked for is not the daemon's trade to make."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        uri = encode_resource_uri("napper", "file:///a.md")
        await client.call("resources/subscribe", {"uri": uri})
        idle_since_long_ago(harness.gateway.supervisor)

        exempt = harness.gateway._subscribed_backends()
        assert exempt == frozenset({"napper"})
        assert await harness.gateway.supervisor.sweep_idle(exempt=exempt) == []
        assert harness.gateway.supervisor.get("napper").running
    finally:
        await harness.close()


async def test_a_backend_with_a_call_in_flight_is_not_idle(tmp_path) -> None:
    """A `tools/call` waiting on a human is the longest-running thing this daemon does."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        supervisor = harness.gateway.supervisor
        napper = supervisor.get("napper")
        idle_since_long_ago(supervisor)
        with napper.serving(object()):
            assert await supervisor.sweep_idle() == []
            assert napper.running
        assert await supervisor.sweep_idle() == ["napper"]
    finally:
        await harness.close()


async def test_concurrent_calls_on_one_sleeping_backend_spawn_one_process(tmp_path) -> None:
    """Starting a subprocess twice would leave one orphaned with nobody holding its pipes."""
    import asyncio

    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        await client.tool_text("napper__echo", {"text": "warm"})
        await harness.gateway.supervisor.get("napper").sleep()

        results = await asyncio.gather(
            *(client.tool_text("napper__echo", {"text": str(n)}) for n in range(6))
        )
        assert sorted(results) == sorted(f"napper:echo:{n}" for n in range(6))
        pids = {harness.gateway.supervisor.get("napper").pid}
        assert None not in pids and len(pids) == 1
    finally:
        await harness.close()


async def test_the_sweeper_only_runs_when_something_uses_the_feature(tmp_path) -> None:
    """A deployment that sets no `idle_ttl` should not get a task waking up forever to find
    nothing to do."""
    without = await daemon(tmp_path, servers="servers:\n" + entry("plain", MOCK_NAME="plain"))
    try:
        assert without.gateway._idle_sweeper is None
    finally:
        await without.close()

    other = tmp_path / "other"
    other.mkdir()
    with_ttl = await daemon(other, servers=SERVERS)
    try:
        assert with_ttl.gateway._idle_sweeper is not None
    finally:
        await with_ttl.close()
