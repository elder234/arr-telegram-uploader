"""Media handling: scanning, stability checks, partitioning, streaming."""

from .partition import PartitionError, Slice, balanced_part_size, balanced_slices, part_count_for

__all__ = [
    "PartitionError",
    "Slice",
    "balanced_part_size",
    "balanced_slices",
    "part_count_for",
]