"""Tests for the SQLite store.

The behaviours worth pinning down are idempotency (three intake paths may report
one movie) and lease behaviour (two workers must not claim the same job, and a
crashed worker's job must become claimable again).
"""

from __future__ import annotations

import sqlite3

from arr_uploader.db.models import JobState, Part, PartState
from arr_uploader.db.store import Store

import pytest


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "uploader.db")


def test_migrate_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.set_meta("k", "v")
    store.migrate()
    store.migrate()
    assert store.get_meta("k") == "v"
    assert store.get_meta("schema_version") == "1"
    store.close()


def test_wal_mode_enabled(tmp_path):
    store = make_store(tmp_path)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    store.close()


def test_upsert_is_idempotent_per_folder(tmp_path):
    store = make_store(tmp_path)

    first = store.upsert_job("/data/media/Movie", title="Movie", source="inbox")
    second = store.upsert_job("/data/media/Movie", title="Movie", source="webhook")

    assert first == second, "same folder must resolve to one job"
    assert store.stats() == {"discovered": 1}
    store.close()


def test_upsert_backfills_metadata_without_losing_progress(tmp_path):
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie", source="inbox")
    store.set_state(job_id, JobState.UPLOADING)
    store.set_partition(job_id, 1000, 500, 2)

    store.upsert_job("/data/media/Movie", movie_id=7, title="Movie", source="reconcile")

    job = store.get_job(job_id)
    assert job.state == str(JobState.UPLOADING)
    assert job.part_count == 2
    assert job.movie_id == 7
    store.close()


def test_terminal_job_is_not_reopened(tmp_path):
    """A reconciler sweep must not resurrect a finished movie."""
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie", source="inbox")
    store.complete_job(job_id)

    store.upsert_job("/data/media/Movie", source="reconcile")

    job = store.get_job(job_id)
    assert job.state == str(JobState.DONE)
    store.close()


def test_claim_is_exclusive(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Movie")

    first = store.claim_next_job("worker-a")
    second = store.claim_next_job("worker-b")

    assert first is not None
    assert second is None, "a leased job must not be handed to a second worker"
    store.close()


def test_expired_lease_is_reclaimable(tmp_path):
    """This is the crash-recovery path."""
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Movie")

    claimed = store.claim_next_job("worker-a", lease_seconds=-1)
    assert claimed is not None

    # Lease already expired, so the job becomes available again.
    reclaimed = store.claim_next_job("worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.lease_owner == "worker-b"
    store.close()


def test_release_lease_makes_job_claimable(tmp_path):
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")
    store.claim_next_job("worker-a")
    store.release_lease(job_id)

    assert store.claim_next_job("worker-b") is not None
    store.close()


def test_backoff_blocks_immediate_retry(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Movie")
    store.claim_next_job("worker-a")

    store.fail_job(1, "boom", delay_seconds=300, max_attempts=5)

    assert store.claim_next_job("worker-b") is None, "backoff window must be respected"
    store.close()


def test_retry_until_attempts_exhausted_then_terminal(tmp_path):
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")

    for _ in range(3):
        store.claim_next_job("worker", lease_seconds=-1)
        state = store.fail_job(job_id, "boom", delay_seconds=-1, max_attempts=3)

    assert state == JobState.FAILED
    assert store.claim_next_job("worker") is None
    store.close()


def test_heartbeat_extends_only_own_lease(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Movie")
    store.claim_next_job("worker-a")

    store.heartbeat(1, "worker-b", lease_seconds=60)
    row = store._query("SELECT lease_owner FROM jobs WHERE id=1")[0]
    assert row["lease_owner"] == "worker-a", "another worker must not extend our lease"
    store.close()


def test_parts_persist_across_repartition(tmp_path):
    """Re-planning must not discard an already-uploaded part reference."""
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")

    store.replace_parts(job_id, [
        Part(job_id=job_id, idx=1, name="Movie.mkv.001", byte_offset=0, byte_size=100),
        Part(job_id=job_id, idx=2, name="Movie.mkv.002", byte_offset=100, byte_size=50),
    ])
    store.record_upload(job_id, 1, chat_id=-1, thread_id=None, message_id=7, file_id="F1", file_size=100, verified=True)

    store.replace_parts(job_id, [
        Part(job_id=job_id, idx=1, name="Movie.mkv.001", byte_offset=0, byte_size=100),
    ])

    parts = store.get_parts(job_id)
    assert len(parts) == 1
    assert parts[0].file_id == "F1", "uploaded part must survive repartitioning"
    assert parts[0].state == str(PartState.VERIFIED)
    store.close()


def test_pending_parts_excludes_uploaded(tmp_path):
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")
    store.replace_parts(job_id, [
        Part(job_id=job_id, idx=1, name="a", byte_offset=0, byte_size=10),
        Part(job_id=job_id, idx=2, name="b", byte_offset=10, byte_size=10),
    ])
    store.record_upload(job_id, 1, chat_id=-1, thread_id=None, message_id=1, file_id="F1", file_size=10, verified=True)

    pending = store.pending_parts(job_id)
    assert [p.idx for p in pending] == [2]
    store.close()


def test_all_parts_verified_gate(tmp_path):
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")
    store.replace_parts(job_id, [
        Part(job_id=job_id, idx=1, name="a", byte_offset=0, byte_size=10),
        Part(job_id=job_id, idx=2, name="b", byte_offset=10, byte_size=10),
    ])

    assert store.all_parts_verified(job_id) is False

    store.record_upload(job_id, 1, chat_id=-1, thread_id=None, message_id=1, file_id="F1", file_size=10, verified=True)
    assert store.all_parts_verified(job_id) is False

    store.record_upload(job_id, 2, chat_id=-1, thread_id=None, message_id=2, file_id="F2", file_size=10, verified=True)
    assert store.all_parts_verified(job_id) is True
    store.close()


def test_no_parts_means_not_verified(tmp_path):
    """An empty job must never satisfy the deletion gate by vacuous truth."""
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")
    assert store.all_parts_verified(job_id) is False
    store.close()


def test_unverified_part_blocks_deletion_gate(tmp_path):
    """A file_id alone must not open the deletion path.

    The gate previously counted ``file_id IS NOT NULL``, so a part that was
    uploaded but never confirmed would satisfy it. Only a verified state whose
    recorded size matches the plan counts.
    """
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")
    store.replace_parts(job_id, [
        Part(job_id=job_id, idx=1, name="a", byte_offset=0, byte_size=10),
    ])

    store.record_upload(
        job_id, 1, chat_id=-1, thread_id=None, message_id=1,
        file_id="F1", file_size=10, verified=False,
    )
    assert store.all_parts_verified(job_id) is False

    part = store.get_parts(job_id)[0]
    assert part.file_id == "F1", "the receipt itself is still recorded"
    assert part.uploaded is True, "uploaded tracks the id; verified is stricter"
    assert part.state == PartState.UPLOADED
    store.close()


def test_verified_part_with_wrong_size_blocks_deletion_gate(tmp_path):
    """A size that disagrees with the plan must refuse, not warn."""
    store = make_store(tmp_path)
    job_id = store.upsert_job("/data/media/Movie")
    store.replace_parts(job_id, [
        Part(job_id=job_id, idx=1, name="a", byte_offset=0, byte_size=10),
    ])

    store.record_upload(
        job_id, 1, chat_id=-1, thread_id=None, message_id=1,
        file_id="F1", file_size=9, verified=True,
    )
    assert store.all_parts_verified(job_id) is False
    store.close()


def test_unique_folder_index_prevents_duplicates(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Movie")
    store.upsert_job("/data/media/Movie")

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("INSERT INTO jobs(folder_path) VALUES('/data/media/Movie')")
    store.close()


def test_known_folders_for_reconciler(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/A")
    store.upsert_job("/data/media/B")
    assert store.known_folders() == {"/data/media/A", "/data/media/B"}
    store.close()


def test_events_are_recorded(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Movie", source="inbox")
    events = [r["event"] for r in store.recent_events()]
    assert "job.created" in events
    store.close()


def test_priority_orders_claims(tmp_path):
    store = make_store(tmp_path)
    store.upsert_job("/data/media/Low", priority=200)
    store.upsert_job("/data/media/High", priority=10)

    assert store.claim_next_job("w").folder_path.endswith("High")
    store.close()