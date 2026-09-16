#!/usr/bin/env python3
"""Put the admin UI where Tauri looks for a frontend, without making a second copy of it.

There is one admin UI, and it lives in `src/mcp_gateway_ui/`. Two hosts serve it: `webui.py`
answers a GET for each file over the daemon's own port, and the desktop shell loads the same
files off disk inside its window. A second checked-in copy would drift within a week -- the
whole reason `webui.ASSETS` exists as an allowlist that `tests/test_webui.py` pins against
the directory listing is that this project already decided one list beats two.

So `src/desktop/.staging/ui` is a *view*, rebuilt by this script and gitignored. Nothing is ever
edited there.

## Two modes, and why both

`--mode link` is for `make tauri-dev`: one symlink, so saving `app.js` in its real home and
pressing Cmd+R in the Tauri window shows the change. That is the same loop as `make run-dev`
and a browser reload, which is the loop this project's UI work actually happens in.

`--mode copy` is for `make tauri-bundle`, and is not optional there: the bundler walks the
frontend directory and would follow a symlink into a path that does not exist inside the
`.app`. The copy takes **`webui.ASSETS` as its manifest rather than globbing the directory**,
so a file that is in the folder but not in the allowlist cannot reach the bundle -- the
allowlist is the security boundary `webui.py` documents, and the desktop build has no
business being more generous than the HTTP one.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "src" / "mcp_gateway_ui"
DEFAULT_TARGET = REPO_ROOT / "src" / "desktop" / ".staging" / "ui"


def assets() -> dict[str, str]:
    """`webui.ASSETS`, imported rather than duplicated.

    Imported from the source tree, not from an installed copy: this runs before any build,
    and the point is to stage what is in *this* checkout.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from mcp_gateway import webui
    finally:
        sys.path.pop(0)
    return dict(webui.ASSETS)


def clear(target: Path) -> None:
    """Remove whatever is there, symlink or directory.

    `Path.exists()` follows symlinks, so a dangling one -- what a `--mode link` staging
    becomes after the repository moves -- reads as absent and would survive a rebuild.
    """
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def stage_link(target: Path) -> None:
    clear(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Relative, so the staging directory keeps working if the checkout is moved or the
    # repository is mounted at a different path inside a container. Computed from `SOURCE`
    # rather than spelled out: a literal `../../...` is a second copy of the layout, and it
    # goes stale silently the next time either end of it moves.
    hop = Path(os.path.relpath(SOURCE, target.parent))
    target.symlink_to(hop, target_is_directory=True)


def stage_copy(target: Path) -> list[str]:
    clear(target)
    target.mkdir(parents=True, exist_ok=True)
    staged = []
    for name in sorted(assets()):
        source = SOURCE / name
        if not source.is_file():
            raise SystemExit(f"stage_ui: webui.ASSETS names {name}, which is not in {SOURCE}")
        # A name may carry a `/` -- the screens are in `screens/`, and `screens/basics/` under
        # that -- and `copy2` does not make the directory it is copying into.
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged.append(name)
    return staged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("link", "copy"), default="copy")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args(argv)

    target = args.target
    if args.mode == "link":
        stage_link(target)
        print(f"stage_ui: {target} -> {SOURCE} (symlink; edits are live)", file=sys.stderr)
    else:
        staged = stage_copy(target)
        print(f"stage_ui: copied {len(staged)} asset(s) to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
