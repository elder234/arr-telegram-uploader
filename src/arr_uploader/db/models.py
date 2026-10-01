"""Row types and state enums shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class JobState(StrEnum):
    DISCOVERED = "discovered"
    SCANNING = "scanning"
    PARTITIONING = "partitioning"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    QUARANTINED = "quarantined"

    @property
    def terminal(self) -> bool:
        return self in {JobState.DONE, JobState.SKIPPED, JobState.QUARANTINED}


class PartState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    VERIFIED = "verified"
    FAILED = "failed"


# States a job may be in when a worker picks it up. Anything mid-flight is only
# reclaimable once its lease has expired.
ACTIVE_STATES: tuple[str, ...] = (
    JobState.SCANNING,
    JobState.PARTITIONING,
    JobState.UPLOADING,
    JobState.VERIFYING,
    JobState.FINALIZING,
)

RESUMABLE_STATES: tuple[str, ...] = (
    JobState.DISCOVERED,
    *ACTIVE_STATES,
)


@dataclass(slots=True)
class Part:
    job_id: int
    idx: int
    name: str
    byte_offset: int
    byte_size: int
    state: str = PartState.PENDING
    attempts: int = 0
    chat_id: int | None = None
    thread_id: int | None = None
    message_id: int | None = None
    file_id: str | None = None
    file_size: int | None = None
    last_error: str | None = None

    @property
    def uploaded(self) -> bool:
        return bool(self.file_id) and self.state in (PartState.UPLOADED, PartState.VERIFIED)

    @classmethod
    def from_row(cls, row: object) -> "Part":
        return cls(
            job_id=row["job_id"],  # type: ignore[index]
            idx=row["idx"],  # type: ignore[index]
            name=row["name"],  # type: ignore[index]
            byte_offset=row["byte_offset"],  # type: ignore[index]
            byte_size=row["byte_size"],  # type: ignore[index]
            state=row["state"],  # type: ignore[index]
            attempts=row["attempts"],  # type: ignore[index]
            chat_id=row["chat_id"],  # type: ignore[index]
            thread_id=row["thread_id"],  # type: ignore[index]
            message_id=row["message_id"],  # type: ignore[index]
            file_id=row["file_id"],  # type: ignore[index]
            file_size=row["file_size"],  # type: ignore[index]
            last_error=row["last_error"],  # type: ignore[index]
        )


@dataclass(slots=True)
class Job:
    id: int
    folder_path: str
    state: str = JobState.DISCOVERED
    movie_id: int | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    title: str = ""
    year: int | None = None
    size_bytes: int = 0
    part_size: int = 0
    part_count: int = 0
    source: str = "inbox"
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    priority: int = 100
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    parts: list[Part] = field(default_factory=list)

    @property
    def folder_name(self) -> str:
        from pathlib import PurePosixPath

        return PurePosixPath(self.folder_path).name

    @classmethod
    def from_row(cls, row: object) -> "Job":
        return cls(
            id=row["id"],  # type: ignore[index]
            folder_path=row["folder_path"],  # type: ignore[index]
            state=row["state"],  # type: ignore[index]
            movie_id=row["movie_id"],  # type: ignore[index]
            imdb_id=row["imdb_id"],  # type: ignore[index]
            tmdb_id=row["tmdb_id"],  # type: ignore[index]
            title=row["title"] or "",  # type: ignore[index]
            year=row["year"],  # type: ignore[index]
            size_bytes=row["size_bytes"] or 0,  # type: ignore[index]
            part_size=row["part_size"] or 0,  # type: ignore[index]
            part_count=row["part_count"] or 0,  # type: ignore[index]
            source=row["source"],  # type: ignore[index]
            attempts=row["attempts"] or 0,  # type: ignore[index]
            next_attempt_at=row["next_attempt_at"],  # type: ignore[index]
            last_error=row["last_error"],  # type: ignore[index]
            priority=row["priority"] or 100,  # type: ignore[index]
            lease_owner=row["lease_owner"],  # type: ignore[index]
            lease_expires_at=row["lease_expires_at"],  # type: ignore[index]
            created_at=row["created_at"] or "",  # type: ignore[index]
            updated_at=row["updated_at"] or "",  # type: ignore[index]
            completed_at=row["completed_at"],  # type: ignore[index]
        )