"""Write the bound port where a supervisor can read it, and take it away on the way out.

`--port 0` is how you avoid a port conflict, and it means nobody knows the port until the
socket exists. Something has to publish it. Until now that something was a log line -- the
desktop shell parsed `listening on ws://host:port/mcp` off stderr -- which made a sentence
written for a human into a wire format that could not be reworded. This is the replacement.

## Why a file rather than a better log line

Two facts have to cross the boundary, and a file carries both where a line carries one. The
number is the obvious half. The other is *readiness*: the file does not exist until the
socket is bound, so its appearance is the signal, and a supervisor that polls for it needs
no agreement about wording, no parser, and no ordering assumption about which line comes
first. A log line can only ever be the number, with readiness inferred from having seen it.

It also generalises past the one caller that prompted it. Anything that starts this daemon
on `--port 0` -- a test harness, a systemd unit, a script -- has the same problem, and
`--port-file` is the answer daemons have been giving to it for decades.

## Why the write is atomic

A supervisor polling for the file will open it the instant it appears. A plain write can be
observed after `open` and before the bytes land, which hands the reader an empty file to
parse as a port number. So the content goes to a temporary file in the same directory and is
renamed into place: `rename` within one filesystem is atomic, so the path either does not
exist or has the whole number in it, and there is no third state for a reader to trip on.

## What is in it

The port in decimal, one line, and nothing else. Not JSON: everything else a reader might
want -- the host, the pid -- it either already knows (it chose the host, it spawned the
process) or has a better source for. A format with one field cannot be misparsed and cannot
grow a compatibility question.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write(path: Path, port: int) -> Path | None:
    """Publish `port` at `path`, atomically. Returns the path, or `None` if it could not.

    A failure here is logged and swallowed rather than raised. The daemon is bound and
    serving by the time this is called, and refusing to run because a convenience file could
    not be written would take a working gateway down over its own bookkeeping. A supervisor
    waiting on the file will time out and say so, which is the right place for that failure
    to surface.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the destination, because `rename` is only atomic within one
        # filesystem and the system temp dir is frequently not the same one.
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(f"{port}\n")
            os.replace(temporary, path)
        except BaseException:
            # `os.replace` never ran, or ran and failed. Either way the temporary is ours.
            _discard(temporary)
            raise
    except OSError as exc:
        logger.warning("could not write the port file %s: %s", path, exc)
        return None
    logger.debug("wrote port %d to %s", port, path)
    return path


def remove(path: Path | None) -> None:
    """Delete the port file, if there is one. Never raises.

    A stale file is worse than a missing one -- it names a port that something else may by
    then be listening on -- so this runs on every exit path that gets the chance. The ones
    it cannot cover are `SIGKILL` and the power going out, which is why a reader should
    treat the contents as a hint to be confirmed by connecting rather than as a fact.
    """
    if path is None:
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - a directory we cannot write is the case
        logger.warning("could not remove the port file %s: %s", path, exc)


def read(path: Path) -> int | None:
    """The port in `path`, or `None` if it is absent, empty or not a port.

    Tolerant on purpose: a reader polling for this file races the writer on every platform
    where `rename` is not what the writer used, and "not a port yet" is a state to keep
    waiting through rather than an error to report.
    """
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.isdigit():
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def _discard(temporary: str) -> None:
    """Remove a temporary that never made it into place."""
    try:
        os.unlink(temporary)
    except OSError:  # pragma: no cover - already gone is the common case
        pass
