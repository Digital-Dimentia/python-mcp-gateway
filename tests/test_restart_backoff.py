"""The floor between one failed start and the next attempt.

`consecutive_failures` was tracked and nothing read it, so nothing stopped a caller from
asking for a restart as fast as it could ask. That caller is not hypothetical: a model
holding `gateway__restart_backend` is exactly the kind of thing that retries a failing tool
in a loop, and every turn of that loop costs a process spawn plus a whole `startup_timeout`
at a backend that was never going to come up.

Driven against real subprocesses, like the rest of the backend tests, except for the
schedule itself -- which is arithmetic and deserves arithmetic.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from mcp_gateway.backend import (
    RESTART_BACKOFF_CAP_SECONDS,
    Backend,
    BackendStatus,
    restart_backoff_seconds,
)
from mcp_gateway.config import ServerSpec
from mcp_gateway.secrets import SecretStore
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY_EXE = sys.executable

#: One that starts perfectly well, for the ordinary case.
WORKING = f"""
servers:
  mock:
    command: {PY_EXE}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "mock"
      MOCK_TOOLS: "fine"
"""

#: A backend that cannot start, ever: the secret its env names is not in the store.
UNCONFIGURED = f"""
servers:
  github:
    command: {PY_EXE}
    args: ["{FIXTURE}"]
    env:
      TOKEN: "${{MISSING_TOKEN}}"
"""


def test_the_first_retry_is_free_and_the_rest_double() -> None:
    """Zero, then one second, then doubling to the cap.

    The first retry is free because a failed start is usually followed by a human fixing
    what broke and then asking for a restart; making that attempt wait punishes the one
    caller who already knows the answer.
    """
    assert restart_backoff_seconds(0) == 0.0
    assert restart_backoff_seconds(1) == 0.0
    assert [restart_backoff_seconds(n) for n in range(2, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


def test_the_delay_stops_growing() -> None:
    """A ceiling, not an ever-doubling delay: the point is to damp a loop, not to give up."""
    assert restart_backoff_seconds(20) == RESTART_BACKOFF_CAP_SECONDS
    assert restart_backoff_seconds(200) == RESTART_BACKOFF_CAP_SECONDS


async def test_a_backend_that_cannot_start_refuses_the_second_restart(tmp_path) -> None:
    """The behaviour the whole thing is for.

    The startup sweep is failure one. The first restart is allowed and is failure two, which
    is what puts a floor down; the next one is refused without spawning anything.
    """
    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        backend = harness.gateway.backend("github")
        assert backend.consecutive_failures == 1, "the startup sweep already tried once"
        assert backend.retry_after_seconds == 0.0, "and the first retry is free"

        assert await harness.gateway.restart_backend("github") is False
        assert backend.consecutive_failures == 2
        assert backend.retry_after_seconds > 0.0

        assert await harness.gateway.restart_backend("github") is False
        assert backend.consecutive_failures == 2, "a refusal is not an attempt"
    finally:
        await harness.close()


async def test_a_refusal_changes_nothing_about_the_backend(tmp_path) -> None:
    """Not even the floor, and not the status.

    A refused restart that reported STOPPED would read as "somebody turned this off" rather
    than "this is broken and waiting", and one that pushed the floor out again would let a
    caller in a tight loop extend its own punishment forever.
    """
    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        backend = harness.gateway.backend("github")
        await harness.gateway.restart_backend("github")
        was = (backend.status, backend.error, backend.restart_count, backend.retry_after_seconds)

        await harness.gateway.restart_backend("github")
        assert backend.status is BackendStatus.FAILED
        assert backend.status == was[0]
        assert backend.error == was[1], "the error still says why it is down"
        assert "MISSING_TOKEN" in backend.error
        assert backend.restart_count == was[2], "a refusal did not restart anything"
        assert backend.retry_after_seconds <= was[3], "and the floor did not move out"
    finally:
        await harness.close()


async def test_the_floor_is_reported_so_an_operator_can_see_it(tmp_path) -> None:
    """`gateway__backend_health`, because a restart button that does nothing needs a reason."""
    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        client = await harness.connect()
        await harness.gateway.restart_backend("github")
        # Named, so the tool answers with the one backend rather than a list of them.
        entry = json.loads(await client.tool_text("gateway__backend_health", {"name": "github"}))
        assert entry["name"] == "github"
        assert entry["consecutive_failures"] == 2
        assert entry["retry_after_seconds"] > 0

        # ...and the same field over `/admin`, which is what the UI reads.
        admin = await harness.connect("/admin", initialize=False)
        reported = await admin.call("admin.health", {"name": "github"})
        assert reported["retry_after_seconds"] > 0
    finally:
        await harness.close()


async def test_the_floor_clears_once_the_backend_starts(tmp_path) -> None:
    """Otherwise a backend that recovered would still be refusing restarts."""
    store = SecretStore({})
    backend = Backend(
        spec=ServerSpec(name="mock", command=PY_EXE, args=(str(FIXTURE),)),
        store=store,
    )
    # Two failures put a real floor down...
    backend.consecutive_failures = 2
    backend._retry_at = time.monotonic() + 30.0
    assert backend.cooling_off
    assert await backend.start() is False, "refused while the floor stands"

    # ...and a start that works clears both, rather than leaving a healthy backend unable to
    # be restarted for another half a minute.
    backend._retry_at = None
    try:
        assert await backend.start() is True, backend.error
        assert backend.consecutive_failures == 0
        assert backend.retry_after_seconds == 0.0
        assert not backend.cooling_off
    finally:
        await backend.stop()


async def test_a_working_backend_is_never_held_back(tmp_path) -> None:
    """The ordinary case: nothing has failed, so nothing waits."""
    harness = await daemon(tmp_path, servers=WORKING)
    try:
        backend = harness.gateway.backend("mock")
        assert backend.running, backend.error
        assert backend.retry_after_seconds == 0.0
        assert not backend.cooling_off
        # Twice in a row, because a healthy backend has no failure count to back off from.
        assert await harness.gateway.restart_backend("mock") is True
        assert await harness.gateway.restart_backend("mock") is True
    finally:
        await harness.close()
