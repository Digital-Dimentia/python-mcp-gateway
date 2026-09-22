"""The Makefile is the interface CI and every developer share; these are its invariants.

CI runs `make venv lint docs-check test build` and nothing else, so a target that
disappears or is renamed breaks the gate in a way no Python test would notice. These
assertions are deliberately about *shape* rather than about recipe text: they pin the
contract, not the implementation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"

#: Every target CI, the README, or the launchd plist names. Losing one silently is the
#: failure this file exists to prevent.
REQUIRED_TARGETS = frozenset(
    {
        "venv",
        "sync",
        "install",
        "lint",
        "docs-check",
        "test",
        "check",
        "build",
        "wheel",
        "sdist",
        "container-image",
        "print-release-platforms",
        "package",
        "run",
        "connect",
        # The pywebview desktop host. Same reasoning as the `tauri-*` block below, minus
        # the toolchain: `src/mcp_gateway/window.md` names it as the way to open the window.
        "run-window",
        # The desktop shell. Not in CI's `make venv lint docs-check test build` line and
        # deliberately not a prerequisite of any of it -- the daemon ships without the app
        # -- but README.md and src/desktop/README.md both name these, so losing one silently is
        # the same failure this file exists to prevent.
        "tauri-python",
        "tauri-stage",
        "tauri-dev",
        "tauri-bundle",
        "tauri-check",
        "clean",
        "clean-outputs",
        "clean-venv",
        "distclean",
    }
)


def _makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _phony_targets() -> frozenset[str]:
    text = _makefile_text()
    match = re.search(r"^\.PHONY:((?:[^\n\\]|\\\n)*)", text, re.MULTILINE)
    assert match is not None, "no .PHONY line in the Makefile"
    return frozenset(match.group(1).replace("\\\n", " ").split())


def test_every_required_target_is_declared_phony() -> None:
    assert REQUIRED_TARGETS <= _phony_targets()


def test_every_required_target_has_a_rule() -> None:
    text = _makefile_text()
    defined = set(re.findall(r"^([A-Za-z][A-Za-z0-9_.-]*):", text, re.MULTILINE))
    missing = REQUIRED_TARGETS - defined
    assert not missing, f"declared .PHONY but never defined: {sorted(missing)}"


def test_clean_does_not_remove_the_virtual_environment() -> None:
    """`clean` must stay safe to run offline.

    Deleting the venv is the one action in this file that forces a reinstall over the
    network -- and behind a TLS-intercepting proxy that may not be recoverable without
    PIP_TRUSTED_HOST. The removal lives in `clean-venv`/`distclean`, which are separate,
    deliberate words.
    """
    text = _makefile_text()
    body = text.split("\nclean: clean-outputs\n", 1)[1].split("\nclean-outputs:", 1)[0]
    assert "$(VENV_DIR)" not in body.replace("$(VENV_DIR) was left alone", "")

    outputs = text.split("\nclean-outputs:\n", 1)[1].split("\n\n", 1)[0]
    assert "$(VENV_DIR)" not in outputs


def test_launch_targets_do_not_take_venv_as_a_prerequisite() -> None:
    """`run` and `connect` use ENSURE_VENV instead.

    make resolves a prerequisite against its own cwd, so `make -f /abs/path/Makefile run`
    from elsewhere would die on `No rule to make target 'pyproject.toml'` before a recipe
    line ran -- and no chdir inside the recipe can help. Both targets are launched by
    other programs, which choose their own cwd.
    """
    text = _makefile_text()
    for target in ("run", "connect"):
        line = re.search(rf"^{target}:(.*)$", text, re.MULTILINE)
        assert line is not None, f"no rule for {target}"
        assert line.group(1).strip() == "", f"{target} must not declare prerequisites"
        body = text.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        assert "$(ENSURE_VENV)" in body, f"{target} must bootstrap via ENSURE_VENV"


def test_release_platforms_are_readable_by_the_release_workflow() -> None:
    """publish-artifacts.yml reads the list from make rather than repeating it."""
    result = subprocess.run(
        ["make", "-s", "print-release-platforms"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    platforms = result.stdout.strip().split(",")
    assert "linux/amd64" in platforms
    assert "linux/arm64" in platforms

    workflow = (REPO_ROOT / ".github" / "workflows" / "publish-artifacts.yml").read_text()
    assert "make -s print-release-platforms" in workflow


def test_ci_runs_the_same_targets_a_developer_does() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for target in ("make venv", "make lint", "make docs-check", "make test", "make build"):
        assert target in ci, f"CI no longer runs `{target}`"


def test_python_floor_is_consistent_across_the_declarations_that_claim_it() -> None:
    """requires-python, the classifiers, the CI matrix and the base image move together.

    A matrix leg is the only thing that checks the claim a classifier makes; a base image
    newer than the floor means the released container is not the compatibility the wheel
    advertises.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    containerfile = (REPO_ROOT / "Containerfile").read_text()

    classified = set(re.findall(r"Programming Language :: Python :: (3\.\d+)", pyproject))
    matrix = set(re.findall(r'"(3\.\d+)"', re.search(r"python-version: \[(.*?)\]", ci).group(1)))
    assert classified == matrix, f"classifiers {sorted(classified)} != CI matrix {sorted(matrix)}"

    floor = min(classified, key=lambda v: int(v.split(".")[1]))
    assert f">={floor}," in pyproject.replace(" ", "")
    assert f"FROM python:{floor}-slim" in containerfile
