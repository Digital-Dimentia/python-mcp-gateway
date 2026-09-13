"""`scripts/collect_desktop_bundle.py`, and the bundle targets it has to keep up with.

Two failures are worth a test here, and neither announces itself.

**A target that is built and never published.** `tauri.linux.conf.json` adds `appimage`;
nobody adds the glob here; the release ships a `.deb` and the AppImage sits in the runner's
temporary directory until the runner is deleted. Nothing fails, and the omission is visible
only to whoever went looking for a file that is not there.

**An asset name that collides.** Three runners upload to one release. `MCP-Gateway.app` from
the macOS leg and `MCP-Gateway_0.1.0_amd64.deb` from the Linux one do not collide, but two
macOS legs -- Apple Silicon and Intel -- produce byte-different files under one name, and a
release keeps whichever landed last.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TAURI = REPO_ROOT / "desktop" / "src-tauri"


def load_script():
    """Import the script by path. `scripts/` is not a package, and should not become one."""
    spec = importlib.util.spec_from_file_location(
        "collect_desktop_bundle", REPO_ROOT / "scripts" / "collect_desktop_bundle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collect_desktop_bundle = load_script()


def conf(name: str) -> dict:
    return json.loads((TAURI / name).read_text())


# --- the bundle targets, and the globs that have to match them -------------------------------


PLATFORM_CONFIGS = {
    "macos": "tauri.conf.json",
    "linux": "tauri.linux.conf.json",
    "windows": "tauri.windows.conf.json",
}

#: How a Tauri bundle target name maps to the directory it is written into. Only the ones
#: this project builds; `cargo tauri build` names the rest itself.
TARGET_DIRECTORIES = {
    "app": "macos",
    "dmg": "dmg",
    "deb": "deb",
    "rpm": "rpm",
    "appimage": "appimage",
    "nsis": "nsis",
    "msi": "msi",
}


@pytest.mark.parametrize("slug,config", sorted(PLATFORM_CONFIGS.items()))
def test_every_target_the_bundler_builds_is_one_the_collector_publishes(
    slug: str, config: str
) -> None:
    """A target with no glob is an artifact built on a runner and then thrown away."""
    targets = conf(config)["bundle"]["targets"]
    directories = {pattern.split("/")[0] for pattern in collect_desktop_bundle.PATTERNS[slug]}
    for target in targets:
        assert TARGET_DIRECTORIES[target] in directories, (
            f"{config} builds {target!r} and nothing collects it"
        )


def test_the_platform_overlays_change_the_targets_and_nothing_else() -> None:
    """`tauri.<platform>.conf.json` is merged over the base, so it must stay small.

    The base config is where the whole design is written down -- the CSP that stops the
    window reaching the network, the resources, the frontend path. A platform overlay that
    restated any of it would be a second copy able to disagree with the first, and the
    disagreement would only ever be visible on one platform.
    """
    for config in ("tauri.linux.conf.json", "tauri.windows.conf.json"):
        overlay = conf(config)
        assert set(overlay) <= {"$schema", "bundle"}, f"{config} reaches past the bundle"
        assert "security" not in json.dumps(overlay), config
        assert "resources" not in overlay["bundle"], (
            f"{config} would fork the resource list; the base config owns it"
        )


def test_the_base_config_still_builds_the_macos_bundle() -> None:
    """`app` is macOS-only, which is why the other two platforms need an overlay at all."""
    assert conf("tauri.conf.json")["bundle"]["targets"] == ["app"]


def test_windows_installs_for_one_user_and_needs_no_administrator() -> None:
    """A developer tool that demands elevation to install is one people do not install.

    `currentUser` also puts the app somewhere the supervisor can write beside, which matters
    because the data directory is created on first launch.
    """
    nsis = conf("tauri.windows.conf.json")["bundle"]["windows"]["nsis"]
    assert nsis["installMode"] == "currentUser"


# --- what each asset is called ---------------------------------------------------------------


def test_the_app_becomes_a_file_and_says_so() -> None:
    """A release cannot hold a directory, and `.app.tar.gz` says what unpacks out of it."""
    name = collect_desktop_bundle.asset_name(
        Path("MCP-Gateway.app"), "MCP-Gateway", "0.1.0", "macos-aarch64"
    )
    assert name == "MCP-Gateway-0.1.0-macos-aarch64.app.tar.gz"


def test_every_platform_gets_a_name_of_its_own() -> None:
    """The property that matters: no two legs of the matrix write the same asset name."""
    product, version = "MCP-Gateway", "0.1.0"
    sources = {
        "macos-aarch64": [Path("MCP-Gateway.app")],
        "macos-x86_64": [Path("MCP-Gateway.app")],
        "linux-x86_64": [
            Path("MCP-Gateway_0.1.0_amd64.deb"),
            Path("MCP-Gateway_0.1.0_amd64.AppImage"),
        ],
        "windows-x86_64": [Path("MCP-Gateway_0.1.0_x64-setup.exe")],
    }
    names = [
        collect_desktop_bundle.asset_name(source, product, version, tag)
        for tag, paths in sources.items()
        for source in paths
    ]
    assert len(names) == len(set(names)), names
    # And every one of them says which platform it is for, which is the point of renaming.
    for tag in sources:
        assert any(tag in name for name in names), tag


def test_the_installer_keeps_the_suffix_the_machine_needs() -> None:
    name = collect_desktop_bundle.asset_name(
        Path("MCP-Gateway_0.1.0_x64-setup.exe"), "MCP-Gateway", "0.1.0", "windows-x86_64"
    )
    assert name.endswith("-setup.exe")

    for suffix in (".deb", ".AppImage"):
        name = collect_desktop_bundle.asset_name(
            Path(f"MCP-Gateway_0.1.0_amd64{suffix}"), "MCP-Gateway", "0.1.0", "linux-x86_64"
        )
        assert name.endswith(suffix), name


def test_the_name_comes_from_the_config_the_bundler_read() -> None:
    """Not a second copy of the product name: the one the `.app` was actually stamped with."""
    product, version = collect_desktop_bundle.product_and_version()
    assert product == conf("tauri.conf.json")["productName"]
    assert version == conf("tauri.conf.json")["version"]


def test_windows_reports_its_architecture_in_a_word_the_triple_knows(monkeypatch) -> None:
    """`AMD64` again. See `scripts/bundle_python.py` -- the two must agree or the asset name
    disagrees with the interpreter inside it."""
    monkeypatch.setattr(collect_desktop_bundle.platform, "machine", lambda: "AMD64")
    assert collect_desktop_bundle.arch_slug() == "x86_64"
    monkeypatch.setattr(collect_desktop_bundle.platform, "machine", lambda: "ARM64")
    assert collect_desktop_bundle.arch_slug() == "aarch64"


# --- the archive ------------------------------------------------------------------------------


def test_the_app_archive_keeps_its_symlinks_and_its_executable_bit(tmp_path: Path) -> None:
    """`shutil.make_archive` would copy the symlinks and drop the bit; the bundle needs both.

    A `.app` whose `Contents/MacOS/` binary comes out non-executable is one that unpacks,
    looks complete, and cannot be launched.
    """
    app = tmp_path / "MCP-Gateway.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    binary = app / "mcp-gateway-desktop"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (tmp_path / "MCP-Gateway.app" / "Contents" / "Current").symlink_to("MacOS")

    archive = tmp_path / "out.tar.gz"
    collect_desktop_bundle.archive_app(tmp_path / "MCP-Gateway.app", archive)

    with tarfile.open(archive) as tar:
        members = {member.name: member for member in tar.getmembers()}
    # Unpacks as the bundle, not as a directory of its insides.
    assert all(name.startswith("MCP-Gateway.app") for name in members), sorted(members)
    assert members["MCP-Gateway.app/Contents/Current"].issym()
    assert members["MCP-Gateway.app/Contents/MacOS/mcp-gateway-desktop"].mode & 0o111


def test_an_empty_bundle_directory_is_an_error_and_not_an_empty_release(tmp_path: Path) -> None:
    """Publishing nothing, quietly, is the failure this whole file exists to prevent."""
    with pytest.raises(SystemExit, match="tauri-bundle"):
        collect_desktop_bundle.collect(tmp_path / "out", tmp_path / "empty")
