"""Tests for balanced partitioning.

The safety property is the ceiling invariant: no part may exceed the configured
limit, because an oversized part is rejected by Telegram and, worse, could make
the deletion gate's accounting wrong.
"""

from __future__ import annotations

import pytest

from arr_uploader.media.partition import (
    PartitionError,
    balanced_part_size,
    balanced_slices,
    part_count_for,
    slices_cover_exactly,
    total_size,
)

MIB = 1024 * 1024


def test_single_part_when_under_ceiling():
    slices = balanced_slices(500 * MIB, 1900 * MIB)
    assert len(slices) == 1
    assert slices[0].size == 500 * MIB
    assert slices[0].offset == 0


def test_exact_multiple_divides_evenly():
    size = 3800 * MIB
    slices = balanced_slices(size, 1900 * MIB)
    assert len(slices) == 2
    # Equal parts, not one full and one empty.
    assert slices[0].size == slices[1].size == 1900 * MIB


def test_one_byte_over_ceiling_needs_two_parts():
    size = 1900 * MIB + 1
    slices = balanced_slices(size, 1900 * MIB)
    assert len(slices) == 2
    assert sum(s.size for s in slices) == size


def test_parts_are_balanced_not_full_then_tiny():
    """The point of balanced splitting: no runt final part."""
    size = 4000 * MIB
    ceiling = 1900 * MIB
    slices = balanced_slices(size, ceiling)

    assert len(slices) == 3
    sizes = [s.size for s in slices]
    # Each part is at least 90% of the average; the smallest is never a runt.
    average = size / 3
    assert min(sizes) > average * 0.9
    assert max(sizes) <= ceiling


def test_ceiling_invariant_holds_across_sizes():
    ceiling = 1900 * MIB
    for size_mib in (1, 100, 1899, 1900, 1901, 5000, 20000, 100000):
        size = size_mib * MIB
        for sl in balanced_slices(size, ceiling):
            assert sl.size <= ceiling, f"part {sl.idx} exceeded ceiling for size {size_mib} MiB"


def test_slices_are_contiguous_and_ordered():
    slices = balanced_slices(9999 * MIB, 1900 * MIB)
    assert [s.idx for s in slices] == list(range(1, len(slices) + 1))

    cursor = 0
    for sl in slices:
        assert sl.offset == cursor
        cursor += sl.size
    assert cursor == 9999 * MIB
    assert slices_cover_exactly(slices, 9999 * MIB)


def test_part_end_property():
    sl = balanced_slices(100 * MIB, 1900 * MIB)[0]
    assert sl.end == sl.offset + sl.size


def test_more_than_99_parts_keeps_ordering_width():
    # 500 parts forces the index past two digits; zero padding keeps sort order.
    size = 500 * 100 * MIB
    ceiling = 100 * MIB
    slices = balanced_slices(size, ceiling)
    assert len(slices) == 500
    assert slices_cover_exactly(slices, size)
    # Sorting by name must equal sorting by index.
    from arr_uploader.naming import render_part_name

    names = [render_part_name(movie_filename="Movie.mkv", index=s.idx, part_index_width=3) for s in slices]
    assert names[0] == "Movie.mkv.001"
    assert names[8] == "Movie.mkv.009"
    assert names[9] == "Movie.mkv.010"
    assert names[99] == "Movie.mkv.100"
    assert names == sorted(names)


def test_empty_file_rejected():
    with pytest.raises(PartitionError):
        balanced_slices(0, 1900 * MIB)


def test_negative_size_rejected():
    with pytest.raises(PartitionError):
        balanced_slices(-1, 1900 * MIB)


@pytest.mark.parametrize("ceiling", [0, -1])
def test_invalid_ceiling_rejected(ceiling):
    with pytest.raises(PartitionError):
        balanced_slices(100 * MIB, ceiling)


def test_part_count_and_size_helpers_agree():
    size = 7777 * MIB
    ceiling = 1900 * MIB
    n = part_count_for(size, ceiling)
    assert n == 5
    assert balanced_part_size(size, ceiling) == -(-size // n)


def test_total_size_matches_input():
    size = 12_345 * MIB
    ceiling = 1900 * MIB
    assert total_size(balanced_slices(size, ceiling)) == size


def test_tiny_file_single_part():
    slices = balanced_slices(1, 1900 * MIB)
    assert len(slices) == 1
    assert slices[0].size == 1