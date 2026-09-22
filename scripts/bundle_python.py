#!/usr/bin/env python3
"""Build the interpreter the desktop shell ships: standalone CPython, with the gateway in it.

The Tauri app does not ask the machine for a Python. It carries one, as a bundle resource,
and `supervisor.rs` runs `<resources>/python/bin/python3 -m mcp_gateway.cli`. This script
produces that directory.

## Why this is cheap here, and would not be elsewhere

Both runtime dependencies are pure Python **by decision, not by luck** -- `ruamel.yaml` is
taken plain rather than with the `libyaml` extras, and `websockets` was chosen over the `mcp`
SDK partly to keep compiled extensions out of the process (see the long note in
`pyproject.toml`). So `site-packages` here is architecture-independent, and the interpreter
is the only per-target artifact in the whole bundle -- which Tauri builds per target anyway.

That is also why there is no PyInstaller. A freezer would buy one file and cost the thing
`webui.py` depends on: `importlib.resources` finding `mcp_gateway_ui/*.js` inside the bundle
exactly as it finds them in a checkout. Here that keeps working because the layout is a
real interpreter with a real `site-packages`, and `cli.py` in the shipped app is still a file
a person can open when a user reports something.

## The interpreter

python-build-standalone, fetched through `uv python install`. uv's managed builds *are*
python-build-standalone, so using uv is not a second dependency on a second thing -- it is
the same artifact with a downloader that already handles the platform triple, and one this
project's contributors have installed anyway.

Without uv on PATH the script fetches the same artifact itself: `PBS_RELEASE`'s
`install_only_stripped` tarball for this triple, checked against a hash pinned in this file,
then adopted exactly as `--interpreter` adopts one -- and the wheel goes in through the
interpreter's own pip instead of `uv pip`. uv stays the default wherever it is installed,
because it floats to the newest patch release and the built-in fetcher cannot; what the
second path costs is a pin to bump by hand. `--fetcher` forces either.

`--interpreter` is the way past both downloads, and exists because the download is the one
step here that a corporate firewall breaks -- everything after it is local. uv's own knobs come first:
`UV_PYTHON_INSTALL_MIRROR` accepts a `file://` directory, so "download the tarball however
you can, then build offline" needs no code at all. `--interpreter` is for underneath that,
where uv cannot run; it adopts an unpacked tree or a `.tar.gz`, and then changes nothing --
`verify` is still the gate. `src/desktop/README.md` has the recipes.

Two edits are made to the interpreter, whichever way it arrived:

* **`EXTERNALLY-MANAGED` is removed.** It exists to stop a person mutating uv's shared copy
  of an interpreter. This is not that copy: it is a private tree that is about to be sealed
  inside a `.app`, and installing the gateway into it is the entire point.
* **The versioned directory name is flattened.** uv installs to
  `cpython-3.13.15-macos-aarch64-none/`; the bundle wants `python/`, so that the Rust side
  names one path that does not move when a patch release lands.

## Three platforms, one script

The interpreter is the only per-target artifact in the bundle, and `uv python install`
already knows every triple, so the platform differences here are all *shape*: Unix is
`bin/python3` beside `lib/python3.13/`, Windows is `python.exe` beside `Lib/` and `DLLs/`.
`interpreter_path`, `stdlib_path` and the two strip lists are the whole of it; the policy --
what is removed, what is verified, what the manifest records -- is identical everywhere.

`interpreter_path` is twinned with `Layout::interpreter` in `supervisor.rs`, which names the
same two paths from Rust.

## Bytecode

Compiled here, and `supervisor.rs` runs the child with `PYTHONDONTWRITEBYTECODE=1`. The
resources directory inside a signed `.app` is read-only -- and under Gatekeeper path
translocation it is read-only in a way that surprises people -- so an interpreter that tried
to write `.pyc` files on first import would silently pay full compile cost on every single
launch. `--invalidation-mode unchecked-hash` is what makes the pre-compiled files usable
without a source `mtime` check, which matters because copying a tree into a bundle does not
preserve the mtimes the default mode validates against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the bundle lands. `tauri.conf.json` names the same path in `bundle.resources`, and
#: `supervisor.rs` resolves `python/bin/python3` under the app's resource dir.
DEFAULT_OUT = REPO_ROOT / "src" / "desktop" / "src-tauri" / "resources" / "python"

#: Pinned rather than floated. `requires-python` allows 3.12 through 3.14, which is a
#: statement about what the *library* supports; a bundle ships exactly one interpreter and
#: the version it ships is a release decision. 3.13 is the middle of that range.
DEFAULT_VERSION = "3.13"

#: What the built-in fetcher downloads when uv is not there to ask. uv floats to the newest
#: patch of `DEFAULT_VERSION`; this cannot, because without uv nothing here knows what the
#: newest one is -- so it is pinned, and bumped by hand: pick a release, copy the six
#: `install_only_stripped` lines out of its `SHA256SUMS`. The hashes live here rather than
#: being fetched beside the tarball because a checksum from the same server as the file
#: only catches a truncated download, and because a `file://` mirror should need nothing
#: but the tarball someone carried in.
PBS_RELEASE = "20260901"
PBS_PYTHON = "3.13.15"
PBS_DOWNLOAD = "https://github.com/astral-sh/python-build-standalone/releases/download"
PBS_SHA256 = {
    "aarch64-apple-darwin": "d3904bd6a072246e07aa0bdadee9a14e80521e42a943c0848059feb16a2816dc",
    "x86_64-apple-darwin": "f712a9143c8a5d248438ec7921a0b48d548bca4f1337d33c690d28c2d0504137",
    "aarch64-unknown-linux-gnu": "01ce0ce9189feaead3298abf10d4efe998c55a489b3d5d38ca4f83dda7e7977e",
    "x86_64-unknown-linux-gnu": "8a689a077337bea6d1c4bc0b7df1d52fcaa28f5f67e50df8bf417c1e3f9d8874",
    "aarch64-pc-windows-msvc": "8b31e1ddae9ebd339eae0549049546aa54484dbf92c81694b1e7510fee869413",
    "x86_64-pc-windows-msvc": "63d263ab0162f34a241a56dc5b283c22d6e131f5516117e6a921350c69ba7d4f",
}

#: What the daemon needs to keep working, spelled out so a future strip cannot quietly
#: remove one. `_ssl` is reached by any backend spec that fetches; `sqlite3`, `lzma` and
#: `ctypes` are reached by the stdlib's own startup and by ruamel's fallbacks. The runtime
#: dependency list is two names long, and that is exactly why this list is not derived
#: from it.
REQUIRED_MODULES = (
    "mcp_gateway",
    "mcp_gateway.cli",
    "mcp_gateway_ui",
    "websockets",
    "ruamel.yaml",
    "ssl",
    "sqlite3",
    "lzma",
    "ctypes",
    "asyncio",
)

#: Removed from the interpreter tree, as glob patterns relative to its root. Every entry is
#: either a developer tool, a build-time artifact, or a GUI toolkit -- nothing here is
#: reachable from a daemon that serves WebSockets and spawns subprocesses.
#:
#: Two lists because the trees are two different shapes, not because the *policy* differs:
#: python-build-standalone lays Unix out as `bin/python3` + `lib/python3.13/`, and Windows
#: as `python.exe` + `Lib/` + `DLLs/` at the root. Every entry below has a counterpart in
#: the other list, and a comment is written once, above the pair.
#:
#: `lib/libpython3.*.dylib` (and `python3*.dll` on Windows) is deliberately **absent**:
#: the interpreter links against it.
UNIX_STRIP_GLOBS = (
    # Developer tools and the REPL's furniture.
    "lib/python3.*/idlelib",
    "lib/python3.*/turtledemo",
    "lib/python3.*/pydoc_data",
    "lib/python3.*/test",
    "lib/python3.*/lib2to3",
    "bin/idle3*",
    "bin/pydoc3*",
    "bin/2to3*",
    # Tk. `tkinter` cannot be imported without it, and nothing here imports tkinter.
    "lib/python3.*/tkinter",
    "lib/python3.*/lib-dynload/_tkinter*.so",
    "lib/tcl*",
    "lib/tk*",
    "lib/libtcl*",
    "lib/libtk*",
    "lib/itcl*",
    "lib/thread*",
    "lib/sqlite3",          # Tcl's sqlite binding, not the `sqlite3` stdlib module
    # Installers. Nothing installs into this tree after this script runs, and leaving pip
    # in a shipped app invites someone to try.
    "lib/python3.*/ensurepip",
    "lib/python3.*/site-packages/pip",
    "lib/python3.*/site-packages/pip-*",
    "lib/python3.*/site-packages/setuptools",
    "lib/python3.*/site-packages/setuptools-*",
    "lib/python3.*/site-packages/pkg_resources",
    "lib/python3.*/site-packages/_distutils_hack",
    "bin/pip*",
    # Build-time support for linking *against* this interpreter. We only run it.
    "lib/python3.*/config-*",
    "lib/pkgconfig",
    "include",
    "bin/python*-config",
    "share/man",
)

WINDOWS_STRIP_GLOBS = (
    # Developer tools and the REPL's furniture.
    "Lib/idlelib",
    "Lib/turtledemo",
    "Lib/pydoc_data",
    "Lib/test",
    "Lib/lib2to3",
    "Scripts/idle*",
    "Scripts/pydoc*",
    "Scripts/2to3*",
    # Tk.
    "Lib/tkinter",
    "DLLs/_tkinter.pyd",
    "DLLs/tcl*.dll",
    "DLLs/tk*.dll",
    "tcl",
    # Installers.
    "Lib/ensurepip",
    "Lib/site-packages/pip",
    "Lib/site-packages/pip-*",
    "Lib/site-packages/setuptools",
    "Lib/site-packages/setuptools-*",
    "Lib/site-packages/pkg_resources",
    "Lib/site-packages/_distutils_hack",
    "Scripts/pip*",
    # Build-time support for linking *against* this interpreter. `libs/` is the import
    # library an extension would build against; `python.exe` does not need it to run.
    "include",
    "libs",
)


def is_windows() -> bool:
    """Whether this build targets Windows. Host-only, like everything else here."""
    return os.name == "nt"


def strip_globs() -> tuple[str, ...]:
    return WINDOWS_STRIP_GLOBS if is_windows() else UNIX_STRIP_GLOBS


def interpreter_path(root: Path) -> Path:
    """The interpreter inside a bundle.

    **Twinned with `Layout::interpreter` in `src/desktop/src-tauri/src/supervisor.rs`**, which
    has to name the same two paths from the other side. If one of them is wrong the app
    starts nothing at all, with a dialog that says nothing useful -- which is why the Rust
    side has a unit test asserting both spellings and this script verifies the real file.
    """
    return root / "python.exe" if is_windows() else root / "bin" / "python3"


def stdlib_path(root: Path) -> Path:
    """The standard library directory: `Lib/` on Windows, `lib/python3.N/` elsewhere."""
    return root / "Lib" if is_windows() else next(root.glob("lib/python3.*"))


def log(message: str) -> None:
    """Progress on stderr, so stdout stays free for a future `--print-path`."""
    print(f"bundle_python: {message}", file=sys.stderr)


def run(argv: list[str], check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a command, showing it first. A failure raises unless `check` says otherwise."""
    log(f"$ {' '.join(str(a) for a in argv)}")
    return subprocess.run(argv, check=check, text=True, **kwargs)  # type: ignore[arg-type]


def uv_environment() -> dict[str, str]:
    """uv's environment, with a cache directory it is allowed to write.

    uv's default cache is under `~/.cache/uv`, which a sandboxed or restricted home may
    refuse -- and uv treats an unwritable cache as fatal rather than as a reason to skip
    caching. Pointing it at the build's own temporary area costs a cold download and makes
    the script run anywhere. An explicit `UV_CACHE_DIR` in the environment still wins.
    """
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "mcp-gateway-uv-cache"))
    return env


def host_platform() -> tuple[str, str]:
    """This machine as `(system, arch)`: `macos`/`linux`/`windows` and `aarch64`/`x86_64`.

    Host-only, deliberately. A cross-built bundle would need a cross-built Tauri binary to
    sit beside it, and Tauri is happiest building for the machine it is on -- so
    `publish-artifacts.yml` runs one runner per platform and each builds its own, rather
    than one runner trying to produce four.
    """
    machine = platform.machine()
    # Case-folded, and `AMD64` spelled out: that is what `platform.machine()` returns on
    # 64-bit Windows, and `ARM64` on Windows on ARM -- neither of which is how any other
    # platform spells the same processor. Getting this wrong is not a subtle failure, it is
    # every Windows build refusing to start.
    arch = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine.lower())
    if arch is None:
        raise SystemExit(f"bundle_python: unsupported architecture {machine!r}")
    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if system is None:
        raise SystemExit(f"bundle_python: unsupported platform {platform.system()!r}")
    return system, arch


def uv_python_tag(version: str) -> str:
    """The name `uv python install` knows this machine's interpreter by."""
    system, arch = host_platform()
    suffix = "none" if system != "linux" else "gnu"
    return f"cpython-{version}-{system}-{arch}-{suffix}"


def pbs_triple() -> str:
    """The target triple python-build-standalone names its release assets with.

    The same machine as `uv_python_tag`, spelled the way the release page spells it --
    which is Rust's triple, not uv's key.
    """
    system, arch = host_platform()
    vendor_os = {
        "macos": "apple-darwin",
        "linux": "unknown-linux-gnu",
        "windows": "pc-windows-msvc",
    }[system]
    return f"{arch}-{vendor_os}"


def pbs_asset(triple: str) -> str:
    """The `install_only_stripped` tarball for `triple` in the pinned release."""
    return f"cpython-{PBS_PYTHON}+{PBS_RELEASE}-{triple}-install_only_stripped.tar.gz"


def pbs_url(asset: str) -> str:
    """Where `asset` is downloaded from, honouring the same mirror variable uv does.

    `UV_PYTHON_INSTALL_MIRROR` replaces the `.../releases/download` prefix for uv, and it
    replaces exactly the same prefix here, so the offline recipe in `src/desktop/README.md`
    -- a `file://` directory laid out `<release>/<asset>` -- is one variable for both
    fetchers rather than one each.
    """
    base = os.environ.get("UV_PYTHON_INSTALL_MIRROR") or PBS_DOWNLOAD
    return f"{base.rstrip('/')}/{PBS_RELEASE}/{urllib.parse.quote(asset)}"


def newest_wheel() -> Path:
    """The most recently built wheel in `dist/`, which is what `make build` leaves."""
    wheels = sorted(
        (REPO_ROOT / "dist").glob("python_mcp_gateway-*.whl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not wheels:
        raise SystemExit("bundle_python: no wheel in dist/ -- run `make build` first")
    return wheels[-1]


def choose_installed(staging: Path, tag: str) -> Path:
    """The one real interpreter directory uv left in `staging`.

    uv installs into a *versioned* directory -- `cpython-3.13.15-windows-x86_64-none` -- and
    drops the major.minor name beside it as a link to that. Which *kind* of link is the
    platform difference this function exists for:

    * On Unix it is a symlink, and skipping symlinks is enough.
    * **On Windows it is a directory junction**, and Python deliberately reports a junction
      as not a symlink -- `Path.is_symlink()` is `False` for one. So the filter that works
      everywhere else saw two directories on Windows and this step refused to guess between
      them, which is every Windows build failing at its first minute.

    So candidates are deduplicated by what they *resolve to*, which collapses a junction and
    a symlink alike onto the directory they point at. The `tag` fallback behind it is for the
    case neither covers -- a uv that one day copies rather than links -- where the alias is
    still the entry named exactly what we asked for, and the real one is the entry carrying
    the patch version.
    """
    by_target: dict[Path, Path] = {}
    for entry in sorted(staging.iterdir()):
        # uv leaves its own `.temp` and `.lock` beside what it installed.
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        by_target.setdefault(entry.resolve(), entry)

    candidates = sorted(by_target)
    if len(candidates) > 1:
        candidates = [p for p in candidates if p.name != tag] or candidates
    if len(candidates) != 1:
        raise SystemExit(
            f"bundle_python: expected one interpreter in {staging}, found {candidates}"
        )
    return candidates[0]


def interpreter_root(candidate: Path) -> Path | None:
    """The interpreter tree inside `candidate`, or `None` if there is not one.

    python-build-standalone's `install_only` tarballs unpack to a single `python/` directory,
    so someone who has just unpacked one by hand holds either that directory or the thing
    containing it -- and which one they hold depends on how they unpacked it, not on anything
    they chose. Both are accepted. Guessing right here is the difference between a flag that
    works the first time and a flag with its own troubleshooting section.
    """
    for root in (candidate, candidate / "python"):
        if interpreter_path(root).exists():
            return root
    return None


def adopt_interpreter(out: Path, source: Path) -> Path:
    """Install an interpreter fetched by hand, instead of asking uv for one.

    The download in `fetch_interpreter` is the single step of this script that a corporate
    firewall reliably breaks; everything after it is a local wheel, local file removal and
    local subprocesses. uv's own escape hatches come first and cost no code --
    `UV_PYTHON_INSTALL_MIRROR` accepts a `file://` directory and `UV_NATIVE_TLS=1` fixes the
    intercepted-TLS case, both documented in `src/desktop/README.md`. This flag is for the
    case underneath those: a machine where uv cannot fetch at all, handed a tarball that
    someone carried in.

    What arrives is **copied, not moved**. A tarball fetched once, by hand, over a link that
    made it hard has to survive a build that fails at `verify`. Symlinks are preserved
    because `bin/python3` is one, and a copy that resolved it would ship two interpreters
    whose halves can drift.

    Nothing downstream is special-cased: the adopted tree goes through the same `unmanage`,
    `install_gateway`, `strip`, `compile_bytecode` and `verify` as a fetched one. `verify` is
    the gate, so a hand-fetched interpreter is held to exactly the standard a fetched one is.
    """
    staging: Path | None = None
    try:
        if source.is_file():
            if not source.name.endswith((".tar.gz", ".tgz")):
                raise SystemExit(
                    f"bundle_python: {source.name} is not a .tar.gz -- python-build-standalone's\n"
                    "  `install_only` assets are, and its `.tar.zst` ones need a zstd this script\n"
                    "  does not carry. Unpack that one yourself and pass the directory."
                )
            staging = Path(tempfile.mkdtemp(prefix="bundle_python-"))
            log(f"unpacking {source.name}")
            shutil.unpack_archive(str(source), str(staging))
            tree = interpreter_root(staging)
        elif source.is_dir():
            tree = interpreter_root(source)
        else:
            raise SystemExit(f"bundle_python: no such interpreter: {source}")

        if tree is None:
            expected = interpreter_path(Path(".")).as_posix()
            raise SystemExit(
                f"bundle_python: {source} has no {expected} in it -- expected an unpacked\n"
                "  python-build-standalone tree, or the directory its `python/` sits in."
            )

        # Checked before the `rmtree` below, which would otherwise delete the source along
        # with the destination and leave a person holding neither.
        if out == tree or out in tree.parents:
            raise SystemExit(f"bundle_python: --interpreter {source} is inside --out {out}")

        shutil.rmtree(out, ignore_errors=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tree, out, symlinks=True)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    log(f"adopted {source}")
    return out


def fetch_interpreter(out: Path, version: str) -> Path:
    """Install a standalone CPython at `out`, replacing whatever was there.

    uv installs into a *versioned* directory under `--install-dir` and also drops a
    major.minor link beside it. Neither name is what the bundle wants, so the real directory
    is moved to `out` -- see `choose_installed` for how it is picked out -- and the staging
    area is discarded: the Rust side then names `python/bin/python3` and keeps naming it
    across patch releases.
    """
    tag = uv_python_tag(version)
    if shutil.which("uv") is None:
        raise SystemExit(
            "bundle_python: uv is not installed, and it is how the interpreter is fetched.\n"
            "  https://docs.astral.sh/uv/getting-started/installation/"
        )

    staging = out.parent / f".{out.name}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    # `--no-bin` because a build must not touch the developer's `~/.local/bin`. Without it
    # uv tries to symlink the interpreter onto PATH and warns when it cannot -- a warning
    # that reads like a failure in the middle of an otherwise clean build.
    run(
        ["uv", "python", "install", "--no-bin", "--install-dir", str(staging), tag],
        env=uv_environment(),
    )

    installed = choose_installed(staging, tag)

    shutil.rmtree(out, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The resolved directory, so what moves is the interpreter and not a link to it. The
    # alias left behind dangles, and the `rmtree` below removes it -- `shutil.rmtree` does
    # not follow a junction or a symlink, so it takes the link and nothing it pointed at.
    shutil.move(str(installed), str(out))
    shutil.rmtree(staging, ignore_errors=True)
    return out


def fetch_interpreter_builtin(out: Path) -> str:
    """Download the pinned standalone CPython without uv, and adopt it at `out`.

    For the machine that has no uv and should not need one to build the app: the stdlib's
    `urllib` fetches the tarball, the pinned hash is checked before anything is unpacked, and
    `adopt_interpreter` takes it from there exactly as it takes a tarball carried in by hand.
    TLS goes through the default context, so an intercepting proxy is `SSL_CERT_FILE`, the
    variable every Python program already reads -- there is deliberately no way to turn
    verification off. Returns the asset name, which is what the manifest records as the tag.
    """
    triple = pbs_triple()
    expected = PBS_SHA256.get(triple)
    if expected is None:
        raise SystemExit(f"bundle_python: no pinned python-build-standalone hash for {triple}")
    asset = pbs_asset(triple)
    url = pbs_url(asset)

    with tempfile.TemporaryDirectory(prefix="bundle_python-") as scratch:
        tarball = Path(scratch) / asset
        log(f"downloading {url}")
        digest = hashlib.sha256()
        received = 0
        with urllib.request.urlopen(url, timeout=60) as response, tarball.open("wb") as sink:
            served_from = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            length = response.headers.get("Content-Length")
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                sink.write(chunk)
                received += len(chunk)
        if digest.hexdigest() != expected:
            # Kept, so the thing that was refused can be looked at: `file` and `head` on it
            # settle "proxy page or truncated download" in a second.
            kept = Path(tempfile.gettempdir()) / f"bundle_python-rejected-{asset}"
            shutil.copyfile(tarball, kept)
            raise SystemExit(mismatch_message(
                asset=asset,
                expected=expected,
                actual=digest.hexdigest(),
                received=received,
                length=int(length) if length and length.isdigit() else None,
                content_type=content_type,
                served_from=served_from,
                head=tarball.read_bytes()[:2],
                kept=kept,
            ))
        log(f"sha256 ok: {asset}")
        adopt_interpreter(out, tarball)
    return asset


def mismatch_message(
    *,
    asset: str,
    expected: str,
    actual: str,
    received: int,
    length: int | None,
    content_type: str,
    served_from: str,
    head: bytes,
    kept: Path,
) -> str:
    """Why a download failed its hash, in the order that is actually likely.

    A wrong hash is almost never a tampered release. It is a download that stopped early, or
    a proxy, captive portal or SSO wall that answered with a page of HTML -- and "sha256
    mismatch" alone reads as a bad pin, which sends a person to the wrong fix. So the checks
    that tell those apart come first, and the hash is the last thing said.
    """
    lines = [f"bundle_python: {asset} failed its sha256 check. Refusing to unpack it."]
    if length is not None and received < length:
        lines.append(
            f"  The download stopped early: {received:,} of {length:,} bytes. "
            "Retry, or fetch it by hand and pass --interpreter."
        )
    elif head != b"\x1f\x8b":
        lines.append(
            f"  What arrived is not a gzip archive ({received:,} bytes, "
            f"Content-Type {content_type or 'unset'!r}). A proxy, captive portal or SSO page "
            "answered instead of the release host."
        )
    else:
        lines.append(
            f"  A complete gzip file of {received:,} bytes arrived, but not the pinned one. "
            "A mirror serving a different build, or a pin that is wrong for this platform."
        )
    lines += [
        f"  served from: {served_from}",
        f"  expected:    {expected}",
        f"  got:         {actual}",
        f"  kept at:     {kept}",
    ]
    return "\n".join(lines)


def resolve_fetcher(requested: str) -> str:
    """`auto` is uv when uv is on PATH -- today's path, unchanged -- and `builtin` when not."""
    if requested != "auto":
        return requested
    return "uv" if shutil.which("uv") is not None else "builtin"


def unmanage(root: Path) -> None:
    """Drop the marker that stops anything installing here. See the module docstring."""
    # `rglob` rather than the stdlib path, because the marker sits in the stdlib directory
    # on Unix and beside it on Windows, and there is only ever one of them in the tree.
    for marker in root.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()
        log(f"removed {marker.relative_to(root)}")


def install_gateway(root: Path, wheel: Path) -> None:
    """Install the wheel and its two pinned dependencies into the bundled interpreter.

    Through uv when there is one, and otherwise through the interpreter's own pip -- which
    python-build-standalone ships, and which `strip` removes again straight afterwards, so
    either way the app leaves here with no installer in it.
    """
    interpreter = interpreter_path(root)
    if shutil.which("uv") is None:
        install_with_pip(interpreter, wheel)
        return

    argv = ["uv", "pip", "install", "--python", str(interpreter), "--system", str(wheel)]

    # Mirrors the escape hatch the Makefile documents for pip: behind a TLS-intercepting
    # proxy, the certifi bundle does not carry the interceptor's CA, and the download fails
    # with an error that names TLS rather than the proxy. Honouring the same environment
    # variable means one thing to set, not two.
    for host in os.environ.get("PIP_TRUSTED_HOST", "").split():
        argv.extend(["--allow-insecure-host", host])

    run(argv, env=uv_environment())


def install_with_pip(interpreter: Path, wheel: Path) -> None:
    """The uv-less install. pip reads `PIP_INDEX_URL` and `PIP_TRUSTED_HOST` itself.

    An adopted tree that someone already stripped may have no pip; `ensurepip` puts one
    back from the wheel the stdlib carries, offline, and is itself stripped afterwards.
    """
    probe = run([str(interpreter), "-m", "pip", "--version"], check=False, capture_output=True)
    if probe.returncode != 0:
        run([str(interpreter), "-m", "ensurepip", "--default-pip"])
    run([
        str(interpreter), "-m", "pip", "install",
        "--disable-pip-version-check", "--no-warn-script-location", "--progress-bar", "off",
        str(wheel),
    ])


def strip(root: Path) -> list[str]:
    """Remove what a daemon never reaches. Returns what was actually removed."""
    removed: list[str] = []
    for pattern in strip_globs():
        for path in sorted(root.glob(pattern)):
            removed.append(str(path.relative_to(root)))
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()

    # After the strip, not before: removing a package leaves its parent's `__pycache__`
    # holding entries for files that no longer exist, and `compileall` below writes the
    # ones that should be there.
    for cache in root.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)
    return removed


def compile_bytecode(root: Path) -> None:
    """Pre-compile, so a read-only bundle is not also a slow one. See the module docstring."""
    lib = stdlib_path(root)
    run(
        [
            str(interpreter_path(root)), "-m", "compileall",
            "-q", "-f", "--invalidation-mode", "unchecked-hash", str(lib),
        ],
        # compileall exits non-zero when *any* file fails to compile, and a standalone
        # distribution carries a few deliberately-unparseable fixtures. The verification
        # step below is the real gate; this one is an optimisation.
        check=False,
    )


def verify(root: Path, config: Path) -> None:
    """Refuse to hand back a bundle that does not run. This is the gate, not a courtesy.

    A `.app` that ships a broken interpreter fails at launch, in front of a user, with a
    dialog that says nothing useful. Everything here is cheap and every one of it has a
    plausible way to break: a strip that went one directory too far, a wheel that did not
    install, an interpreter that did not survive being moved.
    """
    interpreter = interpreter_path(root)
    if not interpreter.exists():
        raise SystemExit(f"bundle_python: no interpreter at {interpreter}")

    imports = "; ".join(f"import {name}" for name in REQUIRED_MODULES)
    run([str(interpreter), "-c", imports])

    # The assets `webui.py` serves, found the way `webui.py` finds them. A wheel that
    # installed without its package data passes every import above and then 404s the UI.
    run([
        str(interpreter), "-c",
        "import sys;"
        "from importlib import resources;"
        "import mcp_gateway.webui as w;"
        "missing=[n for n in w.ASSETS "
        "if not (resources.files(w.ASSET_PACKAGE)/n).is_file()];"
        "sys.exit(f'missing UI assets: {missing}' if missing else 0)",
    ])

    run([str(interpreter), "-m", "mcp_gateway.cli", "--version"])
    run([str(interpreter), "-m", "mcp_gateway.cli", "--check", "--config", str(config)])


def write_manifest(
    root: Path,
    wheel: Path,
    version: str | None,
    removed: list[str],
    source: str,
    tag: str | None,
) -> dict[str, object]:
    """Record what this bundle is, so a bug report can name it.

    The shell logs this at startup. "Which Python, which wheel" is the first question about
    a bundle that misbehaves, and the answer must not require unpacking the `.app`.

    `source` is the third question, and it arrived with `--interpreter`: an adopted tree is
    whatever someone downloaded, so "which uv tag did this ask for" has no answer and
    `python_requested` and `tag` are null rather than repeating a default nobody used. The
    built-in fetcher records `builtin`, and its `tag` is the exact asset it downloaded.
    `python` is read out of the interpreter either way, and is the field to trust.
    """
    interpreter = interpreter_path(root)
    reported = subprocess.run(
        [str(interpreter), "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    manifest = {
        "python": reported,
        "python_requested": version,
        "tag": tag,
        "source": source,
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stripped": removed,
    }
    (root / "BUNDLE.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def megabytes(path: Path) -> int:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    return round(total / 1_000_000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the standalone interpreter the Tauri shell ships.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Where the bundle lands.")
    parser.add_argument(
        "--python-version",
        default=DEFAULT_VERSION,
        help=(
            "CPython major.minor. Unused with --interpreter, which brings its own, and "
            "fixed at PBS_PYTHON's for the built-in fetcher."
        ),
    )
    parser.add_argument("--wheel", type=Path, help="Wheel to install (default: newest in dist/).")
    parser.add_argument(
        "--interpreter",
        type=Path,
        help=(
            "Adopt an already-fetched standalone CPython -- an unpacked tree or a .tar.gz -- "
            "instead of downloading one with uv. For builds behind a firewall; see "
            "src/desktop/README.md."
        ),
    )
    parser.add_argument(
        "--fetcher",
        choices=("auto", "uv", "builtin"),
        default="auto",
        help=(
            "How the interpreter is downloaded: uv, or the stdlib with a pinned release and "
            "hash. `auto` (the default) is uv when it is on PATH. Ignored with --interpreter."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "src" / "desktop" / "src-tauri" / "seed" / "servers.yaml",
        help="Config the verification step runs `--check` against.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Re-run the checks against an existing bundle. Fetches and installs nothing.",
    )
    args = parser.parse_args(argv)

    out = args.out.resolve()

    if args.verify_only:
        verify(out, args.config)
        log(f"{out} verifies")
        return 0

    wheel = (args.wheel or newest_wheel()).resolve()
    log(f"wheel: {wheel.name}")

    if args.interpreter is not None:
        source = args.interpreter.resolve()
        adopt_interpreter(out, source)
        requested, recorded = None, str(source)
        tag = None
    elif resolve_fetcher(args.fetcher) == "uv":
        fetch_interpreter(out, args.python_version)
        requested, recorded = args.python_version, "uv"
        tag = uv_python_tag(requested)
    else:
        # The pin is one interpreter, so a different major.minor is a refusal, not a
        # silent substitution of the one we happen to have a hash for.
        if not PBS_PYTHON.startswith(f"{args.python_version}."):
            raise SystemExit(
                f"bundle_python: the built-in fetcher is pinned to {PBS_PYTHON}; "
                f"--python-version {args.python_version} needs uv"
            )
        tag = fetch_interpreter_builtin(out)
        requested, recorded = args.python_version, "builtin"

    unmanage(out)
    install_gateway(out, wheel)
    removed = strip(out)
    log(f"stripped {len(removed)} paths")
    compile_bytecode(out)
    verify(out, args.config)
    manifest = write_manifest(out, wheel, requested, removed, recorded, tag)

    log(f"Python {manifest['python']} + {wheel.name} -> {out} ({megabytes(out)} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
