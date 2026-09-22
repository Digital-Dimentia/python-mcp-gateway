"""`scripts/bundle_python.py`, minus the download.

The script's real work is a fetch, an install and a subprocess smoke test, none of which
belong in a unit suite -- `make tauri-python` is what exercises those, and its verification
step is the gate that matters. What is here is the part that is pure, easy to get wrong, and
expensive to get wrong: the strip list.

A strip list is a list of things nobody will notice are gone until a user does. `tkinter`
going missing is invisible; `_ssl` going missing is a backend that cannot reach GitHub, three
weeks later, on somebody else's machine. So the patterns are asserted against the module's
own statement of what must survive.

There are two lists -- the Unix tree and the Windows tree are different shapes -- and both
are checked here regardless of which machine is running the suite. A Windows strip pattern
that deletes `_ssl.pyd` would otherwise be found by a user on Windows and by nobody else,
and this project's Python matrix is Linux-only on purpose.
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


def matches(path: str, globs: tuple[str, ...]) -> bool:
    """Whether `path`, relative to the interpreter root, matches any pattern in `globs`."""
    return any(fnmatch(path, pattern) or path.startswith(f"{pattern.rstrip('/')}/")
               for pattern in globs) or any(
        fnmatch(path, f"{pattern}/*") for pattern in globs
    )


def stripped(path: str) -> bool:
    """Whether the Unix strip list removes `path`."""
    return matches(path, bundle_python.UNIX_STRIP_GLOBS)


def stripped_on_windows(path: str) -> bool:
    """Whether the Windows strip list removes `path`."""
    return matches(path, bundle_python.WINDOWS_STRIP_GLOBS)


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
    "lib/python3.13/site-packages/mcp_gateway_ui/app.js",
    "lib/python3.13/site-packages/mcp_gateway_ui/index.html",
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


# --- the same two questions, asked of the Windows tree ---------------------------------------
#
# python-build-standalone lays Windows out as `python.exe` + `Lib/` + `DLLs/` at the root,
# with the extension modules as `.pyd` files rather than `.so` files in `lib-dynload`. The
# policy is identical; only the spellings move.


WINDOWS_SURVIVORS = (
    # The interpreter and the DLL it is: removing either is a bundle that cannot start.
    "python.exe",
    "python313.dll",
    "python3.dll",
    "vcruntime140.dll",
    # The daemon, and what it imports.
    "Lib/site-packages/mcp_gateway/cli.py",
    "Lib/site-packages/mcp_gateway/webui.py",
    "Lib/site-packages/websockets/__init__.py",
    "Lib/site-packages/ruamel/yaml/__init__.py",
    # The UI assets `webui.py` reads through `importlib.resources`.
    "Lib/site-packages/mcp_gateway_ui/app.js",
    "Lib/site-packages/mcp_gateway_ui/index.html",
    # Extension modules, which live in `DLLs/` here. `_ssl` also needs the two OpenSSL
    # DLLs beside it, and `libcrypto` is one `libs`-shaped typo away from being removed.
    "DLLs/_ssl.pyd",
    "DLLs/_sqlite3.pyd",
    "DLLs/_lzma.pyd",
    "DLLs/_ctypes.pyd",
    "DLLs/libssl-3.dll",
    "DLLs/libcrypto-3.dll",
    "DLLs/sqlite3.dll",
    "Lib/ssl.py",
    "Lib/asyncio/__init__.py",
    "Lib/sqlite3/__init__.py",
)


@pytest.mark.parametrize("path", WINDOWS_SURVIVORS)
def test_the_windows_strip_list_spares_what_the_daemon_needs(path: str) -> None:
    assert not stripped_on_windows(path), f"{path} matches a strip pattern"


WINDOWS_CASUALTIES = (
    "Lib/idlelib/__init__.py",
    "Lib/tkinter/__init__.py",
    "Lib/turtledemo/__init__.py",
    "Lib/ensurepip/__init__.py",
    "Lib/site-packages/pip/__init__.py",
    "DLLs/_tkinter.pyd",
    "DLLs/tcl86t.dll",
    "tcl/tcl8.6/init.tcl",
    "include/Python.h",
    "libs/python313.lib",
    "Scripts/pip3.exe",
    "Scripts/idle.exe",
)


@pytest.mark.parametrize("path", WINDOWS_CASUALTIES)
def test_the_windows_strip_list_removes_what_it_claims_to(path: str) -> None:
    assert stripped_on_windows(path), f"{path} matches no strip pattern"


def test_the_import_library_goes_and_the_runtime_dll_stays() -> None:
    """`libs/python313.lib` is for linking against; `python313.dll` is the interpreter."""
    assert stripped_on_windows("libs/python313.lib")
    assert not stripped_on_windows("python313.dll")


def test_the_two_strip_lists_describe_the_same_policy() -> None:
    """Every category in one list has a counterpart in the other.

    Not a spelling check -- the trees are different shapes -- but a check that neither list
    quietly lost a whole category the other still removes. A bundle that ships `pip` on one
    platform and not the other is a bug on the platform that ships it.
    """
    for category in ("idlelib", "turtledemo", "pydoc_data", "tkinter", "ensurepip", "pip"):
        assert any(category in pattern for pattern in bundle_python.UNIX_STRIP_GLOBS), category
        assert any(
            category in pattern for pattern in bundle_python.WINDOWS_STRIP_GLOBS
        ), category


def test_the_interpreter_path_is_the_one_the_rust_side_names() -> None:
    """Twinned with `Layout::interpreter` in `src/desktop/src-tauri/src/supervisor.rs`.

    Asserted from both sides because a disagreement here is an app that starts nothing, and
    neither side can notice it alone.
    """
    root = Path("/anywhere/python")
    assert bundle_python.interpreter_path(root) == root / "bin" / "python3"
    assert bundle_python.stdlib_path.__doc__  # named here so a rename breaks this test too

    rust = (
        REPO_ROOT / "src" / "desktop" / "src-tauri" / "src" / "supervisor.rs"
    ).read_text()
    assert '"python.exe"' in rust, "the Windows interpreter name moved on the Rust side"
    assert 'join("bin").join("python3")' in rust, "the Unix interpreter path moved"


# --- the platform triple -------------------------------------------------------------------


def test_the_triple_names_this_machine(monkeypatch) -> None:
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(bundle_python.platform, "system", lambda: "Darwin")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-macos-aarch64-none"

    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "x86_64")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-macos-x86_64-none"

    # Linux's standalone builds are `-gnu`, not `-none`. This is the suffix the release
    # workflow's Linux runner asks for, and the one thing about the triple that is not
    # simply the platform's own name.
    monkeypatch.setattr(bundle_python.platform, "system", lambda: "Linux")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-linux-x86_64-gnu"

    # Windows spells both architectures differently from everyone else, and `platform`
    # reports what Windows says. `AMD64` is what every 64-bit Windows runner reports; a
    # triple that did not know the word would fail every Windows build at the first step.
    monkeypatch.setattr(bundle_python.platform, "system", lambda: "Windows")
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "AMD64")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-windows-x86_64-none"

    # Windows on ARM, which the workflow does not build but the triple should still name.
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "ARM64")
    assert bundle_python.uv_python_tag("3.13") == "cpython-3.13-windows-aarch64-none"


def test_an_architecture_we_cannot_name_is_refused_rather_than_guessed(monkeypatch) -> None:
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: "sparc")
    with pytest.raises(SystemExit, match="sparc"):
        bundle_python.uv_python_tag("3.13")


# --- picking the interpreter out of uv's staging directory ------------------------------------
#
# uv installs `cpython-3.13.15-<platform>` and puts the major.minor name beside it as a link.
# On Unix that link is a symlink; **on Windows it is a directory junction**, which Python
# reports as not a symlink -- so the filter that worked everywhere else saw two directories
# and refused to guess, which was every Windows build failing at its first minute.
#
# A symlink stands in for the junction here, because the property under test is the one both
# share: two names, one directory, and `resolve()` collapses them.


TAG = "cpython-3.13-linux-x86_64-gnu"
REAL = "cpython-3.13.15-linux-x86_64-gnu"


def staging_with_alias(tmp_path: Path, *, linked: bool) -> Path:
    """uv's staging area: the versioned directory, the alias, and uv's own leavings."""
    staging = tmp_path / ".python.staging"
    real = staging / REAL
    (real / "bin").mkdir(parents=True)
    alias = staging / TAG
    if linked:
        alias.symlink_to(real, target_is_directory=True)
    else:
        # What a junction looks like from Python, and what a uv that copied would leave.
        (alias / "bin").mkdir(parents=True)
    (staging / ".lock").write_text("")
    (staging / ".temp").mkdir()
    return staging


def test_the_linked_alias_is_not_a_second_interpreter(tmp_path: Path) -> None:
    staging = staging_with_alias(tmp_path, linked=True)
    assert bundle_python.choose_installed(staging, TAG) == (staging / REAL).resolve()


def test_a_junction_shaped_alias_is_recognised_by_its_name(tmp_path: Path) -> None:
    """The fallback: two directories that do not resolve to each other, one named the tag."""
    staging = staging_with_alias(tmp_path, linked=False)
    assert bundle_python.choose_installed(staging, TAG) == (staging / REAL).resolve()


def test_uvs_own_lock_and_temp_are_not_mistaken_for_an_interpreter(tmp_path: Path) -> None:
    staging = tmp_path / ".python.staging"
    (staging / REAL / "bin").mkdir(parents=True)
    (staging / ".temp").mkdir()
    (staging / ".lock").write_text("")
    assert bundle_python.choose_installed(staging, TAG) == (staging / REAL).resolve()


def test_a_staging_area_it_cannot_read_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """Two unrelated interpreters, or none: better to stop than to bundle a coin flip."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="expected one interpreter"):
        bundle_python.choose_installed(empty, TAG)

    two = tmp_path / "two"
    (two / "cpython-3.12.8-linux-x86_64-gnu").mkdir(parents=True)
    (two / REAL).mkdir()
    with pytest.raises(SystemExit, match="expected one interpreter"):
        bundle_python.choose_installed(two, TAG)


# --- adopting an interpreter someone fetched by hand ------------------------------------------
#
# `--interpreter` exists because `uv python install` is the one step of the script that talks
# to the network, and that step is what a corporate firewall breaks. The tests below are the
# whole of what the flag decides: which directory in what it was handed is the interpreter,
# that the thing handed over survives, and that a wrong answer stops rather than proceeds --
# because everything after this point treats an adopted tree exactly like a fetched one.


def fake_interpreter(root: Path) -> Path:
    """A tree shaped like python-build-standalone's, at whatever paths this platform uses."""
    interpreter = bundle_python.interpreter_path(root)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\n")
    lib = root / ("Lib" if bundle_python.is_windows() else "lib/python3.13")
    lib.mkdir(parents=True)
    (lib / "os.py").write_text("")
    return root


def test_an_unpacked_tree_is_adopted_whichever_way_it_was_unpacked(tmp_path: Path) -> None:
    """The tarball unpacks to `python/`, so a person holds that or the thing holding it."""
    for name, source in (
        ("direct", fake_interpreter(tmp_path / "direct")),
        ("wrapped", fake_interpreter(tmp_path / "wrapped" / "python").parent),
    ):
        out = tmp_path / f"out-{name}"
        bundle_python.adopt_interpreter(out, source)
        assert bundle_python.interpreter_path(out).exists()
        assert (bundle_python.stdlib_path(out) / "os.py").exists()


def test_a_tarball_is_unpacked_rather_than_refused(tmp_path: Path) -> None:
    """"Download it however you can, hand me the file" is the whole point of the flag."""
    import tarfile

    fake_interpreter(tmp_path / "staging" / "python")
    archive = tmp_path / "cpython-3.13.15+20260901-x86_64-unknown-linux-gnu.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "staging" / "python", arcname="python")

    out = tmp_path / "out"
    bundle_python.adopt_interpreter(out, archive)
    assert bundle_python.interpreter_path(out).exists()
    assert archive.exists(), "the download has to survive the build that used it"


@pytest.mark.skipif(bundle_python.is_windows(), reason="no `bin/python3` symlink on Windows")
def test_the_interpreters_own_symlink_is_not_resolved_into_a_second_copy(tmp_path: Path) -> None:
    """`bin/python3` is a symlink to `python3.13`. Copying it flat ships two that can drift."""
    source = fake_interpreter(tmp_path / "source")
    real = source / "bin" / "python3.13"
    real.write_text("#!/bin/sh\n")
    (source / "bin" / "python3").unlink()
    (source / "bin" / "python3").symlink_to("python3.13")

    out = tmp_path / "out"
    bundle_python.adopt_interpreter(out, source)
    assert (out / "bin" / "python3").is_symlink()


def test_what_was_adopted_is_copied_and_not_moved(tmp_path: Path) -> None:
    """A tarball fetched by hand over a bad link must survive a build that fails later."""
    source = fake_interpreter(tmp_path / "source")
    bundle_python.adopt_interpreter(tmp_path / "out", source)
    assert bundle_python.interpreter_path(source).exists()


def test_a_tree_that_is_not_an_interpreter_stops_the_build(tmp_path: Path) -> None:
    """Better here, with the path in the message, than three minutes later inside `verify`."""
    empty = tmp_path / "empty"
    (empty / "python").mkdir(parents=True)
    with pytest.raises(SystemExit, match="expected an unpacked"):
        bundle_python.adopt_interpreter(tmp_path / "out", empty)

    with pytest.raises(SystemExit, match="no such interpreter"):
        bundle_python.adopt_interpreter(tmp_path / "out", tmp_path / "nowhere")

    zstd = tmp_path / "cpython-3.13.15+20260901-x86_64-unknown-linux-gnu.tar.zst"
    zstd.write_bytes(b"")
    with pytest.raises(SystemExit, match="not a .tar.gz"):
        bundle_python.adopt_interpreter(tmp_path / "out", zstd)


def test_an_interpreter_inside_the_output_is_refused_before_anything_is_deleted(
    tmp_path: Path,
) -> None:
    """`--out` is emptied first, so pointing it at the source would destroy both."""
    out = tmp_path / "out"
    source = fake_interpreter(out / "unpacked" / "python")
    with pytest.raises(SystemExit, match="is inside"):
        bundle_python.adopt_interpreter(out, source)
    assert bundle_python.interpreter_path(source).exists()


# --- the version it pins -------------------------------------------------------------------


def test_the_bundled_version_is_one_the_project_supports() -> None:
    """`requires-python` is what the library supports; a bundle ships exactly one of them."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.12,<3.15"' in pyproject, "the supported range moved"
    major, minor = bundle_python.DEFAULT_VERSION.split(".")
    assert (int(major), int(minor)) >= (3, 12)
    assert (int(major), int(minor)) < (3, 15)


# --- building without uv -------------------------------------------------------------------
#
# The built-in fetcher is the path for a machine with no uv, so nothing that runs this suite
# exercises it for real unless someone hides uv on purpose. What is pinned here is what would
# otherwise fail on the first build that needed it: a triple spelled the way the release page
# does not, a platform with no hash, a checksum that is compared but not enforced.

BUILT_TRIPLES = (
    ("Darwin", "arm64", "aarch64-apple-darwin"),
    ("Darwin", "x86_64", "x86_64-apple-darwin"),
    ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
    ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
    ("Windows", "ARM64", "aarch64-pc-windows-msvc"),
    ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
)


@pytest.mark.parametrize(("system", "machine", "triple"), BUILT_TRIPLES)
def test_every_platform_has_a_release_triple_and_a_pinned_hash(
    monkeypatch, system: str, machine: str, triple: str
) -> None:
    monkeypatch.setattr(bundle_python.platform, "system", lambda: system)
    monkeypatch.setattr(bundle_python.platform, "machine", lambda: machine)
    assert bundle_python.pbs_triple() == triple
    digest = bundle_python.PBS_SHA256[triple]
    assert len(digest) == 64 and int(digest, 16) >= 0


def test_the_pin_is_the_version_uv_would_have_been_asked_for() -> None:
    """Two fetchers, one interpreter: a bump to one that forgets the other is caught here."""
    assert bundle_python.PBS_PYTHON.startswith(f"{bundle_python.DEFAULT_VERSION}.")
    assert set(bundle_python.PBS_SHA256) == {triple for _, _, triple in BUILT_TRIPLES}


def test_a_github_proxy_replaces_the_host_and_keeps_the_path() -> None:
    """`GITHUB_PROXY_BASE` is read at import, so this asserts the shape it produces."""
    assert bundle_python.PBS_DOWNLOAD.startswith(bundle_python.PBS_GITHUB_BASE.rstrip("/"))
    assert bundle_python.PBS_DOWNLOAD.endswith(
        "/astral-sh/python-build-standalone/releases/download"
    )
    assert "//astral-sh" not in bundle_python.PBS_DOWNLOAD, "a trailing slash doubled up"


def test_the_download_url_honours_uvs_mirror_variable(monkeypatch) -> None:
    asset = bundle_python.pbs_asset("aarch64-apple-darwin")
    assert asset == (
        f"cpython-{bundle_python.PBS_PYTHON}+{bundle_python.PBS_RELEASE}"
        "-aarch64-apple-darwin-install_only_stripped.tar.gz"
    )

    monkeypatch.delenv("UV_PYTHON_INSTALL_MIRROR", raising=False)
    url = bundle_python.pbs_url(asset)
    assert url.startswith(f"{bundle_python.PBS_DOWNLOAD}/{bundle_python.PBS_RELEASE}/")
    assert "%2B" in url, "the `+` in the asset name has to survive as a URL"

    monkeypatch.setenv("UV_PYTHON_INSTALL_MIRROR", "file:///srv/pbs/")
    assert bundle_python.pbs_url(asset).startswith(f"file:///srv/pbs/{bundle_python.PBS_RELEASE}/")


def mirror_with(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A `file://` mirror holding a fake tarball for this machine. Returns it and its hash."""
    import hashlib
    import tarfile

    fake_interpreter(tmp_path / "staging" / "python")
    release = tmp_path / "mirror" / bundle_python.PBS_RELEASE
    release.mkdir(parents=True)
    archive = release / bundle_python.pbs_asset(bundle_python.pbs_triple())
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "staging" / "python", arcname="python")
    monkeypatch.setenv("UV_PYTHON_INSTALL_MIRROR", (tmp_path / "mirror").as_uri())
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_the_builtin_fetcher_adopts_what_it_downloaded(tmp_path: Path, monkeypatch) -> None:
    archive, digest = mirror_with(tmp_path, monkeypatch)
    monkeypatch.setitem(bundle_python.PBS_SHA256, bundle_python.pbs_triple(), digest)

    out = tmp_path / "out"
    assert bundle_python.fetch_interpreter_builtin(out) == archive.name
    assert bundle_python.interpreter_path(out).exists()


def test_a_download_with_the_wrong_hash_is_never_unpacked(tmp_path: Path, monkeypatch) -> None:
    mirror_with(tmp_path, monkeypatch)
    monkeypatch.setitem(bundle_python.PBS_SHA256, bundle_python.pbs_triple(), "0" * 64)

    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="Refusing to unpack"):
        bundle_python.fetch_interpreter_builtin(out)
    assert not out.exists()


def test_a_hash_mismatch_says_which_kind_of_bad_download_it_was(tmp_path: Path) -> None:
    """"sha256 mismatch" alone sends a person to the pin. It is usually the network."""
    common = dict(
        asset="cpython.tar.gz", expected="a" * 64, actual="b" * 64,
        served_from="https://example/x", kept=tmp_path / "kept",
    )

    cut_off = bundle_python.mismatch_message(
        received=10, length=100, content_type="application/octet-stream",
        head=b"\x1f\x8b", **common,
    )
    assert "stopped early: 10 of 100 bytes" in cut_off

    portal = bundle_python.mismatch_message(
        received=900, length=900, content_type="text/html", head=b"<!", **common,
    )
    assert "not a gzip archive" in portal and "text/html" in portal

    other_build = bundle_python.mismatch_message(
        received=900, length=None, content_type="", head=b"\x1f\x8b", **common,
    )
    assert "not the pinned one" in other_build

    for message in (cut_off, portal, other_build):
        assert "https://example/x" in message and str(tmp_path / "kept") in message


def test_an_html_page_in_place_of_the_tarball_is_named_as_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The captive-portal case, end to end through a `file://` mirror."""
    archive, _ = mirror_with(tmp_path, monkeypatch)
    archive.write_text("<!doctype html><title>Sign in to the network</title>")
    with pytest.raises(SystemExit, match="not a gzip archive"):
        bundle_python.fetch_interpreter_builtin(tmp_path / "out")


def test_auto_is_uv_when_there_is_one_and_builtin_when_not(monkeypatch) -> None:
    monkeypatch.setattr(bundle_python.shutil, "which", lambda name: "/usr/bin/uv")
    assert bundle_python.resolve_fetcher("auto") == "uv"
    assert bundle_python.resolve_fetcher("builtin") == "builtin"

    monkeypatch.setattr(bundle_python.shutil, "which", lambda name: None)
    assert bundle_python.resolve_fetcher("auto") == "builtin"
    assert bundle_python.resolve_fetcher("uv") == "uv"


def test_without_uv_the_wheel_goes_in_through_the_interpreters_own_pip(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    calls: list[list[str]] = []

    def fake_run(argv, check=True, **kwargs):
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bundle_python.shutil, "which", lambda name: None)
    monkeypatch.setattr(bundle_python, "run", fake_run)
    root = fake_interpreter(tmp_path / "python")
    wheel = tmp_path / "python_mcp_gateway-0.1.0-py3-none-any.whl"

    bundle_python.install_gateway(root, wheel)
    interpreter = str(bundle_python.interpreter_path(root))
    assert calls[0] == [interpreter, "-m", "pip", "--version"]
    assert calls[-1][:4] == [interpreter, "-m", "pip", "install"]
    assert calls[-1][-1] == str(wheel)
    assert not any(argv[0] == "uv" for argv in calls)


# --- against a bundle that was actually built ------------------------------------------------
#
# The tests above reason about the patterns with `fnmatch`, which is not what the script
# uses -- `Path.glob` does not let `*` cross a directory separator and `fnmatch` does. The
# two agree on every pattern here, but "agree today" is not a property worth trusting, so
# when a real bundle is on disk it gets asked directly. Skipped otherwise: a checkout that
# has never run `make tauri-python` stays green, the same way `tests/ui/` skips without node.

BUNDLE = REPO_ROOT / "src" / "desktop" / "src-tauri" / "resources" / "python"


def bundle_or_skip() -> Path:
    """The built bundle, or a skip. Asked through the script, so Windows is not a skip."""
    if not bundle_python.interpreter_path(BUNDLE).exists():
        pytest.skip("no bundled interpreter -- run `make tauri-python`")
    return BUNDLE


def bundle_lib() -> Path:
    """The bundle's standard library, wherever this platform puts it."""
    return bundle_python.stdlib_path(bundle_or_skip())


def test_the_built_bundle_kept_everything_the_daemon_needs() -> None:
    root = bundle_or_skip()
    lib = bundle_lib()
    missing = [
        name for name in (
            "site-packages/mcp_gateway/cli.py",
            "site-packages/mcp_gateway/webui.py",
            "site-packages/mcp_gateway_ui/app.js",
            "site-packages/mcp_gateway_ui/index.html",
            "site-packages/websockets/__init__.py",
            "site-packages/ruamel/yaml/__init__.py",
            "ssl.py",
            "asyncio/__init__.py",
            "sqlite3/__init__.py",
        ) if not (lib / name).exists()
    ]
    assert not missing, missing
    # The library the interpreter links against: a `.dylib` beside the stdlib on macOS, a
    # `.so` there on Linux, a `.dll` at the root on Windows. Removing it is the one strip
    # that turns the bundle into a file that cannot start at all.
    shared = ["python3*.dll"] if bundle_python.is_windows() else [
        "lib/libpython3.*.dylib", "lib/libpython3.*.so*"
    ]
    assert any(list(root.glob(pattern)) for pattern in shared), "the interpreter links against this"


def test_the_built_bundle_dropped_what_it_should_have() -> None:
    lib = bundle_lib()
    present = [
        name for name in ("idlelib", "tkinter", "ensurepip", "turtledemo", "site-packages/pip")
        if (lib / name).exists()
    ]
    assert not present, present
    leftover = "tcl" if bundle_python.is_windows() else "lib/tcl*"
    assert not list(bundle_or_skip().glob(leftover)), "Tk went, its runtime should too"


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
