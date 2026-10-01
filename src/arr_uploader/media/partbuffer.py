"""Single-part disk buffer (fallback path).

Primary uploads use :mod:`.ranged_reader`, which costs no extra disk. If a
Kurigram release turns out to require a real on-disk path, we degrade to
buffering *one* part at a time into a reused scratch file. That is the important
difference from mirror-leech, which writes every part to disk first and so peaks
at roughly double the movie's size.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .partition import DEFAULT_CHUNK, Slice

LOG = logging.getLogger(__name__)


class PartBuffer:
    """Materialises one slice at a time into a reusable scratch file."""

    def __init__(self, buffer_dir: str | os.PathLike[str], chunk: int = DEFAULT_CHUNK) -> None:
        self.dir = Path(buffer_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chunk = chunk
        self._path = self.dir / "part.bin"
        self._part = 0

    @property
    def path(self) -> Path:
        return self._path

    def _copy_range(self, src: Path, sl: Slice) -> int:
        """Copy ``sl`` out of *src* into the scratch file. Returns bytes written."""
        written = 0
        remaining = sl.size

        with src.open("rb") as fin, self._path.open("wb") as fout:
            fin.seek(sl.offset)
            while remaining > 0:
                block = fin.read(min(self.chunk, remaining))
                if not block:
                    break
                fout.write(block)
                written += len(block)
                remaining -= len(block)

        if written != sl.size:
            raise IOError(f"short read on part {sl.idx}: wrote {written} of {sl.size}")

        return written

    @contextmanager
    def materialize(self, src: str | os.PathLike[str], sl: Slice) -> Iterator[Path]:
        """Yield a scratch path holding exactly *sl*.

        The previous part's bytes are overwritten in place, so peak extra disk
        is one part regardless of how many parts the movie has.
        """
        source = Path(src)
        if not source.is_file():
            raise FileNotFoundError(source)

        self._part = sl.idx
        written = self._copy_range(source, sl)
        LOG.info(
            "part buffered to disk",
            extra={"source": source.name, "part": sl.idx, "size": written, "path": str(self._path)},
        )

        try:
            yield self._path
        finally:
            # Truncate rather than unlink so a concurrent reader keeps its inode.
            try:
                with self._path.open("wb"):
                    pass
            except OSError:  # pragma: no cover - best effort
                LOG.warning("could not clear part buffer", extra={"path": str(self._path)})

    def cleanup(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            LOG.warning("could not remove part buffer", extra={"path": str(self._path)})

    def peak_bytes(self, part_size: int) -> int:
        """Extra disk required, for capacity planning."""
        return part_size