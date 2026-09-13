"""The port file: written atomically, read tolerantly, removed on the way out.

Small enough to be obvious and load-bearing enough to be worth pinning anyway — the desktop
shell will not connect if any of it is wrong, and the way it fails is a window that never
finishes starting rather than an error anybody sees.

The atomicity is the part that repays a test. A supervisor polls for this file and opens it
the instant it appears; a plain write is observable between `open` and the bytes landing,
which hands the reader an empty file to parse as a port number. That window is too small to
catch by running it, so what is asserted instead is the mechanism that closes it.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp_gateway import portfile


def test_a_written_port_reads_back(tmp_path) -> None:
    path = tmp_path / "gateway.port"
    assert portfile.write(path, 49613) == path
    assert path.read_text() == "49613\n"
    assert portfile.read(path) == 49613


def test_the_write_goes_through_a_rename_in_the_same_directory(tmp_path) -> None:
    """The atomicity claim, asserted where it can be. `rename` is only atomic within one
    filesystem, and the system temp dir is frequently not the same one as the target."""
    path = tmp_path / "sub" / "gateway.port"
    seen: list[tuple[str, str]] = []
    real = os.replace

    def watching(src, dst):
        seen.append((str(src), str(dst)))
        return real(src, dst)

    os.replace = watching
    try:
        portfile.write(path, 8765)
    finally:
        os.replace = real

    assert len(seen) == 1, "the port should arrive by exactly one rename"
    source, destination = seen[0]
    assert Path(destination) == path
    assert Path(source).parent == path.parent, "a cross-filesystem rename is not atomic"


def test_a_missing_parent_directory_is_created(tmp_path) -> None:
    path = tmp_path / "deep" / "deeper" / "gateway.port"
    assert portfile.write(path, 8765) == path
    assert portfile.read(path) == 8765


def test_no_temporary_file_is_left_behind(tmp_path) -> None:
    portfile.write(tmp_path / "gateway.port", 8765)
    assert [p.name for p in tmp_path.iterdir()] == ["gateway.port"]


def test_a_write_that_cannot_happen_is_logged_rather_than_raised(tmp_path) -> None:
    """The daemon is already bound and serving by then. Refusing to run because a
    convenience file could not be written would take a working gateway down over its own
    bookkeeping; a supervisor waiting on the file times out and says so instead."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("I am a file")
    assert portfile.write(blocked / "gateway.port", 8765) is None


def test_reading_is_tolerant_of_everything_a_poller_can_see(tmp_path) -> None:
    """A reader polling for this file races the writer on any platform where the writer did
    not use `rename`. "Not a port yet" is a state to wait through, not an error."""
    path = tmp_path / "gateway.port"
    for contents, expected in [
        ("49613\n", 49613),
        ("8765", 8765),
        ("  8765  \n", 8765),
        ("", None),
        ("   ", None),
        ("notaport", None),
        ("8765x", None),
        ("-1", None),
        ("0", None),
        ("65536", None),
        ("99999999", None),
    ]:
        path.write_text(contents)
        assert portfile.read(path) == expected, contents


def test_reading_a_file_that_is_not_there_is_none(tmp_path) -> None:
    assert portfile.read(tmp_path / "nothing-here") is None


def test_removing_is_idempotent_and_never_raises(tmp_path) -> None:
    """It runs on every exit path, including ones where it has already run."""
    path = tmp_path / "gateway.port"
    portfile.write(path, 8765)
    portfile.remove(path)
    assert not path.exists()
    portfile.remove(path)
    portfile.remove(None)
