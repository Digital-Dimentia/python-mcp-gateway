#!/usr/bin/env python3
"""Carry `branding:` into the desktop bundle, which the daemon cannot reach.

The window *title* is solved at run time: the page reads `admin.status` and calls
`setTitle`. Two things are not, because they are baked into the artifact before any config
file exists -- the bundle's name (`Acme-Tools.app`, the process in Activity Monitor, the
Windows install directory) and its icon (the Dock, the taskbar, the Finder). Those are
Tauri's `productName` and `bundle.icon`, read by `cargo tauri build`.

So this script writes an **overlay**, `desktop/src-tauri/tauri.brand.json`, which
`make tauri-bundle` passes to `--config` when it exists. It does not edit
`tauri.conf.json`.

## Why an overlay rather than an edit

`tauri.conf.json` is committed, and `tests/test_desktop_layout.py` pins what is in it --
that the bundle name needs no quoting, that the CSP cannot dial out, that the resources are
there. Rewriting that file in place would mean a rebranded checkout fails its own test suite
and shows a permanent diff in `git status`, which is how a rebrand ends up committed by
accident. The overlay is gitignored, is regenerated from the catalogue whenever the
catalogue changes, and merges exactly the way `tauri.windows.conf.json` beside it already
does.

## Why the name is not the title

`productName` becomes a path. `Acme Internal Tools.app` is legal and Apple uses spaces
freely, but that path gets typed, pasted into scripts and handed to `open`, where an
unquoted space fails in a way that reads as a broken app rather than a broken command line.
So whitespace becomes `-` and anything that is not alphanumeric, `-`, `_` or `.` is dropped.
The window keeps the prose; nothing has to quote a title bar.

## The icon needs a raster

`.icns` and `.ico` are built from pixels, and `cargo tauri icon` wants a large square PNG --
1024x1024 is what Tauri documents. An SVG is the right answer for the *web* favicon and the
wrong input here, so a non-PNG `icon:` is reported and skipped rather than guessed at: the
bundle keeps the stock icon, the admin UI still wears the brand, and the message says what
file to add. Point `--icon` at a PNG to brand the bundle without changing the catalogue.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from mcp_gateway.branding import DEFAULT_TITLE  # noqa: E402
from mcp_gateway.config import ConfigError, load  # noqa: E402

TAURI_DIR = REPO / "desktop" / "src-tauri"
OVERLAY = TAURI_DIR / "tauri.brand.json"
#: Generated icons go beside the stock set, never over it: `cargo tauri icon` defaults to
#: overwriting `icons/`, and a committed file replaced by a build step is a diff nobody
#: asked for.
ICON_DIR = TAURI_DIR / "icons" / "brand"

#: What `cargo tauri icon` produces and Tauri's bundler consumes, in the order the default
#: config lists them: the two PNGs for Linux, the Apple bundle icon, the Windows one.
ICON_SET = (
    "32x32.png",
    "128x128.png",
    "128x128@2x.png",
    "icon.icns",
    "icon.ico",
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def product_name(title: str) -> str:
    """A title as a filesystem path component. See the module docstring."""
    collapsed = _UNSAFE.sub("", re.sub(r"\s+", "-", title.strip()))
    # `Acme / Tools` loses the slash and would otherwise keep both hyphens around it.
    collapsed = re.sub(r"-{2,}", "-", collapsed)
    return collapsed.strip("-.") or "MCP-Gateway"


def generate_icons(source: Path) -> bool:
    """Run `cargo tauri icon`. Returns whether there is now a branded icon set."""
    if source.suffix.lower() != ".png":
        print(
            f"branding icon {source} is not a PNG; the bundle keeps its stock icon.\n"
            f"  The .icns and .ico in a desktop bundle are built from pixels: add a square "
            f"PNG (1024x1024 is what Tauri wants) and re-run with --icon pointing at it.",
            file=sys.stderr,
        )
        return False
    if shutil.which("cargo") is None:
        print("cargo is not on PATH; skipping icon generation.", file=sys.stderr)
        return False
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cargo", "tauri", "icon", str(source), "--output", str(ICON_DIR)],
        cwd=TAURI_DIR,
    )
    if result.returncode != 0:
        print(
            "cargo tauri icon failed. `cargo install tauri-cli` provides it; "
            "the bundle keeps its stock icon until it succeeds.",
            file=sys.stderr,
        )
        return False
    missing = [name for name in ICON_SET if not (ICON_DIR / name).exists()]
    if missing:
        print(f"cargo tauri icon wrote no {', '.join(missing)}; keeping the stock icon.", file=sys.stderr)
        return False
    return True


def overlay_for(title: str, *, icons: bool) -> dict:
    """The merge document. Only the keys a rebrand changes -- everything else inherits."""
    document: dict = {
        "productName": product_name(title),
        "app": {"windows": [{"title": title}]},
    }
    if icons:
        # Relative to `src-tauri/`, which is where `cargo tauri build` resolves them.
        document["bundle"] = {"icon": [f"icons/brand/{name}" for name in ICON_SET]}
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO / "servers.yaml",
        help="the catalogue to read `branding:` from (default: servers.yaml)",
    )
    parser.add_argument(
        "--icon",
        type=Path,
        help="a square PNG for the bundle icon, overriding the catalogue's `icon:`. "
        "Use this when the web favicon is an SVG, which it should be.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="remove the overlay and the generated icons, returning the bundle to stock",
    )
    args = parser.parse_args()

    if args.clear:
        OVERLAY.unlink(missing_ok=True)
        shutil.rmtree(ICON_DIR, ignore_errors=True)
        print(f"removed {OVERLAY.relative_to(REPO)} and the generated icons; the bundle is stock.")
        return 0

    try:
        branding = load(args.config).branding
    except ConfigError as refusal:
        print(str(refusal), file=sys.stderr)
        return 2

    source = args.icon or branding.icon_path
    icons = generate_icons(source) if source else False
    if branding.title == DEFAULT_TITLE and not icons:
        # Nothing to say. Leaving a stale overlay behind would be worse than writing none:
        # it would keep applying a rebrand the catalogue no longer asks for.
        OVERLAY.unlink(missing_ok=True)
        print(
            f"{args.config} has no branding the bundle can carry; "
            f"no overlay written (the window title is applied at run time regardless)."
        )
        return 0

    OVERLAY.write_text(json.dumps(overlay_for(branding.title, icons=icons), indent=2) + "\n")
    print(f"wrote {OVERLAY.relative_to(REPO)}: {product_name(branding.title)}.app, titled {branding.title!r}")
    print("`make tauri-bundle` picks it up automatically; `make tauri-brand ARGS=--clear` undoes it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
