"""`scripts/bundle_python.py`, minus the download.

The script's real work is a fetch, an install and a subprocess smoke test, none of which
belong in a unit suite -- `make tauri-python` is what exercises those, and its verification
step is the gate that matters. What is here is the part that is pure, easy to get wrong, and
expensive to get wrong: the strip list.

A strip list is a list of things nobody will notice are gone until a user does. `tkinter`
going missing is invisible; `_ssl` going missing is a backend that cannot reach GitHub, three
weeks later, on somebody else's machine. So the patterns are asserted against the module's
own statement of what must survive.
"""

from __future__ import annotations

import importlib.util
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_script():
    """Import the script by path. `scripts/` is not a package, and should not become one."""
    spec = importlib.util.spec_from_file_location(
        "bundle_python", REPO_ROOT / "scripts" / "bundle_python.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bundle_python = load_script()


def stripped(path: str) -> bool:
    """Whether `path`, relative to the interpreter root, matches any strip pattern."""
    return any(fnmatch(path, pattern) or path.startswith(f"{pattern.rstrip('/')}/")
               for pattern in bundle_python.STRIP_GLOBS) or any(
        fnmatch(path, f"{pattern}/*") for pattern in bundle_python.STRIP_GLOBS
    )


# --- what must survive ---------------------------------------------------------------------


SURVIVORS = (
    # The interpreter itself, and the library it links against. Removing the dylib is the
    # one strip that turns the bundle into a file that cannot start at all.
    "bin/python3",
    "lib/libpython3.13.dylib",
    # The daemon, and what it imports.
    "lib/python3.13/site-packages/mcp_gateway/cli.py",
    "lib/python3.13/site-packages/mcp_gateway/webui.py",
    "lib/python3.13/site-packages/websockets/__init__.py",
    "lib/python3.13/site-packages/ruamel/yaml/__init__.py",
    # The UI assets `webui.py` reads through `importlib.resources`. A strip that took these
    # would leave a bundle that starts, serves, and 404s its own page.
    "lib/python3.13/site-packages/mcp_gateway/ui/app.js",
    "lib/python3.13/site-packages/mcp_gateway/ui/index.html",
    # Extension modules the two-name dependency list does not mention and something still
    # reaches. See REQUIRED_MODULES in the script.
    "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so",
    "lib/python3.13/lib-dynload/_sqlite3.cpython-313-darwin.so",
    "lib/python3.13/lib-dynload/_lzma.cpython-313-darwin.so",
    "lib/python3.13/lib-dynload/_ctypes.cpython-313-darwin.so",
    "lib/python3.13/ssl.py",
    "lib/python3.13/asyncio/__init__.py",
    "lib/python3.13/sqlite3/__init__.py",
)


@pytest.mark.parametrize("path", SURVIVORS)
def test_the_strip_list_spares_what_the_daemon_needs(path: str) -> None:
    assert not stripped(path), f"{path} matches a strip pattern"


CASUALTIES = (
    "lib/python3.13/idlelib/__init__.py",
    "lib/python3.13/tkinter/__init__.py",
    "lib/python3.13/turtledemo/__init__.py",
    "lib/python3.13/ensurepip/__init__.py",
    "lib/python3.13/config-3.13-darwin/libpython3.13.a",
    "lib/python3.13/site-packages/pip/__init__.py",
    "lib/tcl9.0/init.tcl",
    "lib/libtcl9.0.dylib",
    "include/python3.13/Python.h",
    "bin/idle3.13",
    "bin/pip3",
    "share/man/man1/python3.1",
)


@pytest.mark.parametrize("path", CASUALTIES)
def test_the_strip_list_removes_what_it_claims_to(path: str) -> None:
    assert stripped(path), f"{path} matches no strip pattern"


def test_the_dylib_is_never_a_casualty_of_the_static_library_rule() -> None:
    """`libpython3.13.a` goes and `libpython3.13.dylib` stays. One character apart."""
    assert stripped("lib/python3.13/config-3.13-darwin/libpython3.13.a")
    assert not stripped("lib/libpython3.13.dylib")


# --- the platform triple -------------------------------------------------------------------


def test_the_triple_names_this_machine(monkeypatch) -> None:
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(bundle_python.platform, "system", lambda: "Darwin")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-macos-aarch64-none"

    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "x86_64")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-macos-x86_64-none"

    # Linux's standalone builds are `-gnu`, not `-none`. Cross-platform bundling is its own
    # bead; getting the suffix right here costs nothing and makes that bead smaller.
    monkeypatch.setattr(bundle_python.platform, "system", lambda: "Linux")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-linux-x86_64-gnu"


def test_an_architecture_we_cannot_name_is_refused_rather_than_guessed(monkeypatch) -> None:
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "sparc")
    with pytest.raises(SystemExit, match="sparc"):
        bundle_python.uv_python_tag("3.13")


# --- the version it pins -------------------------------------------------------------------


def test_the_bundled_version_is_one_the_project_supports() -> None:
    """`requires-python` is what the library supports; a bundle ships exactly one of them."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.12,<3.15"' in pyproject, "the supported range moved"
    major, minor = bundle_python.DEFAULT_VERSION.split(".")
    assert (int(major), int(minor)) >= (3, 12)
    assert (int(major), int(minor)) < (3, 15)


# --- against a bundle that was actually built ------------------------------------------------
#
# The tests above reason about the patterns with `fnmatch`, which is not what the script
# uses -- `Path.glob` does not let `*` cross a directory separator and `fnmatch` does. The
# two agree on every pattern here, but "agree today" is not a property worth trusting, so
# when a real bundle is on disk it gets asked directly. Skipped otherwise: a checkout that
# has never run `make tauri-python` stays green, the same way `tests/ui/` skips without node.

BUNDLE = REPO_ROOT / "desktop" / "src-tauri" / "resources" / "python"


def bundle_or_skip() -> Path:
    if not (BUNDLE / "bin" / "python3").exists():
        pytest.skip("no bundled interpreter -- run `make tauri-python`")
    return BUNDLE


def bundle_lib() -> Path:
    """The bundle's `lib/python3.X`, whichever X it was built with."""
    return next(bundle_or_skip().glob("lib/python3.*"))


def test_the_built_bundle_kept_everything_the_daemon_needs() -> None:
    root = bundle_or_skip()
    lib = bundle_lib()
    missing = [
        name for name in (
            "site-packages/mcp_gateway/cli.py",
            "site-packages/mcp_gateway/webui.py",
            "site-packages/mcp_gateway/ui/app.js",
            "site-packages/mcp_gateway/ui/index.html",
            "site-packages/websockets/__init__.py",
            "site-packages/ruamel/yaml/__init__.py",
            "ssl.py",
            "asyncio/__init__.py",
            "sqlite3/__init__.py",
        ) if not (lib / name).exists()
    ]
    assert not missing, missing
    assert list(root.glob("lib/libpython3.*.dylib")), "the interpreter links against this"


def test_the_built_bundle_dropped_what_it_should_have() -> None:
    lib = bundle_lib()
    present = [
        name for name in ("idlelib", "tkinter", "ensurepip", "turtledemo", "site-packages/pip")
        if (lib / name).exists()
    ]
    assert not present, present
    assert not list(bundle_or_skip().glob("lib/tcl*")), "Tk went, its runtime should too"


def test_the_built_bundle_carries_its_manifest() -> None:
    import json

    manifest = json.loads((bundle_or_skip() / "BUNDLE.json").read_text())
    assert manifest["python"].startswith(bundle_python.DEFAULT_VERSION)
    assert manifest["wheel"].endswith(".whl")
    assert len(manifest["wheel_sha256"]) == 64
    assert manifest["stripped"], "a bundle that stripped nothing did not run the strip step"


def test_the_built_bundle_is_precompiled() -> None:
    """A read-only bundle that has to compile on every launch is a slow bundle."""
    lib = bundle_lib()
    assert (lib / "__pycache__").is_dir(), "compileall did not run"
    assert list((lib / "site-packages" / "mcp_gateway" / "__pycache__").glob("cli.*.pyc")), (
        "the gateway's own modules were not compiled"
    )
