"""Tests for path containment and tree sizing.

These guard the only irreversible operation in the system, so the interesting
cases are the escapes: symlinks, traversal, and sibling directories that share
a prefix with the media root.
"""

from __future__ import annotations

import os

from arr_uploader.statefs import UnsafePathError, move_to_quarantine, resolve_under, tree_size

import pytest


def test_resolve_accepts_child(tmp_path):
    root = tmp_path / "media"
    (root / "Movie (2024)").mkdir(parents=True)

    resolved = resolve_under(root, root / "Movie (2024)")
    assert resolved == (root / "Movie (2024)").resolve()


def test_relative_candidate_resolves_against_root(tmp_path):
    root = tmp_path / "media"
    (root / "Movie").mkdir(parents=True)

    assert resolve_under(root, "Movie") == (root / "Movie").resolve()


def test_traversal_is_rejected(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "etc"
    outside.mkdir()

    with pytest.raises(UnsafePathError):
        resolve_under(root, outside)


def test_dotdot_escape_rejected(tmp_path):
    root = tmp_path / "media"
    root.mkdir()

    with pytest.raises(UnsafePathError):
        resolve_under(root, root / ".." / "..")


def test_symlink_escape_rejected(tmp_path):
    """A symlink inside the library must not become a way out of it."""
    root = tmp_path / "media"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "private.mkv").write_bytes(b"x")

    link = root / "sneaky"
    try:
        link.symlink_to(secret, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        return  # cannot create symlinks here; nothing to assert

    with pytest.raises(UnsafePathError):
        resolve_under(root, link)


def test_sibling_prefix_is_not_treated_as_child(tmp_path):
    """'/data/media_evil' must not pass as '/data/media'.

    A naive startswith() check would allow this.
    """
    root = tmp_path / "media"
    root.mkdir()
    evil = tmp_path / "media_evil"
    evil.mkdir()

    with pytest.raises(UnsafePathError):
        resolve_under(root, evil)


def test_root_itself_is_allowed(tmp_path):
    """For a lookup, the root is a legitimate answer."""
    root = tmp_path / "media"
    root.mkdir()
    assert resolve_under(root, root) == root.resolve()


def test_require_subdir_rejects_the_root(tmp_path):
    """For anything destructive, the root must never be an acceptable target.

    Without this flag a job whose folder_path happened to name media_root would
    pass containment and hand rmtree the entire library.
    """
    root = tmp_path / "media"
    (root / "Movie").mkdir(parents=True)

    with pytest.raises(UnsafePathError):
        resolve_under(root, root, require_subdir=True)

    # Children still resolve, including a relative form.
    assert resolve_under(root, root / "Movie", require_subdir=True) == (root / "Movie").resolve()
    assert resolve_under(root, "Movie", require_subdir=True) == (root / "Movie").resolve()


def test_require_subdir_rejects_a_symlink_pointing_at_the_root(tmp_path):
    """A link to the root resolves to the root, so the subdir rule still bites."""
    root = tmp_path / "media"
    root.mkdir()
    link = tmp_path / "alias"
    try:
        link.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        return

    with pytest.raises(UnsafePathError):
        resolve_under(root, link, require_subdir=True)


def test_tree_size_sums_files(tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x" * 1000)
    (tmp_path / "b.srt").write_bytes(b"y" * 250)
    assert tree_size(tmp_path) == 1250


def test_tree_size_single_file(tmp_path):
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"z" * 42)
    assert tree_size(target) == 42


def test_tree_size_counts_hardlink_once(tmp_path):
    """Radarr hardlinks downloads into the library; the same inode must not be
    double counted, or the pre/post-upload size comparison never matches."""
    original = tmp_path / "orig.mkv"
    original.write_bytes(b"a" * 5000)
    link = tmp_path / "linked.mkv"

    try:
        os.link(original, link)
    except (OSError, NotImplementedError):  # pragma: no cover
        return

    assert tree_size(tmp_path) == 5000, "hardlinked inode counted twice"


def test_tree_size_walks_subdirectories(tmp_path):
    nested = tmp_path / "BDMV" / "STREAM"
    nested.mkdir(parents=True)
    (nested / "main.m2ts").write_bytes(b"m" * 700)
    (tmp_path / "movie.mkv").write_bytes(b"v" * 300)

    assert tree_size(tmp_path) == 1000


def test_tree_size_empty_dir(tmp_path):
    assert tree_size(tmp_path) == 0


def test_move_to_quarantine_preserves_data(tmp_path):
    movie = tmp_path / "Movie (2024)"
    movie.mkdir()
    (movie / "movie.mkv").write_bytes(b"payload")
    quarantine = tmp_path / "quarantine"

    moved = move_to_quarantine(movie, quarantine)

    assert moved.exists()
    assert (moved / "movie.mkv").read_bytes() == b"payload"
    assert not movie.exists(), "original must be gone from the library"
    assert quarantine.exists()


def test_quarantine_creates_dir_and_avoids_collision(tmp_path):
    first = tmp_path / "Movie"
    first.mkdir()
    (first / "a.mkv").write_bytes(b"first")
    quarantine = tmp_path / "q"

    move_to_quarantine(first, quarantine)

    second = tmp_path / "Movie"
    second.mkdir()
    (second / "b.mkv").write_bytes(b"second")
    moved2 = move_to_quarantine(second, quarantine)

    assert moved2.exists()
    assert (moved2 / "b.mkv").read_bytes() == b"second"