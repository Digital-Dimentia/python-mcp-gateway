"""`tests/ui/`: the admin UI's JavaScript, run rather than only parsed.

Driven from pytest so the project keeps one test command, the way
`test_webui.py::test_the_modules_parse` already drives `node --check`. Skipped rather than
required: a checkout with no Node, or one that has not run `npm install` in `tests/ui`,
stays green -- the daemon ships no Node anything and nothing here is a runtime dependency.

**Why there is a jsdom suite at all**, given that `webui.md` spent a while arguing there
should not be. Two logic bugs landed in the variables column in one sitting, both in code no
Python test can reach: a cascade child inherited its parent's variable and built
`zoo://continents/congo/countries//animals`, and a form opened onto picks already made came
up empty (that one shipped, in 4c8d117, and a person found it in a browser). Both were cheap
to catch the moment there was a DOM to run the real modules against. See
`src/mcp_gateway/webui.md` and python-mcp-gateway-6cm.

What is *not* here is anything about how the page looks. jsdom has no layout, so a passing
test here is not a promise that the page renders; `make run-dev` against
`examples/zoo_server.py` is still what says that.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_TESTS = REPO_ROOT / "tests" / "ui"

#: `node --test` with `describe`/`it` out of `node:test` is stable from 20.
MINIMUM_NODE = (20,)

INSTALL = "npm install --prefix tests/ui"


def node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    try:
        major = int(version.strip().lstrip("v").split(".")[0])
    except ValueError:                                       # pragma: no cover - odd build
        pytest.skip(f"cannot read node's version from {version!r}")
    if (major,) < MINIMUM_NODE:
        pytest.skip(f"node {major} is older than {MINIMUM_NODE[0]}, which `node --test` needs")
    return node


def jsdom_or_skip(node: str) -> None:
    """Asked of node rather than of the filesystem.

    A local `tests/ui/node_modules` is the ordinary way to have jsdom, but a global install
    reached through NODE_PATH is just as good and this is the one check that covers both.
    """
    probe = subprocess.run(
        [node, "--input-type=module", "-e", "await import('jsdom')"],
        cwd=UI_TESTS, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"jsdom is not installed; run `{INSTALL}` to run the UI suite")


def test_the_modules_behave() -> None:
    """The whole of `tests/ui/`, as one test.

    One subprocess rather than one per file: `node --test` is the runner, and splitting it
    would put pytest in the business of discovering JavaScript. What a failure needs is the
    runner's own report, which is what gets attached to the assertion below.
    """
    node = node_or_skip()
    jsdom_or_skip(node)
    result = subprocess.run(
        [node, "--test", "--test-reporter=spec"],
        cwd=UI_TESTS, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"\n{result.stdout}\n{result.stderr}"


def test_every_suite_is_reached_by_that_one_test() -> None:
    """A suite `node --test` does not pick up is a suite nobody runs.

    Cheap insurance against the naming convention drifting: the runner matches `*.test.mjs`,
    so a file named `variables.mjs` would sit there green and unexecuted.
    """
    suites = {p.name for p in UI_TESTS.glob("*.mjs")} - {"harness.mjs", "zoo.mjs"}
    assert suites, "tests/ui/ has no suites"
    for name in suites:
        assert name.endswith(".test.mjs"), f"{name} is not a name `node --test` runs"
