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
`webui.py` depends on: `importlib.resources` finding `mcp_gateway/ui/*.js` inside the bundle
exactly as it finds them in a checkout. Here that keeps working because the layout is a
real interpreter with a real `site-packages`, and `cli.py` in the shipped app is still a file
a person can open when a user reports something.

## The interpreter

python-build-standalone, fetched through `uv python install`. uv's managed builds *are*
python-build-standalone, so using uv is not a second dependency on a second thing -- it is
the same artifact with a downloader that already handles the platform triple, and one this
project's contributors have installed anyway.

Two edits are made to what uv hands over:

* **`EXTERNALLY-MANAGED` is removed.** It exists to stop a person mutating uv's shared copy
  of an interpreter. This is not that copy: it is a private tree that is about to be sealed
  inside a `.app`, and installing the gateway into it is the entire point.
* **The versioned directory name is flattened.** uv installs to
  `cpython-3.13.15-macos-aarch64-none/`; the bundle wants `python/`, so that the Rust side
  names one path that does not move when a patch release lands.

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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the bundle lands. `tauri.conf.json` names the same path in `bundle.resources`, and
#: `supervisor.rs` resolves `python/bin/python3` under the app's resource dir.
DEFAULT_OUT = REPO_ROOT / "desktop" / "src-tauri" / "resources" / "python"

#: Pinned rather than floated. `requires-python` allows 3.12 through 3.14, which is a
#: statement about what the *library* supports; a bundle ships exactly one interpreter and
#: the version it ships is a release decision. 3.13 is the middle of that range.
DEFAULT_VERSION = "3.13"

#: What the daemon needs to keep working, spelled out so a future strip cannot quietly
#: remove one. `_ssl` is reached by any backend spec that fetches; `sqlite3`, `lzma` and
#: `ctypes` are reached by the stdlib's own startup and by ruamel's fallbacks. The runtime
#: dependency list is two names long, and that is exactly why this list is not derived
#: from it.
REQUIRED_MODULES = (
    "mcp_gateway",
    "mcp_gateway.cli",
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
#: `lib/libpython3.*.dylib` is deliberately **absent**: `bin/python3` links against it.
STRIP_GLOBS = (
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


def uv_python_tag(version: str) -> str:
    """The python-build-standalone triple for this machine.

    Host-only, deliberately: a cross-built bundle needs a cross-built Tauri binary to sit
    beside it, and that is the cross-platform-CI bead, not this one.
    """
    machine = platform.machine()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(machine)
    if arch is None:
        raise SystemExit(f"bundle_python: unsupported architecture {machine!r}")
    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if system is None:
        raise SystemExit(f"bundle_python: unsupported platform {platform.system()!r}")
    suffix = "none" if system != "linux" else "gnu"
    return f"cpython-{version}-{system}-{arch}-{suffix}"


def newest_wheel() -> Path:
    """The most recently built wheel in `dist/`, which is what `make build` leaves."""
    wheels = sorted(
        (REPO_ROOT / "dist").glob("python_mcp_gateway-*.whl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not wheels:
        raise SystemExit("bundle_python: no wheel in dist/ -- run `make build` first")
    return wheels[-1]


def fetch_interpreter(out: Path, version: str) -> Path:
    """Install a standalone CPython at `out`, replacing whatever was there.

    uv installs into a *versioned* directory under `--install-dir` and also drops a
    major.minor symlink beside it. Neither name is what the bundle wants, so the real
    directory is moved to `out` and the staging area is discarded: the Rust side then names
    `python/bin/python3` and keeps naming it across patch releases.
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

    # uv leaves its own `.temp` and `.lock` beside what it installed, and the major.minor
    # name is a symlink to the versioned one. What is wanted is the single real directory.
    installed = [
        p for p in staging.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    ]
    if len(installed) != 1:
        raise SystemExit(f"bundle_python: expected one interpreter in {staging}, found {installed}")

    shutil.rmtree(out, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(installed[0]), str(out))
    shutil.rmtree(staging, ignore_errors=True)
    return out


def unmanage(root: Path) -> None:
    """Drop the marker that stops anything installing here. See the module docstring."""
    for marker in root.glob("lib/python3.*/EXTERNALLY-MANAGED"):
        marker.unlink()
        log(f"removed {marker.relative_to(root)}")


def install_gateway(root: Path, wheel: Path) -> None:
    """Install the wheel and its two pinned dependencies into the bundled interpreter."""
    interpreter = root / "bin" / "python3"
    argv = ["uv", "pip", "install", "--python", str(interpreter), "--system", str(wheel)]

    # Mirrors the escape hatch the Makefile documents for pip: behind a TLS-intercepting
    # proxy, the certifi bundle does not carry the interceptor's CA, and the download fails
    # with an error that names TLS rather than the proxy. Honouring the same environment
    # variable means one thing to set, not two.
    for host in os.environ.get("PIP_TRUSTED_HOST", "").split():
        argv.extend(["--allow-insecure-host", host])

    run(argv, env=uv_environment())


def strip(root: Path) -> list[str]:
    """Remove what a daemon never reaches. Returns what was actually removed."""
    removed: list[str] = []
    for pattern in STRIP_GLOBS:
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
    lib = next(root.glob("lib/python3.*"))
    run(
        [
            str(root / "bin" / "python3"), "-m", "compileall",
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
    interpreter = root / "bin" / "python3"
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


def write_manifest(root: Path, wheel: Path, version: str, removed: list[str]) -> dict[str, object]:
    """Record what this bundle is, so a bug report can name it.

    The shell logs this at startup. "Which Python, which wheel" is the first question about
    a bundle that misbehaves, and the answer must not require unpacking the `.app`.
    """
    interpreter = root / "bin" / "python3"
    reported = subprocess.run(
        [str(interpreter), "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    manifest = {
        "python": reported,
        "python_requested": version,
        "tag": uv_python_tag(version),
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
    parser.add_argument("--python-version", default=DEFAULT_VERSION, help="CPython major.minor.")
    parser.add_argument("--wheel", type=Path, help="Wheel to install (default: newest in dist/).")
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "desktop" / "src-tauri" / "seed" / "servers.yaml",
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

    fetch_interpreter(out, args.python_version)
    unmanage(out)
    install_gateway(out, wheel)
    removed = strip(out)
    log(f"stripped {len(removed)} paths")
    compile_bytecode(out)
    verify(out, args.config)
    manifest = write_manifest(out, wheel, args.python_version, removed)

    log(f"Python {manifest['python']} + {wheel.name} -> {out} ({megabytes(out)} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
