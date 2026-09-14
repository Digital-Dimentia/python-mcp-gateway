"""`scripts/check_docs.py` is a gate; this is the gate on the gate.

It runs the real checker over the real repository, which is the only assertion that
matters: every production module has its sibling `.md`, every relative link resolves, and
no Mermaid edge names a node its block never defined.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repository_satisfies_its_own_documentation_invariants() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_docs.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cargos_build_tree_is_not_mistaken_for_ours() -> None:
    """`src/desktop/src-tauri/target/` is vendored crate sources, not this project's docs.

    A dependency's own `README.md` is full of links that resolve inside *its* repository, so
    a walker that descends into `target/` fails `make docs-check` on files nobody here
    wrote. This is the `.venv` bug the module documents, in a second language -- and the
    reason it is pinned is that the first one was found only after it bit.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from check_docs import is_ignored
    finally:
        sys.path.pop(0)

    assert is_ignored(Path("src/desktop/src-tauri/target/package/foo-1.0/README.md"))
    assert is_ignored(Path("src/desktop/src-tauri/target/debug/build/x/out/NOTES.md"))
    # The shell's own documentation is ours, and stays checked.
    assert not is_ignored(Path("src/desktop/README.md"))
    assert not is_ignored(Path("src/desktop/src-tauri/src/supervisor.rs"))
