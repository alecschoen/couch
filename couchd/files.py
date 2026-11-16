"""Writing files so that a power cut leaves either the old file or the new one, never half.

The Pi loses power without warning (#25), so every file Couch keeps across reboots is
replaced the same way: write a temporary file next to it, fsync it, rename it over the old
one, then fsync the directory so the rename itself is on the card. ext4 keeps a rename
atomic; the two fsyncs make sure that what the rename points at is really written.

    write_atomic(Path("/var/lib/couch/settings.json"), b"{...}\\n")

A cut between the temporary file and the rename leaves a `.NAME.couch-tmp.*` file behind,
and `remove_leftovers` clears those up. Standard library only: install.sh runs code that uses
this (couchd.kodi_profile) before couchd's own dependencies matter.
"""

import contextlib
import os
import tempfile
from pathlib import Path

TMP_INFIX = ".couch-tmp."


def write_atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    """Replace `path` with `data`. `mode` defaults to the old file's, or 0o644."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}{TMP_INFIX}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)


def fsync_dir(directory: Path) -> None:
    """Make a rename or unlink in `directory` durable."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_leftovers(path: Path) -> list[Path]:
    """Delete the temporary files a power cut left next to `path`. Returns what it removed."""
    path = Path(path)
    removed = []
    with contextlib.suppress(FileNotFoundError):
        for p in path.parent.glob(f".{path.name}{TMP_INFIX}*"):
            with contextlib.suppress(OSError):
                p.unlink()
                removed.append(p)
    return removed
