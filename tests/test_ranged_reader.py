"""Tests for the streaming range reader.

The reader is what keeps peak disk usage flat: it presents one part's byte range
as a file-like object without that part ever existing on disk. If its offset
arithmetic is wrong, uploads silently contain the wrong bytes -- so these tests
check content, not just lengths.
"""

from __future__ import annotations

import io

from arr_uploader.media.partition import Slice, balanced_slices
from arr_uploader.media.ranged_reader import RangedFileReader, probe_readable

MIB = 1024 * 1024


def make_source(tmp_path, size: int, name: str = "Movie.mkv"):
    """Write a file whose bytes encode their own offset, so errors are visible."""
    path = tmp_path / name
    with path.open("wb") as fh:
        for offset in range(0, size, 4096):
            block = min(4096, size - offset)
            fh.write(offset.to_bytes(8, "big") * (block // 8 + 1))
    return path


def test_reads_only_the_requested_window(tmp_path):
    path = make_source(tmp_path, 4096)
    reader = RangedFileReader(path, Slice(idx=1, offset=1024, size=512))

    with reader:
        data = reader.read()
    assert len(data) == 512
    assert reader.size == 512
    assert reader.name == "Movie.mkv"


def test_read_is_clamped_to_part_boundary(tmp_path):
    """Asking for more than the part holds must not spill into the next part."""
    path = make_source(tmp_path, 4096)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=100))

    with reader:
        data = reader.read(99999)
    assert len(data) == 100


def test_sequential_reads_reassemble_the_whole_file(tmp_path):
    """The end-to-end property that matters: concatenated reads must equal the
    original file exactly."""
    path = make_source(tmp_path, 65536)
    size = path.stat().st_size
    slices = balanced_slices(size, 16384)

    assembled = bytearray()
    for sl in slices:
        with RangedFileReader(path, sl) as reader:
            assembled.extend(reader.read())

    assert len(assembled) == size
    assert bytes(assembled) == path.read_bytes()


def test_reading_past_end_returns_empty(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=512))

    with reader:
        reader.read()
        assert reader.read() == b""


def test_seek_within_window(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=512, size=512))

    with reader:
        assert reader.seek(0) == 0
        assert reader.tell() == 0
        # seek is relative to the window, not the file: SEEK_END is the end of
        # the part (512), not the end of the underlying file.
        assert reader.seek(0, io.SEEK_END) == 512
        assert reader.seek(-8, io.SEEK_END) == 504
        assert len(reader.read()) == 8
        assert reader.tell() == 512


def test_seek_cur_is_relative(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=512))

    with reader:
        assert len(reader.read(100)) == 100
        assert reader.seek(50, io.SEEK_CUR) == 150
        assert reader.seek(0, io.SEEK_SET) == 0
        assert reader.seek(150, io.SEEK_SET) == 150
        assert len(reader.read(50)) == 50


def test_negative_seek_clamps_to_zero(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=512))

    with reader:
        assert reader.seek(-100) == 0


def test_custom_name_is_used(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=100), name="Movie.mkv.001")

    with reader:
        assert reader.name == "Movie.mkv.001"


def test_readinto_fills_buffer(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=256))

    with reader:
        buf = bytearray(64)
        count = reader.readinto(buf)
        assert count == 64
        assert reader.tell() == 64


def test_capabilities(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=1, offset=0, size=100))

    with reader:
        assert reader.readable()
        assert reader.seekable()
        assert not reader.writable()


def test_probe_readable_reports_consistency(tmp_path):
    path = make_source(tmp_path, 4096)
    sl = Slice(idx=2, offset=1024, size=2048)

    info = probe_readable(path, sl)
    assert info["declared_size"] == 2048
    assert info["read_size"] == 2048
    assert info["matches"] is True


def test_repr_is_informative(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=3, offset=100, size=200))

    with reader:
        text = repr(reader)
    assert "part=3" in text
    assert "size=200" in text


def test_context_manager_closes_handle(tmp_path):
    path = make_source(tmp_path, 1024)
    with RangedFileReader(path, Slice(idx=1, offset=0, size=100)) as reader:
        assert not reader._fh.closed
    assert reader._fh.closed


def test_part_index_exposed(tmp_path):
    path = make_source(tmp_path, 1024)
    reader = RangedFileReader(path, Slice(idx=7, offset=0, size=100))

    with reader:
        assert reader.part_index == 7