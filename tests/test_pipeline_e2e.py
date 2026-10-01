"""End-to-end pipeline tests.

These run the real :class:`Pipeline` against a fake Telegram client, and they are
the tests that matter most: they exercise the ordering rule that local data is
destroyed only after a verified upload. The negative cases (verification failure,
size drift, unsafe path) assert that data is *retained*, which is the property
that separates this design from mirror-leech.
"""

from __future__ import annotations

import asyncio

from pathlib import Path

from arr_uploader.config import Settings
from arr_uploader.db.models import JobState
from arr_uploader.db.store import Store
from arr_uploader.pipeline import Pipeline

import pytest


# --------------------------------------------------------------- fake client


class FakeDocument:
    def __init__(self, file_id: str, file_size: int) -> None:
        self.file_id = file_id
        self.file_size = file_size
        self.attributes = []


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, message_id: int, chat_id: int, document) -> None:
        self.id = message_id
        self.chat = FakeChat(chat_id)
        self.document = document
        self.video = None
        self.audio = None
        self.photo = None


class FakeTelegramClient:
    """Stands in for the Kurigram client.

    Reads the full payload from the reader so tests can prove the right bytes
    were sent, and can be told to misreport sizes or fail.
    """

    def __init__(self, *, size_delta: int = 0, fail_times: int = 0) -> None:
        self.size_delta = size_delta
        self.fail_times = fail_times
        self.uploads: list[tuple[str, bytes]] = []
        self._next_id = 1000
        self.chat_id = -1001234567890

    def stop_transmission(self) -> None:
        pass

    async def send_document(self, chat_id, document=None, caption=None, **_kw):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated upload failure")

        # Drain the reader to confirm the window maps to real bytes.
        payload = document.read() if hasattr(document, "read") else bytes(document)

        self._next_id += 1
        self.uploads.append((caption or "", payload))

        reported = len(payload) + self.size_delta
        return FakeMessage(
            message_id=self._next_id,
            chat_id=chat_id,
            document=FakeDocument(file_id=f"F{self._next_id}", file_size=reported),
        )


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fixtures


def build_settings(tmp_path, **overrides) -> Settings:
    media = tmp_path / "media"
    state = tmp_path / "state"
    media.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)

    settings = Settings()
    settings.paths.media_root = str(media)
    settings.paths.state_dir = str(state)
    settings.paths.inbox_dir = str(state / "inbox")
    settings.deletion.quarantine_dir = str(state / "quarantine")
    settings.telegram.chat_id = -1001234567890
    settings.telegram.api_id = 1
    settings.telegram.api_hash = "hash"
    settings.uploader.stability_seconds = 0
    settings.radarr.unmonitor_after_upload = False
    settings.radarr.exclude_after_upload = False
    settings.radarr.api_key = ""
    settings.radarr.url = ""

    # Keys are Python identifiers, e.g. "deletion.enabled", "telegram.chat_id".
    for key, value in overrides.items():
        target = settings
        *path, leaf = key.split(".")
        for part in path:
            target = getattr(target, part)
        setattr(target, leaf, value)

    return settings


def make_movie(tmp_path, settings, size=40_000, name="Movie (2024)", subtitle=True):
    folder = Path(settings.paths.media_root) / name
    folder.mkdir(parents=True, exist_ok=True)
    payload = bytes(range(256)) * (size // 256) + b"\x00" * (size % 256)
    (folder / "movie.mkv").write_bytes(payload)

    if subtitle:
        (folder / "movie.en.srt").write_text("1\n", encoding="utf-8")

    return folder, payload


async def instant_sleep(_seconds: float) -> None:
    """No-op delay so retry paths run without real waiting."""
    return None


def make_pipeline(settings, store, client):
    return Pipeline(settings, store, client, should_cancel=lambda: False, sleep=instant_sleep)


def enqueue(store, folder):
    job_id = store.upsert_job(str(folder), title="Movie", year=2024, source="test")
    return store.get_job(job_id)


# --------------------------------------------------------------------- tests


def test_happy_path_uploads_then_deletes(tmp_path):
    """The intended behaviour, in order: parts uploaded and verified, then gone."""
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, payload = make_movie(tmp_path, settings, size=40_000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient()
    pipeline = make_pipeline(settings, store, client)

    outcome = run(pipeline.run_job(job, "worker"))

    assert outcome.final_state == str(JobState.DONE)
    assert outcome.deleted is True
    assert not folder.exists(), "folder should be removed after a verified upload"
    assert len(client.uploads) >= 1

    # The bytes Telegram received must be exactly the movie, reassembled.
    reassembled = b"".join(data for _name, data in client.uploads)
    assert reassembled == payload, "uploaded bytes differ from the source file"
    store.close()


def test_part_naming_uses_split_convention(tmp_path):
    settings = build_settings(tmp_path, **{"telegram.part_ceiling_mb": 1})
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=2_500_000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient()
    run(make_pipeline(settings, store, client).run_job(job, "worker"))

    names = [name for name, _data in client.uploads]
    assert names == ["movie.mkv.001", "movie.mkv.002", "movie.mkv.003"]
    store.close()


def test_verification_failure_retains_local_data(tmp_path):
    """The critical negative case.

    Telegram under-reporting every size must not result in deletion. This is
    precisely where mirror-leech would have already removed the files.
    """
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=40_000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient(size_delta=-1)  # every upload looks short
    pipeline = make_pipeline(settings, store, client)

    with pytest.raises(Exception):
        run(pipeline.run_job(job, "worker"))

    assert folder.exists(), "local data must survive a verification failure"
    assert (folder / "movie.mkv").exists()
    store.close()


def test_unverified_parts_never_allow_deletion(tmp_path):
    """A file_id without a verified size must not unlock the delete path.

    The gate used to count any non-null ``file_id``, so a part that was uploaded
    but never confirmed satisfied it. Here the receipts are forged as
    ``verified=False`` with a plausible file_id: deletion must still be refused
    and the folder must survive.
    """
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=40_000, subtitle=False)

    job = enqueue(store, folder)
    run(make_pipeline(settings, store, FakeTelegramClient()).run_job(job, "worker"))

    # Re-create the folder: the happy path above deleted it.
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "movie.mkv").write_bytes(b"\0" * 40_000)

    # Downgrade every part to unverified while keeping its file_id.
    for part in store.get_parts(job.id):
        store.mark_part_unverified(job.id, part.idx)

    requeued = enqueue(store, folder)
    store.set_state(requeued.id, JobState.DISCOVERED)
    # The gate refuses before any cleanup runs, so this surfaces as a
    # PipelineError rather than a JobOutcome.
    with pytest.raises(Exception) as excinfo:
        run(make_pipeline(settings, store, FakeTelegramClient()).run_job(requeued, "worker"))

    assert "unverified" in str(excinfo.value), (
        f"the failure should name the unverified parts, got: {excinfo.value}"
    )
    assert folder.exists(), "the folder must be retained"
    assert (folder / "movie.mkv").exists()
    assert not store.all_parts_verified(requeued.id)
    store.close()


def test_media_root_itself_is_never_a_delete_target(tmp_path):
    """require_subdir: a job naming media_root must not hand rmtree the library."""
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")

    root = Path(settings.paths.media_root)
    keep = root / "Another Movie (2021)"
    keep.mkdir(parents=True)
    (keep / "keep.mkv").write_bytes(b"\0" * 1024)

    job = store.get_job(store.upsert_job(str(root)))
    client = FakeTelegramClient()

    outcome = run(make_pipeline(settings, store, client).run_job(job, "worker"))

    assert outcome.final_state == str(JobState.SKIPPED)
    assert "root itself" in outcome.detail, outcome.detail
    assert root.exists(), "media_root must survive"
    assert (keep / "keep.mkv").exists(), "unrelated movies must survive"
    assert not client.uploads, "nothing should have been uploaded from the root"
    store.close()


def test_upload_failure_retains_local_data(tmp_path):
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=40_000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient(fail_times=99)
    pipeline = make_pipeline(settings, store, client)

    with pytest.raises(Exception):
        run(pipeline.run_job(job, "worker"))

    assert folder.exists(), "local data must survive a failed upload"
    store.close()


def test_size_drift_blocks_deletion(tmp_path):
    """A file appearing mid-upload means the folder is not what we scanned."""
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=40_000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient()

    # Append a part after the job row is loaded but before the deletion gate runs.
    original_cleanup = Pipeline._cleanup

    async def cleanup_then_mutate(self, job_, folder_, scan_result, uploaded_bytes, detail):
        (folder_ / "appeared.mid.upload.mkv").write_bytes(b"x" * 5000)
        return await original_cleanup(self, job_, folder_, scan_result, uploaded_bytes, detail)

    Pipeline._cleanup = cleanup_then_mutate
    try:
        outcome = run(make_pipeline(settings, store, client).run_job(job, "worker"))
    finally:
        Pipeline._cleanup = original_cleanup

    assert outcome.deleted is False, "deletion must be refused when the size changed"
    assert folder.exists(), "the drifted folder must be kept"
    assert "kept local" in outcome.detail
    store.close()


def test_deletion_disabled_keeps_everything(tmp_path):
    settings = build_settings(tmp_path, **{"deletion.enabled": False})
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=40_000, subtitle=False)

    job = enqueue(store, folder)
    outcome = run(make_pipeline(settings, store, FakeTelegramClient()).run_job(job, "worker"))

    assert outcome.deleted is False
    assert folder.exists()
    store.close()


def test_unsafe_path_is_skipped_without_touching_files(tmp_path):
    """A job whose folder escaped the media root must not be acted on."""
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x" * 100)

    job_id = store.upsert_job(str(outside), title="Evil", source="test")
    job = store.get_job(job_id)
    client = FakeTelegramClient()

    outcome = run(make_pipeline(settings, store, client).run_job(job, "worker"))

    assert outcome.final_state == str(JobState.SKIPPED)
    assert (outside / "secret.mkv").exists(), "nothing outside the library may be touched"
    assert client.uploads == []
    store.close()


def test_subtitles_uploaded_separately(tmp_path):
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, payload = make_movie(tmp_path, settings, size=40_000, subtitle=True)

    job = enqueue(store, folder)
    client = FakeTelegramClient()
    run(make_pipeline(settings, store, client).run_job(job, "worker"))

    names = [name for name, _d in client.uploads]
    video_uploads = [n for n in names if n.endswith(".mkv") or ".mkv." in n]
    sub_uploads = [n for n in names if n.endswith(".srt")]

    assert len(video_uploads) >= 1
    assert len(sub_uploads) == 1, "subtitle should be its own upload, not folded into a part"

    # The reassembled video must still equal the movie exactly.
    reassembled = b"".join(d for n, d in client.uploads if ".srt" not in n)
    assert reassembled == payload
    store.close()


def test_single_part_movie(tmp_path):
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder, payload = make_movie(tmp_path, settings, size=1000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient()
    outcome = run(make_pipeline(settings, store, client).run_job(job, "worker"))

    assert outcome.deleted is True
    assert client.uploads[0][1] == payload
    store.close()


def test_resume_skips_already_uploaded_parts(tmp_path):
    """Simulates a crash mid-movie: recorded parts must not be re-sent."""
    settings = build_settings(tmp_path, **{"telegram.part_ceiling_mb": 1})
    store = Store(tmp_path / "u.db")
    folder, payload = make_movie(tmp_path, settings, size=2_500_000, subtitle=False)

    job = enqueue(store, folder)
    client = FakeTelegramClient()
    pipeline = make_pipeline(settings, store, client)
    run(pipeline.run_job(job, "worker"))

    # Pretend the movie is still there and that part 1 was already recorded.
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "movie.mkv").write_bytes(payload)

    job2 = enqueue(store, folder)
    store.set_state(job2.id, JobState.DISCOVERED)

    parts = store.get_parts(job2.id)
    first = parts[0]
    store.record_upload(
        job2.id,
        first.idx,
        chat_id=-1001234567890,
        thread_id=None,
        message_id=999,
        file_id="ALREADY",
        file_size=first.byte_size,
        verified=True,
    )

    client2 = FakeTelegramClient()
    run(make_pipeline(settings, store, client2).run_job(job2, "worker"))

    names = [name for name, _d in client2.uploads]
    assert "movie.mkv.001" not in names, "an already-uploaded part must not be resent"
    assert len(client2.uploads) < len(parts)
    store.close()


def test_empty_folder_does_not_delete_anything(tmp_path):
    settings = build_settings(tmp_path)
    store = Store(tmp_path / "u.db")
    folder = Path(settings.paths.media_root) / "Empty (2024)"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("nothing here", encoding="utf-8")

    job = enqueue(store, folder)
    client = FakeTelegramClient()

    with pytest.raises(Exception):
        run(make_pipeline(settings, store, client).run_job(job, "worker"))

    assert folder.exists()
    assert (folder / "notes.txt").exists()
    store.close()


def test_all_parts_recorded_on_success(tmp_path):
    settings = build_settings(tmp_path, **{"telegram.part_ceiling_mb": 1})
    store = Store(tmp_path / "u.db")
    folder, _ = make_movie(tmp_path, settings, size=2_500_000, subtitle=False)

    job = enqueue(store, folder)
    run(make_pipeline(settings, store, FakeTelegramClient()).run_job(job, "worker"))

    parts = store.get_parts(job.id)
    assert len(parts) == 3
    assert all(p.file_id for p in parts), "every part needs a durable reference"
    assert all(p.state == "verified" for p in parts)
    assert store.all_parts_verified(job.id)
    store.close()

