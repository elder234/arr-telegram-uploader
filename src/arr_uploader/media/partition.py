"""Stable partitioning.

Splits are *balanced*: given a file of ``size`` bytes and a per-part ceiling, we
compute the smallest number of parts that fits under the ceiling and then divide
the file evenly among them. This avoids mirror-leech's typical layout where the
last part is a few hundred megabytes and every earlier part is full.

The invariant that matters is ``part_size <= ceiling``. It holds because
``n = ceil(size / ceiling)`` implies ``size / n <= ceiling``, and rounding a
value up to an integer that is itself an integer cannot exceed it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

LOG = logging.getLogger(__name__)

DEFAULT_CHUNK = 8 * 1024 * 1024  # 8 MiB read granularity


class PartitionError(ValueError):
    """Raised when a file cannot be partitioned sanely."""


@dataclass(frozen=True, slots=True)
class Slice:
    """One part of a file, described by byte range rather than materialised."""

    idx: int          # 1-based
    offset: int
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size


def part_count_for(size: int, ceiling: int) -> int:
    """Number of parts needed to fit *size* under *ceiling*."""
    if size < 0:
        raise PartitionError(f"negative size: {size}")
    if ceiling <= 0:
        raise PartitionError(f"ceiling must be positive, got {ceiling}")
    if size == 0:
        return 1
    return -(-size // ceiling)  # integer ceil-div


def balanced_part_size(size: int, ceiling: int) -> int:
    """Size of each part when *size* is split evenly across the minimum count."""
    n = part_count_for(size, ceiling)
    # ceil-div: guarantees the parts sum to >= size with no short part.
    return -(-size // n)


def balanced_slices(size: int, ceiling: int) -> list[Slice]:
    """Split ``[0, size)`` into balanced, contiguous, gapless slices.

    Parts are 1-based and ordered. Slice sizes are equal except for the last,
    which absorbs the remainder.
    """
    if size <= 0:
        raise PartitionError(f"cannot partition an empty file (size={size})")
    if ceiling <= 0:
        raise PartitionError(f"ceiling must be positive, got {ceiling}")

    n = part_count_for(size, ceiling)
    part_size = -(-size // n)

    slices: list[Slice] = []
    offset = 0
    for i in range(1, n + 1):
        length = min(part_size, size - offset)
        if length <= 0:
            break
        slices.append(Slice(idx=i, offset=offset, size=length))
        offset += length

    if offset != size:  # pragma: no cover - guarded by construction
        raise PartitionError(f"partition lost bytes: covered {offset} of {size}")

    LOG.debug(
        "partition computed",
        extra={"size": size, "ceiling": ceiling, "parts": len(slices), "part_size": part_size},
    )
    return slices


def slices_cover_exactly(slices: list[Slice], size: int) -> bool:
    """Invariant check used by tests and by the pipeline before uploading."""
    if not slices:
        return False
    expected = 1
    cursor = 0
    for s in slices:
        if s.idx != expected or s.offset != cursor:
            return False
        if s.size <= 0:
            return False
        cursor += s.size
        expected += 1
    return cursor == size


def total_size(slices: list[Slice]) -> int:
    return sum(s.size for s in slices)