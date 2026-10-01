"""Detecting a movie folder that has stopped changing.

Radarr fires its import event when it has finished writing, but a rename can
still land afterwards, and a competing download client may still be flushing.
We sample the folder twice and require the aggregate to be identical before
touching anything.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..statefs import tree_size

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FolderSnapshot:
    """Identity of a folder's contents at one instant."""

    size: int
    file_count: int
    newest_mtime: int

    def same_as(self, other: "FolderSnapshot") -> bool:
        return (
            self.size == other.size
            and self.file_count == other.file_count
            and self.newest_mtime == other.newest_mtime
        )

    def __str__(self) -> str:
        return f"size={self.size} files={self.file_count} mtime_ns={self.newest_mtime}"


def snapshot(path: str | os.PathLike[str]) -> FolderSnapshot:
    """Capture size, file count, and newest mtime under *path*."""
    root = Path(path)
    total = 0
    count = 0
    newest = 0

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                # Vanished mid-walk; the folder is still changing.
                LOG.debug("stat failed during snapshot", extra={"path": str(full)})
                return FolderSnapshot(size=-1, file_count=-1, newest_mtime=time.time_ns())
            total += st.st_size
            count += 1
            # Nanoseconds, not float seconds: two writes can land inside the same
            # millisecond, and float seconds would report them as identical.
            newest = max(newest, st.st_mtime_ns)

    return FolderSnapshot(size=total, file_count=count, newest_mtime=newest)


def is_stable(path: str | os.PathLike[str], stability_seconds: float, sleep: object = time.sleep) -> bool:
    """Return True when *path* is unchanged across two samples.

    Blocking by design: the caller is deciding whether a movie is safe to read,
    so it is not an operation to fire off into the background. With
    ``stability_seconds == 0`` a single sample is enough, which is only
    appropriate in tests.
    """
    first = snapshot(path)
    if first.size < 0:
        return False

    if stability_seconds <= 0:
        # Single-sample fast path. Still takes a second sample so a folder that is
        # mid-write during this call is caught rather than accepted blindly.
        second = snapshot(path)
        return first.same_as(second)

    sleep(stability_seconds)

    second = snapshot(path)
    stable = first.same_as(second)
    LOG.info(
        "stability check",
        extra={"path": str(path), "stable": stable, "first": str(first), "second": str(second)},
    )
    return stable


def wait_until_stable(
    path: str | os.PathLike[str],
    stability_seconds: float,
    timeout_seconds: float = 3600,
    poll_seconds: float = 10,
    sleep: object = time.sleep,
) -> bool:
    """Poll until the folder looks stable for a full ``stability_seconds``.

    Distinct from :func:`is_stable`, which samples once. This retries, because a
    folder actively being written will never pass a single two-sample check.
    """
    deadline = time.monotonic() + timeout_seconds
    attempts = 0

    while True:
        attempts += 1
        if is_stable(path, stability_seconds, sleep=sleep):
            LOG.info("folder stable", extra={"path": str(path), "attempts": attempts})
            return True
        if time.monotonic() >= deadline:
            LOG.warning(
                "folder never became stable",
                extra={"path": str(path), "attempts": attempts, "timeout": timeout_seconds},
            )
            return False
        sleep(min(poll_seconds, max(timeout_seconds - (deadline - time.monotonic()), 0.1)))


def size_matches(path: str | os.PathLike[str], expected: int) -> bool:
    """Compare current folder size against what was recorded pre-upload.

    The deletion gate calls this: if the size moved, a new file appeared while
    we were uploading and must not be swept away.
    """
    current = tree_size(path)
    return current == expected