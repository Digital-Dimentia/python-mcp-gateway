#!/usr/bin/env python3
"""Gather what `cargo tauri build` produced into one directory, under names that travel.

`tauri-bundle` leaves its output under `desktop/src-tauri/target/release/bundle/`, in a
per-format subdirectory, named for the product and its version and nothing else:
`MCP-Gateway.app`, `MCP-Gateway_0.1.0_amd64.deb`, `MCP-Gateway_0.1.0_x64-setup.exe`. Three
runners uploading those to one GitHub release is three assets, two of which collide on
nothing and one of which -- the `.app` -- is a *directory* a release cannot hold at all.

So this script does two things and no more:

* **Names every asset for its platform.** `MCP-Gateway-0.1.0-macos-aarch64.dmg`-shaped:
  product, version, OS, architecture. A person downloading from a release page can tell
  which file is theirs without opening any of them, and the same name is what a bug report
  can quote back.
* **Turns the `.app` into a file.** `tar.gz`, because it is the one archive format that
  preserves the symlinks inside a macOS bundle *and* the executable bit on the binary --
  `zip` on the runner would produce a bundle that unpacks unlaunchable.

Everything else the bundler emits (`.deb`, `.AppImage`, the NSIS installer) is already a
single file and is copied verbatim.

Deliberately not a release step of its own: it reads a directory and writes a directory,
runs identically on a developer's machine (`make tauri-artifacts`) and on a runner, and
knows nothing about GitHub.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where `cargo tauri build` leaves its output.
BUNDLE_ROOT = REPO_ROOT / "desktop" / "src-tauri" / "target" / "release" / "bundle"

#: The config the product name and version are read from, so this script cannot drift from
#: what the bundler actually stamped on the files.
TAURI_CONF = REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"

DEFAULT_OUT = REPO_ROOT / "artifacts"

#: What to look for, per platform, as globs relative to [`BUNDLE_ROOT`].
#:
#: One entry per bundle target named in `tauri.conf.json` and its two platform overlays --
#: `tauri.linux.conf.json` and `tauri.windows.conf.json`. A target added there and not here
#: is an artifact that is built and then silently not published, which is why
#: `tests/test_collect_desktop_bundle.py` checks the two lists against each other.
PATTERNS: dict[str, tuple[str, ...]] = {
    "macos": ("macos/*.app",),
    "linux": ("deb/*.deb", "appimage/*.AppImage"),
    "windows": ("nsis/*.exe",),
}


def log(message: str) -> None:
    print(f"collect_desktop_bundle: {message}", file=sys.stderr)


def os_slug() -> str:
    """`macos`, `linux` or `windows` -- the same words the uv triple uses."""
    slug = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if slug is None:
        raise SystemExit(f"collect_desktop_bundle: unsupported platform {platform.system()!r}")
    return slug


def arch_slug() -> str:
    """`aarch64` or `x86_64`.

    Case-folded and knowing the word `AMD64`, for the same reason
    `scripts/bundle_python.py` does: that is what `platform.machine()` says on Windows.
    """
    machine = platform.machine().lower()
    arch = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine)
    if arch is None:
        raise SystemExit(f"collect_desktop_bundle: unsupported architecture {machine!r}")
    return arch


def product_and_version(config: Path = TAURI_CONF) -> tuple[str, str]:
    conf = json.loads(config.read_text())
    return conf["productName"], conf["version"]


def asset_name(source: Path, product: str, version: str, platform_tag: str) -> str:
    """What `source` is published as.

    The `.app` becomes a `.app.tar.gz` -- kept in the name rather than hidden, so that what
    comes out of the archive is not a surprise. Everything else keeps its suffix, because
    the suffix is what tells a user's machine what to do with it.
    """
    stem = f"{product}-{version}-{platform_tag}"
    if source.suffix == ".app":
        return f"{stem}.app.tar.gz"
    if source.name.endswith("-setup.exe") or source.suffix == ".exe":
        return f"{stem}-setup.exe"
    return f"{stem}{source.suffix}"


def archive_app(app: Path, target: Path) -> None:
    """`tar.gz` a macOS bundle, symlinks and permission bits intact.

    `shutil.make_archive` would flatten the symlinks inside `Contents/Frameworks` into
    copies, and `tarfile` in Python does the right thing with both those and the executable
    bit. The bundle goes in at its own name, so unpacking anywhere yields `MCP-Gateway.app`
    and not a directory of its insides.
    """
    with tarfile.open(target, "w:gz") as archive:
        archive.add(app, arcname=app.name)


def verify_app(app: Path) -> None:
    """Ask `codesign` whether the bundle is one macOS will open, before shipping it.

    Ad-hoc signed is still signed, and the failure this catches is not subtle: a bundle with
    no `_CodeSignature` is one Apple Silicon may refuse outright, with a LaunchServices
    error code that names nothing. Cheap to ask here, expensive to find out from a user.

    A failure is reported and not fatal: signing properly is its own bead
    (python-mcp-gateway-c1q), and a release that stops because of it helps nobody.
    """
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log(f"warning: codesign --verify failed for {app.name}: {result.stderr.strip()}")


def collect(out: Path, bundle_root: Path = BUNDLE_ROOT) -> list[Path]:
    """Copy every bundle this platform produced into `out`. Returns what was written."""
    product, version = product_and_version()
    tag = f"{os_slug()}-{arch_slug()}"
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for pattern in PATTERNS[os_slug()]:
        for source in sorted(bundle_root.glob(pattern)):
            target = out / asset_name(source, product, version, tag)
            if source.suffix == ".app":
                verify_app(source)
                archive_app(source, target)
            else:
                shutil.copy2(source, target)
            log(f"{source.relative_to(bundle_root)} -> {target.name}")
            written.append(target)

    if not written:
        raise SystemExit(
            f"collect_desktop_bundle: nothing under {bundle_root} matched "
            f"{PATTERNS[os_slug()]} -- run `make tauri-bundle` first"
        )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Where assets land.")
    parser.add_argument(
        "--bundle-root",
        type=Path,
        default=BUNDLE_ROOT,
        help="Where to look for what the bundler produced.",
    )
    args = parser.parse_args(argv)

    written = collect(args.out.resolve(), args.bundle_root.resolve())
    log(f"{len(written)} asset(s) in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
