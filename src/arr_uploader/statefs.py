"""Filesystem safety primitives.

Every path that originates outside this process -- Radarr, a webhook, or a
filesystem sweep -- passes through :func:`resolve_under` before we stat, read, or
delete it. Anything that escapes the configured root is rejected rather than
silently followed.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

LOG = logging.getLogger(__name__)


class UnsafePathError(Exception):
    """Raised when a path does not resolve inside its declared root."""


def resolve_under(
    root: str | os.PathLike[str],
    candidate: str | os.PathLike[str],
    *,
    require_subdir: bool = False,
) -> Path:
    """Resolve *candidate* and assert it stays under *root*.

    Uses ``realpath`` on both sides so symlinks cannot be used to escape. The
    containment check is done with ``Path.is_relative_to`` rather than a string
    prefix test, which would wrongly accept ``/data/media_evil`` for root
    ``/data/media``.

    Set ``require_subdir`` for anything destructive. Without it the root itself is
    an acceptable answer, which is correct for a lookup but would let a
    misidentified job name ``media_root`` as its folder and hand ``rmtree`` the
    entire library.
    """
    root_real = Path(root).resolve()
    cand = Path(candidate)

    # Relative candidates are interpreted against the root itself, which is what
    # Radarr's Custom Script vars sometimes give us.
    if not cand.is_absolute():
        cand = root_real / cand

    cand_real = Path(os.path.realpath(cand))

    if cand_real == root_real:
        if require_subdir:
            raise UnsafePathError(f"{cand_real} is the root itself; a subdirectory is required")
        return cand_real

    if cand_real.is_relative_to(root_real):
        return cand_real

    raise UnsafePathError(f"{cand_real} escapes root {root_real}")


def tree_size(path: str | os.PathLike[str]) -> int:
    """Total bytes under *path*, counting each inode once.

    Hardlinks matter here: Radarr's copy/hardlink of a movie means the same
    inode can appear twice, and counting it twice would corrupt the
    before/after size comparison that gates deletion.
    """
    root = Path(path)
    if root.is_file():
        return root.stat().st_size

    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                st = full.lstat()
            except OSError:
                continue
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            total += st.st_size
    return total


def move_to_quarantine(path: str | os.PathLike[str], quarantine_dir: str | os.PathLike[str]) -> Path:
    """Relocate *path* into quarantine rather than destroying it.

    Used when any precondition for deletion cannot be proven. A quarantined
    movie is recoverable; a wrongly deleted one is not.
    """
    qdir = Path(quarantine_dir)
    qdir.mkdir(parents=True, exist_ok=True)

    src = Path(path)
    dest = qdir / src.name
    if dest.exists():
        stamp = str(int(os.path.getmtime(src)))
        dest = qdir / f"{src.name}.{stamp}"
        LOG.warning("quarantine target already present, using %s", dest)

    LOG.warning("quarantining %s -> %s", src, dest)
    shutil.move(str(src), str(dest))
    return dest