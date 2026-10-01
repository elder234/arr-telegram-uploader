"""Tests for folder scanning and stability detection.

Scanning must pick the main feature over samples and extras, and stability
detection must not fire while a file is still being written.
"""

from __future__ import annotations

import os

from arr_uploader.media.scan import ScanError, find_subtitles, find_videos, parse_movie_identity, scan
from arr_uploader.media.stability import is_stable, size_matches, snapshot, wait_until_stable

import pytest


def make_movie(folder, video_bytes=b"x" * 5000, name="Movie.mkv"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(video_bytes)
    return folder


def test_scan_picks_largest_video(tmp_path):
    """The feature must win over a smaller extra in the same folder."""
    root = make_movie(tmp_path / "Movie (2024)", video_bytes=b"x" * 5000)
    (root / "interview.mkv").write_bytes(b"x" * 100)

    result = scan(root)
    assert result.video.name == "Movie.mkv", "the feature must win over a smaller extra"


def test_scan_skips_sample_files(tmp_path):
    root = make_movie(tmp_path / "Movie (2024)")
    (root / "Movie-sample.mkv").write_bytes(b"x" * 2000)

    videos = find_videos(root)
    assert [v.name for v in videos] == ["Movie.mkv"]


def test_scan_finds_subsides_in_subfolder(tmp_path):
    root = make_movie(tmp_path / "Movie (2024)")
    subs = root / "Subs"
    subs.mkdir()
    (subs / "Movie.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")

    found = find_subtitles(root, root / "Movie.mkv")
    assert [p.name for p in found] == ["Movie.en.srt"]


def test_scan_empty_folder_raises(tmp_path):
    empty = tmp_path / "Empty"
    empty.mkdir()

    with pytest.raises(ScanError):
        scan(empty)


def test_scan_missing_dir_raises(tmp_path):
    with pytest.raises(ScanError):
        scan(tmp_path / "nope")


def test_scan_counts_ignored_artwork(tmp_path):
    root = make_movie(tmp_path / "Movie (2024)")
    (root / "poster.jpg").write_bytes(b"j" * 100)
    (root / "movie.nfo").write_text("<movie/>", encoding="utf-8")

    result = scan(root)
    assert result.video.name == "Movie.mkv"
    assert {p.name for p in result.ignored} == {"poster.jpg", "movie.nfo"}


def test_scan_folder_size_hardlink_once(tmp_path):
    """Radarr hardlinks the download; the same bytes must not be double counted."""
    root = make_movie(tmp_path / "Movie (2024)", video_bytes=b"x" * 8000)
    try:
        os.link(root / "Movie.mkv", root / "Movie.mkv.link")
    except (OSError, NotImplementedError):  # pragma: no cover
        return

    assert scan(root, upload_subtitles=False).folder_size == 8000


def test_zero_byte_video_is_not_a_candidate(tmp_path):
    root = tmp_path / "Movie"
    root.mkdir()
    (root / "empty.mkv").write_bytes(b"")

    assert find_videos(root) == []


def test_parse_movie_identity_with_year():
    assert parse_movie_identity("The Movie (2024)") == ("The Movie", 2024)


def test_parse_movie_identity_without_year():
    assert parse_movie_identity("The Movie") == ("The Movie", None)


def test_parse_movie_identity_keeps_inner_parens():
    assert parse_movie_identity("Movie (Director's Cut) (1999)") == ("Movie (Director's Cut)", 1999)


# ----------------------------------------------------------------- stability


def test_snapshot_reports_size_and_count(tmp_path):
    root = make_movie(tmp_path / "Movie", video_bytes=b"x" * 100)
    snap = snapshot(root)
    assert snap.size == 100
    assert snap.file_count == 1


def test_unchanged_folder_is_stable(tmp_path):
    root = make_movie(tmp_path / "Movie")
    assert is_stable(root, 0) is True


def test_writes_during_window_detected(tmp_path):
    """The key guard: a folder still being written must not look stable."""
    root = make_movie(tmp_path / "Movie", video_bytes=b"x" * 100)

    def mutate(_seconds: float) -> None:
        with (root / "extra.bin").open("wb") as fh:
            fh.write(b"y" * 500)

    assert is_stable(root, 0.01, sleep=mutate) is False


def test_mtime_change_alone_is_unstable(tmp_path):
    """A rename or touch changes mtime without changing size, and must still
    read as unstable.

    The mtime is pushed forward explicitly rather than relying on touch(), because
    filesystem timestamp granularity can be coarse enough that a touch lands in
    the same tick as the original write.
    """
    root = make_movie(tmp_path / "Movie", video_bytes=b"x" * 100)
    target = root / "Movie.mkv"

    def bump_mtime(_seconds: float) -> None:
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    assert is_stable(root, 0.01, sleep=bump_mtime) is False


def test_wait_until_stable_succeeds_when_quiet(tmp_path):
    root = make_movie(tmp_path / "Movie", video_bytes=b"x" * 100)
    assert wait_until_stable(root, 0, timeout_seconds=1) is True


def test_wait_until_stable_times_out_on_churn(tmp_path):
    root = tmp_path / "Movie"
    root.mkdir()
    counter = {"n": 0}

    def churn(_seconds: float) -> None:
        counter["n"] += 1
        (root / f"part{counter['n']}.bin").write_bytes(b"z" * 100)

    # stability_seconds must be > 0 here; with 0 the single-sample fast path
    # would accept the folder immediately.
    assert wait_until_stable(root, 0.01, timeout_seconds=0.2, poll_seconds=0.01, sleep=churn) is False


def test_size_matches_detects_new_files(tmp_path):
    """The deletion gate compares sizes to catch a file that appeared mid-upload."""
    root = make_movie(tmp_path / "Movie", video_bytes=b"x" * 500)
    assert size_matches(root, 500) is True

    (root / "new.mkv").write_bytes(b"y" * 100)
    assert size_matches(root, 500) is False