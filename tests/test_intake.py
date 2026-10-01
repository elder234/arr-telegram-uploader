"""Tests for intake: inbox parsing, atomicity, idempotency, and the reconciler.

Intake is the only place a movie enters the queue, so the properties that matter
are that a malformed payload cannot wedge the watcher and that three paths
reporting one movie still produce one job.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from arr_uploader.db.store import Store
from arr_uploader.intake.inbox import InboxWatcher, parse_job_payload, read_job_file
from arr_uploader.intake.reconciler import Reconciler

import pytest


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "uploader.db")


# ------------------------------------------------------------------ payloads


def test_parse_modern_radarr_fields():
    request = parse_job_payload({
        "folderPath": "/data/media/Movie (2024)",
        "movieId": 42,
        "title": "Movie",
        "year": 2024,
        "tmdbId": 1234,
    })
    assert request.folder_path == "/data/media/Movie (2024)"
    assert request.movie_id == 42
    assert request.tmdb_id == 1234
    assert request.year == 2024


def test_parse_env_var_style_payload():
    """Radarr v3/v4 supplies these as environment variables."""
    request = parse_job_payload({
        "RADARR_MOVIE_PATH": "/data/media/Movie (2024)",
        "RADARR_MOVIE_ID": "42",
        "RADARR_MOVIE_TITLE": "Movie",
        "RADARR_MOVIE_YEAR": "2024",
    })
    assert request.folder_path == "/data/media/Movie (2024)"
    assert request.movie_id == 42
    assert request.year == 2024


def test_parse_imdb_style_id():
    request = parse_job_payload({
        "folderPath": "/data/media/Movie",
        "movieId": "tt1234567",
    })
    assert request.imdb_id == "tt1234567"
    assert request.movie_id is None


def test_parse_without_folder_raises():
    with pytest.raises(ValueError) as exc:
        parse_job_payload({"title": "Movie"})
    assert "folder" in str(exc.value)


def test_parse_tolerates_empty_optionals():
    request = parse_job_payload({"folderPath": "/data/media/Movie", "year": "", "movieId": None})
    assert request.year is None
    assert request.movie_id is None


def test_read_job_file(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"folderPath": "/data/media/Movie"}), encoding="utf-8")

    assert read_job_file(path).folder_path == "/data/media/Movie"


# --------------------------------------------------------------------- inbox


def test_inbox_creates_dir(tmp_path):
    inbox = tmp_path / "inbox"
    InboxWatcher(inbox, make_store(tmp_path))
    assert inbox.is_dir()


def test_inbox_enqueues_pending_file(tmp_path):
    store = make_store(tmp_path)
    inbox = tmp_path / "inbox"
    watcher = InboxWatcher(inbox, store)

    watcher.write_inbox_file({"folderPath": str(tmp_path / "Movie"), "title": "Movie"})

    assert watcher.poll() == 1
    assert store.stats() == {"discovered": 1}


def test_inbox_is_idempotent_on_duplicate_events(tmp_path):
    """Radarr can fire the same event twice; that must not create two jobs."""
    store = make_store(tmp_path)
    watcher = InboxWatcher(tmp_path / "inbox", store)

    for _ in range(3):
        watcher.write_inbox_file({"folderPath": str(tmp_path / "Movie")})
        watcher.poll()

    assert store.stats() == {"discovered": 1}


def test_inbox_processed_files_are_removed(tmp_path):
    store = make_store(tmp_path)
    inbox = tmp_path / "inbox"
    watcher = InboxWatcher(inbox, store)

    watcher.write_inbox_file({"folderPath": str(tmp_path / "Movie")})
    watcher.poll()

    assert list(inbox.iterdir()) == []


def test_malformed_file_is_discarded_not_wedged(tmp_path):
    """A bad payload must not stop the watcher from processing the next one."""
    store = make_store(tmp_path)
    inbox = tmp_path / "inbox"
    watcher = InboxWatcher(inbox, store)

    (inbox / "broken.json").write_text("{not json", encoding="utf-8")
    watcher.write_inbox_file({"folderPath": str(tmp_path / "Movie")})

    assert watcher.poll() == 1
    assert list(inbox.glob("*.json")) == []


def test_file_without_folder_is_discarded(tmp_path):
    store = make_store(tmp_path)
    inbox = tmp_path / "inbox"
    watcher = InboxWatcher(inbox, store)

    watcher.write_inbox_file({"title": "no path here"})
    assert watcher.poll() == 0


def test_stale_processing_file_is_reclaimed(tmp_path):
    """A crash mid-parse must not strand a movie forever."""
    store = make_store(tmp_path)
    inbox = tmp_path / "inbox"
    watcher = InboxWatcher(inbox, store, reconcile_after_seconds=0.0)

    watcher.write_inbox_file({"folderPath": str(tmp_path / "Movie")})
    pending = next(inbox.glob("*.json"))
    pending.rename(pending.with_suffix(".json.processing"))
    # Backdate so it looks abandoned.
    os.utime(pending.with_suffix(".json.processing"), (0, 0))

    assert watcher.poll() == 1


# ---------------------------------------------------------------- reconciler


def make_movie_folder(root, name="Movie (2024)", size=5000):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "movie.mkv").write_bytes(b"x" * size)
    return folder


def test_reconciler_enqueues_unknown_folder(tmp_path):
    media = tmp_path / "media"
    make_movie_folder(media)
    store = make_store(tmp_path)

    reconciler = Reconciler(media, store, min_age_seconds=0)
    assert reconciler.sweep() == 1
    assert store.stats() == {"discovered": 1}


def test_reconciler_skips_known_folders(tmp_path):
    """Already-queued movies must not be re-enqueued on every sweep."""
    media = tmp_path / "media"
    make_movie_folder(media)
    store = make_store(tmp_path)

    Reconciler(media, store, min_age_seconds=0).sweep()
    assert Reconciler(media, store, min_age_seconds=0).sweep() == 0
    store.close()


def test_reconciler_respects_min_age(tmp_path):
    """A folder that is still importing must be left alone."""
    media = tmp_path / "media"
    make_movie_folder(media)
    store = make_store(tmp_path)

    reconciler = Reconciler(media, store, min_age_seconds=86400)
    assert reconciler.sweep() == 0
    assert store.stats() == {}
    store.close()


def test_reconciler_ignores_non_movie_folders(tmp_path):
    media = tmp_path / "media"
    (media / "Extras").mkdir(parents=True)
    (media / "Extras" / "featurette.mkv").write_bytes(b"x" * 100)
    store = make_store(tmp_path)

    reconciler = Reconciler(media, store, min_age_seconds=0)
    assert reconciler.sweep() == 0, "configured skip-name should be ignored"
    store.close()


def test_reconciler_skips_folders_without_video(tmp_path):
    media = tmp_path / "media"
    (media / "Empty (2024)").mkdir(parents=True)
    (media / "Empty (2024)" / "readme.txt").write_text("nothing", encoding="utf-8")
    store = make_store(tmp_path)

    assert Reconciler(media, store, min_age_seconds=0).sweep() == 0
    store.close()


def test_reconciler_parses_identity_from_folder_name(tmp_path):
    media = tmp_path / "media"
    # No colon: that is illegal in a Windows directory name, so the folder-name
    # identity parse is exercised with a title that is valid everywhere.
    make_movie_folder(media, "The Long Goodnight (2024)")
    store = make_store(tmp_path)

    Reconciler(media, store, min_age_seconds=0).sweep()
    job = store.get_job(1)
    assert job.title == "The Long Goodnight"
    assert job.year == 2024
    assert job.year == 2024
    assert job.source == "reconcile"
    store.close()


def test_reconciler_bounds_each_pass(tmp_path):
    media = tmp_path / "media"
    for i in range(10):
        make_movie_folder(media, f"Movie {i} (2024)")
    store = make_store(tmp_path)

    reconciler = Reconciler(media, store, min_age_seconds=0, max_folders_per_pass=3)
    assert reconciler.sweep() == 3
    store.close()


def test_reconciler_missing_root_is_not_fatal(tmp_path):
    store = make_store(tmp_path)
    reconciler = Reconciler(tmp_path / "does-not-exist", store, min_age_seconds=0)
    assert reconciler.sweep() == 0
    store.close()


def test_reconciler_due_respects_interval(tmp_path):
    """due() is monotonic-vs-monotonic, independent of the sweep's clock."""
    store = make_store(tmp_path)
    reconciler = Reconciler(tmp_path / "media", store)

    reconciler.sweep()
    loop_now = time.monotonic()

    assert reconciler.due(loop_now + 100, 900) is False
    assert reconciler.due(loop_now + 1000, 900) is True
    store.close()


def test_reconciler_sweep_uses_wall_clock_not_monotonic(tmp_path):
    """Regression: the worker passed loop.time() to sweep().

    A monotonic clock differenced against epoch-based st_mtime is hugely negative,
    so every folder read as "too fresh" and the reconciler silently enqueued
    nothing. A stale folder must be picked up regardless of which clock is used
    for due().
    """
    store = make_store(tmp_path)
    media = tmp_path / "media"
    movie = media / "Movie (2020)"
    movie.mkdir(parents=True)
    (movie / "movie.mkv").write_bytes(b"x" * 4096)

    stale = time.time() - 7200
    os.utime(movie, (stale, stale))

    reconciler = Reconciler(media, store, min_age_seconds=60)
    assert reconciler.sweep() == 1
    assert any(Path(p).name == movie.name for p in store.known_folders())

    # A folder that genuinely just changed must still be left alone.
    fresh = media / "Movie (2021)"
    fresh.mkdir()
    (fresh / "movie.mkv").write_bytes(b"x" * 4096)
    assert reconciler.sweep() == 0
    store.close()