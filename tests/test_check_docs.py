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
