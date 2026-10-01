"""Streaming byte-range reader.

Gives Kurigram a file-like object that presents only one part's byte range, so
parts never exist on disk. The object implements the ``read``/``seek``/``tell``
surface the MTProto uploader uses, and reports a ``name`` and ``size`` so the
emitted document is named correctly without a temp file.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import BinaryIO

from .partition import Slice

LOG = logging.getLogger(__name__)


class RangedFileReader(io.RawIOBase):
    """A read-only window onto ``[offset, offset+size)`` of a file.

    Reads are clamped to the window, so a caller cannot accidentally read the
    head of the next part. Position tracking follows normal file semantics,
    including ``seek`` past the end, because the MTProto uploader probes the
    stream before uploading.
    """

    def __init__(self, path: str | Path, sl: Slice, *, name: str | None = None, chunk: int = 8 * 1024 * 1024) -> None:
        self._path = Path(path)
        self._slice = sl
        self._name = name or self._path.name
        self._chunk = chunk
        self._pos = 0  # position relative to the window start
        self._fh: BinaryIO = self._path.open("rb")
        self._fh.seek(sl.offset)

    # ------------------------------------------------------------- properties

    @property
    def name(self) -> str:
        """Filename Telegram will display for this part."""
        return self._name

    @property
    def size(self) -> int:
        return self._slice.size

    @property
    def part_index(self) -> int:
        return self._slice.idx

    @property
    def path(self) -> Path:
        return self._path

    # ----------------------------------------------------------- file protocol

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._slice.size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")

        self._pos = max(target, 0)
        self._fh.seek(self._slice.offset + self._pos)
        return self._pos

    def read(self, size: int = -1) -> bytes:
        remaining = self._slice.size - self._pos
        if remaining <= 0:
            return b""

        want = remaining if size is None or size < 0 else min(size, remaining)
        want = min(want, self._chunk) if self._chunk else want

        data = self._fh.read(want)
        if not data:
            return b""
        self._pos += len(data)
        return data

    def readinto(self, buffer: object) -> int:
        view = memoryview(buffer)  # type: ignore[arg-type]
        data = self.read(len(view))
        if not data:
            return 0
        view[: len(data)] = data
        return len(data)

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        super().close()

    def __enter__(self) -> "RangedFileReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"RangedFileReader({self._path.name!r}, part={self._slice.idx}, "
            f"offset={self._slice.offset}, size={self._slice.size})"
        )


def probe_readable(path: str | Path, sl: Slice, name: str | None = None) -> dict[str, object]:
    """Read a slice once and describe it.

    Used by the pipeline to confirm the window maps to the expected bytes before
    an upload begins, and by tests to validate offset arithmetic.
    """
    with RangedFileReader(path, sl, name=name) as reader:
        payload = reader.read()
        return {
            "name": reader.name,
            "declared_size": reader.size,
            "read_size": len(payload),
            "matches": len(payload) == sl.size,
            "head": payload[:16],
        }