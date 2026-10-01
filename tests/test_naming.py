"""Tests for part naming.

Two guarantees: emitted names never exceed the length cap, and zero padding keeps
parts in numeric order when a chat client sorts them as text.
"""

from __future__ import annotations

from arr_uploader.naming import render_part_name, sanitize, subtitle_name


def test_default_template_matches_gnu_split():
    """Must stay byte-compatible with `split --numeric-suffixes=1 --suffix-length=3`."""
    assert render_part_name(movie_filename="Movie.mkv", index=1) == "Movie.mkv.001"
    assert render_part_name(movie_filename="Movie.mkv", index=42) == "Movie.mkv.042"


def test_zero_padding_keeps_numeric_order():
    names = [render_part_name(movie_filename="M.mkv", index=i, part_index_width=3) for i in range(1, 12)]
    assert names[:3] == ["M.mkv.001", "M.mkv.002", "M.mkv.003"]
    assert "M.mkv.010" in names
    assert names == sorted(names), "names must sort in upload order"


def test_index_zero_has_no_suffix():
    assert render_part_name(movie_filename="Movie.mkv", index=0) == "Movie.mkv"


def test_extension_is_preserved():
    name = render_part_name(movie_filename="Some.Movie.2024.1080p.mkv", index=3)
    assert name == "Some.Movie.2024.1080p.mkv.003"


def test_file_without_extension():
    assert render_part_name(movie_filename="moviefile", index=2) == "moviefile.002"


def test_sanitize_strips_illegal_characters():
    assert sanitize('a/b\\c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert sanitize("trailing dots and spaces .  ") == "trailing dots and spaces"
    assert sanitize("null\x00byte") == "null_byte"


def test_colon_becomes_space_for_readable_titles():
    # "Dune: Part Two" should not become "Dune_ Part Two".
    assert sanitize("Dune: Part Two") == "Dune Part Two"


def test_control_characters_removed():
    assert sanitize("line1\nline2\tend") == "line1 line2 end"


def test_long_name_is_truncated_but_keeps_extension_and_part():
    long_stem = "A" * 400
    name = render_part_name(movie_filename=f"{long_stem}.mkv", index=7, max_length=60)
    assert len(name) <= 60
    assert name.endswith(".mkv.007"), "extension and part suffix must survive truncation"
    assert name.startswith("A")


def test_truncation_at_exact_boundary():
    # Stem + ext + part is exactly max_length; nothing should be cut.
    stem = "B" * 10
    name = render_part_name(movie_filename=f"{stem}.mkv", index=1, max_length=len("B" * 10) + 4 + 4)
    assert name == f"{stem}.mkv.001"


def test_pathological_stem_still_yields_part_suffix():
    name = render_part_name(movie_filename="X.mkv", index=3, max_length=5)
    assert name == ".003"


def test_custom_template_tokens():
    name = render_part_name(
        movie_filename="Movie.mkv",
        index=4,
        template="{title} ({year}) - {basename}{ext}{part}",
        title="Dune: Part Two",
        year=2024,
    )
    assert name == "Dune Part Two (2024) - Movie.mkv.004"


def test_max_length_respected_with_custom_template():
    name = render_part_name(
        movie_filename="Movie.mkv",
        index=1,
        template="{title}{part}",
        title="T" * 300,
        max_length=40,
    )
    assert len(name) <= 40
    assert name.endswith(".001")


def test_subtitle_name_keeps_video_stem():
    name = subtitle_name("Movie.2024.mkv", "Movie.2024.en.srt")
    assert name == "Movie.2024.Movie.2024.en.srt"


def test_subtitle_name_respects_cap():
    name = subtitle_name("V.mkv", f"{'S' * 400}.srt", max_length=32)
    assert len(name) <= 32
    assert name.endswith(".srt")


def test_unicode_preserved():
    name = render_part_name(movie_filename="Amélie.mkv", index=1)
    assert name == "Amélie.mkv.001"