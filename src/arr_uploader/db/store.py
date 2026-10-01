"""SQLite persistence.

All access goes through a single connection guarded by a lock. SQLite is a
coarse tool compared to a client/server database, but this workload is a single
writer (one uploader worker) plus a short-lived CLI, so WAL plus
``BEGIN IMMEDIATE`` gives correct multi-process behaviour without the
operational weight of a server.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import ACTIVE_STATES, RESUMABLE_STATES, Job, JobState, Part, PartState

LOG = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"


def utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def iso_in(seconds: float) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Store:
    """Durable job queue and part ledger."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction. IMMEDIATE takes the write lock up front
        so two workers cannot both read, then race to write."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ setup

    def migrate(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.set_meta("schema_version", SCHEMA_VERSION)
        LOG.info("database ready", extra={"path": str(self.path), "schema": SCHEMA_VERSION})

    def set_meta(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    # ------------------------------------------------------------------- jobs

    def upsert_job(self, folder_path: str, **fields: Any) -> int:
        """Create a job for *folder_path*, or return the existing one.

        Intake is idempotent by folder: a webhook, an inbox drop, and the
        reconciler can all fire for the same movie without creating duplicates.
        A job that already finished is never silently reopened.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT id, state FROM jobs WHERE folder_path=?", (folder_path,)
            ).fetchone()

            if row is None:
                allowed = {
                    "movie_id", "imdb_id", "tmdb_id", "title", "year", "source",
                    "priority", "state", "size_bytes",
                }
                cols = {k: v for k, v in fields.items() if k in allowed}
                cols["folder_path"] = folder_path
                names = ", ".join(cols)
                marks = ", ".join("?" * len(cols))
                cur = conn.execute(f"INSERT INTO jobs({names}) VALUES({marks})", tuple(cols.values()))
                job_id = int(cur.lastrowid)
                self._event(conn, job_id, "job.created", f"source={cols.get('source', 'inbox')}")
                return job_id

            job_id = int(row["id"])
            if JobState(row["state"]).terminal:
                LOG.info("intake ignored, job already terminal", extra={"job": job_id, "state": row["state"]})
                return job_id

            # Backfill metadata on an open job without disturbing progress.
            allowed = {"movie_id", "imdb_id", "tmdb_id", "title", "year"}
            updates = {k: v for k, v in fields.items() if k in allowed}
            if updates:
                assign = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE jobs SET {assign}, updated_at=datetime('now') WHERE id=?",
                    (*updates.values(), job_id),
                )
            self._event(conn, job_id, "job.intake_refreshed")
            return job_id

    def get_job(self, job_id: int) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        job = Job.from_row(rows[0])
        job.parts = self.get_parts(job_id)
        return job

    def find_job_by_folder(self, folder_path: str) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE folder_path=?", (folder_path,))
        if not rows:
            return None
        job = Job.from_row(rows[0])
        job.parts = self.get_parts(job.id)
        return job

    def known_folders(self) -> set[str]:
        return {r["folder_path"] for r in self._query("SELECT folder_path FROM jobs")}

    def claim_next_job(self, worker_id: str, lease_seconds: int = 3600) -> Job | None:
        """Atomically claim the next runnable job and lease it to *worker_id*.

        Runnable means: not finished, not backed off, and either never started or
        carrying an expired lease. The expired-lease clause is the recovery path
        for a worker that was killed mid-upload.
        """
        now = utcnow()
        expires = iso_in(lease_seconds)
        placeholders = ", ".join("?" * len(RESUMABLE_STATES))

        with self._write() as conn:
            row = conn.execute(
                f"""
                SELECT id FROM jobs
                WHERE state IN ({placeholders})
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                (*RESUMABLE_STATES, now, now),
            ).fetchone()

            if row is None:
                return None

            job_id = int(row["id"])
            cur = conn.execute(
                """
                UPDATE jobs
                SET lease_owner=?, lease_expires_at=?, state=?, updated_at=datetime('now')
                WHERE id=? AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (worker_id, expires, JobState.SCANNING, job_id, now),
            )
            if cur.rowcount == 0:  # pragma: no cover - lost race, retry next poll
                return None

            self._event(conn, job_id, "job.claimed", f"worker={worker_id}")

        job = self.get_job(job_id)
        if job:
            LOG.info("claimed job", extra={"job": job_id, "folder": job.folder_name, "worker": worker_id})
        return job

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int = 3600) -> None:
        """Extend our lease. Called during long uploads so a healthy worker is
        never mistaken for a dead one."""
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=datetime('now') "
                "WHERE id=? AND lease_owner=?",
                (iso_in(lease_seconds), job_id, worker_id),
            )

    def set_state(self, job_id: int, state: JobState | str, error: str | None = None) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET state=?, last_error=?, updated_at=datetime('now') WHERE id=?",
                (str(state), error, job_id),
            )
            self._event(conn, job_id, "job.state", f"{state} error={error or '-'}")

    def set_partition(self, job_id: int, size_bytes: int, part_size: int, part_count: int) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET size_bytes=?, part_size=?, part_count=?, updated_at=datetime('now') "
                "WHERE id=?",
                (size_bytes, part_size, part_count, job_id),
            )
            self._event(
                conn, job_id, "job.partitioned",
                f"size={size_bytes} part_size={part_size} parts={part_count}",
            )

    def record_size(self, job_id: int, size_bytes: int) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET size_bytes=?, updated_at=datetime('now') WHERE id=?",
                (size_bytes, job_id),
            )

    def complete_job(self, job_id: int, worker_id: str | None = None) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL, "
                "completed_at=datetime('now'), updated_at=datetime('now'), last_error=NULL "
                "WHERE id=? AND (? IS NULL OR lease_owner=?)",
                (JobState.DONE, job_id, worker_id, worker_id),
            )
            self._event(conn, job_id, "job.done")

    def release_lease(self, job_id: int) -> None:
        """Drop the lease without judging the job, so another worker can retry."""
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=datetime('now') "
                "WHERE id=?",
                (job_id,),
            )

    def fail_job(self, job_id: int, error: str, delay_seconds: float, max_attempts: int) -> JobState:
        """Record a failure and schedule a retry, or give up permanently.

        Returns the resulting state so the caller can decide whether to alert.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT attempts FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            exhausted = attempts >= max_attempts
            state = JobState.FAILED if exhausted else JobState.DISCOVERED
            next_at = None if exhausted else iso_in(delay_seconds)

            conn.execute(
                "UPDATE jobs SET state=?, attempts=?, next_attempt_at=?, last_error=?, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=datetime('now') WHERE id=?",
                (state, attempts, next_at, error[:2000], job_id),
            )
            self._event(conn, job_id, "job.attempt_failed", f"attempt={attempts} error={error[:500]}")

        LOG.warning(
            "job attempt failed",
            extra={"job": job_id, "attempts": attempts, "exhausted": exhausted, "error": error[:500]},
        )
        return state

    def set_terminal(self, job_id: int, state: JobState, detail: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET state=?, last_error=?, lease_owner=NULL, lease_expires_at=NULL, "
                "completed_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                (str(state), detail[:2000], job_id),
            )
            self._event(conn, job_id, "job.terminal", detail)

    # ------------------------------------------------------------------ parts

    def replace_parts(self, job_id: int, parts: list[Part]) -> None:
        """Rewrite the partition plan, preserving rows already uploaded.

        Only safe while re-partitioning before any upload has succeeded, or when
        shrinking the ceiling: uploaded parts keep their row and are never
        silently discarded.
        """
        with self._write() as conn:
            existing = {
                int(r["idx"]): r
                for r in conn.execute("SELECT * FROM parts WHERE job_id=?", (job_id,)).fetchall()
            }
            for part in parts:
                prior = existing.pop(part.idx, None)
                if prior is not None and prior["file_id"]:
                    continue
                conn.execute(
                    """
                    INSERT INTO parts(job_id, idx, name, byte_offset, byte_size, state)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, idx) DO UPDATE SET
                        name=excluded.name,
                        byte_offset=excluded.byte_offset,
                        byte_size=excluded.byte_size
                    """,
                    (job_id, part.idx, part.name, part.byte_offset, part.byte_size, PartState.PENDING),
                )
            # Drop stale rows only when they carry no uploaded file.
            for idx, row in existing.items():
                if not row["file_id"]:
                    conn.execute("DELETE FROM parts WHERE job_id=? AND idx=?", (job_id, idx))

    def get_parts(self, job_id: int) -> list[Part]:
        rows = self._query("SELECT * FROM parts WHERE job_id=? ORDER BY idx", (job_id,))
        return [Part.from_row(r) for r in rows]

    def pending_parts(self, job_id: int) -> list[Part]:
        """Parts still needing upload, in order."""
        return [p for p in self.get_parts(job_id) if not p.uploaded]

    def mark_part_uploading(self, job_id: int, idx: int) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE parts SET state=?, attempts=attempts+1, last_error=NULL "
                "WHERE job_id=? AND idx=?",
                (PartState.UPLOADING, job_id, idx),
            )

    def record_upload(
        self,
        job_id: int,
        idx: int,
        *,
        chat_id: int,
        thread_id: int | None,
        message_id: int,
        file_id: str,
        file_size: int,
        verified: bool,
    ) -> None:
        """Persist a successful upload.

        The row is committed before the pipeline is allowed to consider the part
        done, so a crash between upload and bookkeeping costs at most a
        duplicate message rather than a lost file reference.
        """
        state = PartState.VERIFIED if verified else PartState.UPLOADED
        with self._write() as conn:
            conn.execute(
                """
                UPDATE parts
                SET state=?, chat_id=?, thread_id=?, message_id=?, file_id=?, file_size=?,
                    uploaded_at=datetime('now'), last_error=NULL
                WHERE job_id=? AND idx=?
                """,
                (state, chat_id, thread_id, message_id, file_id, file_size, job_id, idx),
            )
            self._event(
                conn, job_id, "part.uploaded",
                f"idx={idx} msg={message_id} size={file_size} verified={verified}",
            )

    def mark_part_unverified(self, job_id: int, idx: int) -> None:
        """Revoke a part's verified status while keeping its receipt.

        Used by tests and by the operator path for backing out a verification
        decision: the file stays in Telegram and the local data stays put.
        """
        with self._write() as conn:
            conn.execute(
                "UPDATE parts SET state=? WHERE job_id=? AND idx=?",
                (PartState.UPLOADED, job_id, idx),
            )
            self._event(conn, job_id, "part.unverified", f"idx={idx}")

    def mark_part_failed(self, job_id: int, idx: int, error: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE parts SET state=?, last_error=? WHERE job_id=? AND idx=?",
                (PartState.FAILED, error[:1000], job_id, idx),
            )

    def all_parts_verified(self, job_id: int) -> bool:
        """True only when every planned part is verified against its own size.

        This is the licence to delete, so it checks the strictest available
        facts: at least one part exists, every part reached the ``verified``
        state, and each recorded ``file_size`` equals the ``byte_size`` we
        planned. Checking merely for a ``file_id`` would let a part that was
        uploaded but never confirmed through the gate open the deletion path.
        """
        row = self._query(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN state=? AND file_id IS NOT NULL
                             AND file_size IS NOT NULL
                             AND file_size = byte_size THEN 1 ELSE 0 END) AS good,
                   COALESCE(SUM(CASE WHEN file_size IS NOT NULL
                                      AND file_size <> byte_size THEN 1 ELSE 0 END), 0) AS mismatched
            FROM parts WHERE job_id=?
            """,
            (PartState.VERIFIED, job_id),
        )[0]

        total = int(row["n"])
        good = int(row["good"] or 0)
        mismatched = int(row["mismatched"] or 0)

        if total == 0:
            return False

        if mismatched:
            LOG.error(
                "refusing deletion: verified part size disagrees with the plan",
                extra={"job": job_id, "mismatched_parts": mismatched},
            )
            return False

        return good == total

    # ----------------------------------------------------------------- events

    def _event(self, conn: sqlite3.Connection, job_id: int | None, event: str, detail: str = "") -> None:
        conn.execute(
            "INSERT INTO events(job_id, level, event, detail) VALUES(?, ?, ?, ?)",
            (job_id, "info", event, detail[:4000]),
        )

    def log_event(self, job_id: int | None, event: str, detail: str = "", level: str = "info") -> None:
        with self._write() as conn:
            self._event(conn, job_id, event, detail)

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def stats(self) -> dict[str, int]:
        rows = self._query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        return {r["state"]: int(r["n"]) for r in rows}