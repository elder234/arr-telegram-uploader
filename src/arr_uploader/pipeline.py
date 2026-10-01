"""The upload pipeline.

One job, one pass through a state machine:

    scan -> partition -> upload (verify each part) -> verify all -> finalize -> delete

The ordering that matters is at the end. Deletion is the *last* step and is
gated on re-proving every precondition, because it is the only irreversible
action in the system. Any doubt results in quarantine, never in ``rmtree``.

This is the deliberate inversion of mirror-leech, whose uploader removes each
file immediately after its upload attempt and whose error path calls
``clean_download``, destroying local data on failure.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, ceiling_bytes
from .db.models import Job, JobState, Part, PartState
from .db.store import Store
from .media.partition import PartitionError, Slice, balanced_slices, slices_cover_exactly
from .media.scan import ScanError, parse_movie_identity, scan
from .media.stability import is_stable, wait_until_stable
from .naming import render_part_name, subtitle_name
from .radarr import RadarrClient
from .statefs import UnsafePathError, move_to_quarantine, resolve_under, tree_size
from .telegram.uploader import PartUploadError, Uploader, UploadCancelled
from .telegram.verify import VerificationError

LOG = logging.getLogger(__name__)


class PipelineError(Exception):
    """A failure that should be retried rather than treated as terminal."""


@dataclass(slots=True)
class JobOutcome:
    job_id: int
    final_state: str
    parts_uploaded: int = 0
    bytes_uploaded: int = 0
    deleted: bool = False
    quarantined: bool = False
    detail: str = ""


@dataclass(slots=True)
class DeleteDecision:
    """Why the deletion gate said what it said."""

    allowed: bool
    reason: str
    detail: str = ""


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        telegram_client: Any,
        *,
        should_cancel: Any = lambda: False,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = telegram_client
        self.should_cancel = should_cancel
        # Injectable so tests can exercise retry paths without real delays.
        self.sleep = sleep
        self.radarr = RadarrClient(settings.radarr, event_log=store.log_event)

    # ------------------------------------------------------------------- entry

    async def run_job(self, job: Job, worker_id: str) -> JobOutcome:
        """Process one claimed job end to end."""
        LOG.info("processing job", extra={"job": job.id, "folder": job.folder_name, "state": job.state})

        try:
            folder = resolve_under(
                self.settings.paths.media_root, job.folder_path, require_subdir=True
            )
        except UnsafePathError as exc:
            # Nothing to trust here; refuse rather than guess.
            self.store.set_terminal(job.id, JobState.SKIPPED, f"unsafe path: {exc}")
            LOG.error("job path rejected", extra={"job": job.id, "error": str(exc)})
            return JobOutcome(job.id, JobState.SKIPPED, detail=str(exc))

        try:
            self.store.set_state(job.id, JobState.SCANNING)
            scan_result = await self._scan_stable(folder)
            self.store.set_state(job.id, JobState.PARTITIONING)
            slices, parts = self._plan_parts(job, scan_result)
            self.store.set_state(job.id, JobState.UPLOADING)
            uploaded = await self._upload_parts(job, scan_result, slices, parts, worker_id)
            self.store.set_state(job.id, JobState.VERIFYING)
            self._assert_all_verified(job.id)
            self.store.set_state(job.id, JobState.FINALIZING)
            detail = await self._finalize(job, scan_result)
            return await self._cleanup(job, folder, scan_result, uploaded, detail)

        except UploadCancelled as exc:
            self.store.release_lease(job.id)
            self.store.log_event(job.id, "job.cancelled", str(exc))
            LOG.info("job cancelled cleanly", extra={"job": job.id})
            return JobOutcome(job.id, str(JobState.UPLOADING), detail="cancelled for shutdown")

        except (ScanError, PartitionError) as exc:
            # Transient: the movie may still be settling.
            raise PipelineError(str(exc)) from exc

        except (PartUploadError, VerificationError) as exc:
            raise PipelineError(str(exc)) from exc

    # ------------------------------------------------------------------- steps

    async def _scan_stable(self, folder: Path):
        """Wait for the folder to quiesce, then scan it."""
        # wait_until_stable is blocking, so the pause happens off the event loop.
        stable = await asyncio.to_thread(
            wait_until_stable,
            folder,
            self.settings.uploader.stability_seconds,
            max(self.settings.uploader.stability_seconds * 20, 900),
        )
        if not stable:
            raise ScanError(f"{folder} never stopped changing")

        result = scan(folder, upload_subtitles=self.settings.naming.upload_subtitles)
        return result

    def _plan_parts(self, job: Job, scan_result) -> tuple[list[Slice], list[Part]]:
        """Compute the balanced split and persist it.

        The plan is written to the database *before* any upload so that a crash
        mid-movie resumes against the same names and offsets.
        """
        ceiling = ceiling_bytes(self.settings.telegram, is_premium=self._premium())
        slices = balanced_slices(scan_result.video_size, ceiling)

        if not slices_cover_exactly(slices, scan_result.video_size):
            raise PartitionError("computed slices do not cover the file exactly")

        parts = [
            Part(
                job_id=job.id,
                idx=sl.idx,
                name=render_part_name(
                    movie_filename=scan_result.video.name,
                    index=sl.idx,
                    template=self.settings.naming.template,
                    part_index_width=self.settings.naming.part_index_width,
                    max_length=self.settings.naming.max_length,
                    title=job.title,
                    year=job.year,
                ),
                byte_offset=sl.offset,
                byte_size=sl.size,
            )
            for sl in slices
        ]

        self.store.replace_parts(job.id, parts)
        self.store.set_partition(job.id, scan_result.folder_size, slices[0].size, len(slices))

        # Re-read from the database: on a resumed job some parts already carry a
        # file_id, and the locally built list has no idea. The database is the
        # authority on what has actually been uploaded.
        parts = self.store.get_parts(job.id)

        LOG.info(
            "planned upload",
            extra={
                "job": job.id,
                "video": scan_result.video.name,
                "video_size": scan_result.video_size,
                "parts": len(slices),
                "part_size": slices[0].size,
                "first_part": parts[0].name,
                "last_part": parts[-1].name,
            },
        )
        return slices, parts

    async def _upload_parts(
        self,
        job: Job,
        scan_result,
        slices: list[Slice],
        parts: list[Part],
        worker_id: str,
    ):
        """Upload pending parts, persisting each receipt as it lands."""
        uploader = Uploader(
            self.client,
            self.settings.telegram,
            sleep=self.sleep,
            should_cancel=self.should_cancel,
        )

        pending = [p for p in parts if not p.uploaded]
        if not pending:
            LOG.info("all parts already uploaded", extra={"job": job.id})
            return 0

        uploaded_bytes = 0
        total = len(pending)
        done = 0

        for part in pending:
            sl = next(s for s in slices if s.idx == part.idx)
            self.store.mark_part_uploading(job.id, part.idx)
            self.store.heartbeat(job.id, worker_id)

            try:
                receipt = await uploader.upload_part(
                    source=str(scan_result.video),
                    sl=sl,
                    name=part.name,
                    part_count=len(slices),
                )
            except PartUploadError as exc:
                self.store.mark_part_failed(job.id, part.idx, str(exc))
                raise

            # Persisted before we continue: a crash now costs at most a duplicate
            # message, never a lost reference to a file already in Telegram.
            self.store.record_upload(
                job.id,
                part.idx,
                chat_id=receipt.chat_id,
                thread_id=self.settings.telegram.thread_id or None,
                message_id=receipt.message_id,
                file_id=receipt.file_id,
                file_size=receipt.file_size,
                verified=receipt.verified,
            )

            uploaded_bytes += receipt.file_size
            done += 1
            self.store.log_event(
                job.id,
                "part.progress",
                f"{done}/{total} parts, {uploaded_bytes} bytes",
            )

        await self._upload_subtitles(job, scan_result)
        return uploaded_bytes

    async def _upload_subtitles(self, job: Job, scan_result) -> None:
        """Upload sidecar subs as separate documents.

        They are not partitioned: a subtitle file is tiny, and folding it into
        the video parts would break rejoining.
        """
        if not self.settings.naming.upload_subtitles or not scan_result.subtitles:
            return

        uploader = Uploader(
            self.client,
            self.settings.telegram,
            sleep=self.sleep,
            should_cancel=self.should_cancel,
        )

        for sub in scan_result.subtitles:
            try:
                size = sub.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue

            sl = Slice(idx=0, offset=0, size=size)
            name = subtitle_name(scan_result.video.name, sub.name, self.settings.naming.max_length)

            try:
                await uploader.upload_part(source=str(sub), sl=sl, name=name, part_count=1)
            except PartUploadError as exc:
                # Subs are not worth failing an otherwise-complete movie over.
                LOG.warning(
                    "subtitle upload failed, continuing",
                    extra={"job": job.id, "subtitle": sub.name, "error": str(exc)[:300]},
                )
                continue

            LOG.info("subtitle uploaded", extra={"job": job.id, "subtitle": name})

    def _assert_all_verified(self, job_id: int) -> None:
        """Last gate before anything destructive.

        Re-reads from the database rather than trusting in-memory state, so a
        partially recorded upload cannot slip through.
        """
        if not self.store.all_parts_verified(job_id):
            job = self.store.get_job(job_id)
            if job is None:
                raise VerificationError(f"job {job_id} vanished")

            # Report on verification, not on upload: a part can carry a file_id and still
            # fail the gate, and "parts not verified: []" told an operator nothing.
            missing = [p.idx for p in job.parts if not p.uploaded]
            unverified = [
                p.idx
                for p in job.parts
                if p.uploaded and p.state != PartState.VERIFIED
            ]
            wrong_size = [
                p.idx
                for p in job.parts
                if p.file_size is not None and p.file_size != p.byte_size
            ]
            detail = []
            if missing:
                detail.append(f"not uploaded: {missing}")
            if unverified:
                detail.append(f"uploaded but unverified: {unverified}")
            if wrong_size:
                detail.append(f"size disagrees with plan: {wrong_size}")
            raise VerificationError(
                "parts not verified" + (f" ({'; '.join(detail)})" if detail else "")
            )

        job = self.store.get_job(job_id)
        if job is None:
            raise VerificationError(f"job {job_id} vanished")

        recorded = sum(p.file_size or 0 for p in job.parts)
        planned = sum(p.byte_size for p in job.parts)
        if recorded != planned:
            raise VerificationError(f"size accounting mismatch: recorded {recorded}, planned {planned}")

        LOG.info("all parts verified", extra={"job": job_id, "parts": len(job.parts), "bytes": recorded})

    async def _finalize(self, job: Job, scan_result) -> str:
        """Radarr bookkeeping. Never allowed to fail the job."""
        notes: list[str] = []

        if not (self.settings.radarr.unmonitor_after_upload or self.settings.radarr.exclude_after_upload):
            return "radarr finalization disabled"

        if not self.radarr.configured:
            notes.append("radarr not configured")
            return "; ".join(notes)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.settings.radarr.timeout_seconds) as http:
                result = await self.radarr.finalize(
                    http, job.movie_id, job.tmdb_id, job.title, job.id
                )
            notes.append(f"unmonitored={result.unmonitored} excluded={result.excluded}")
            if result.error:
                notes.append(f"radarr: {result.error}")
        except ImportError:  # pragma: no cover
            notes.append("httpx unavailable")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"radarr error: {exc}"[:200])

        return "; ".join(notes)

    # -------------------------------------------------------------- deletion

    def evaluate_deletion(self, job: Job, folder: Path, expected_size: int) -> DeleteDecision:
        """Decide whether the local folder may be destroyed.

        Every condition is re-proven here. This runs after upload, on freshly
        read state, and refuses by default.
        """
        cfg = self.settings.deletion

        if not cfg.enabled:
            return DeleteDecision(False, "deletion disabled")

        if not self.store.all_parts_verified(job.id):
            return DeleteDecision(False, "not all parts verified")

        try:
            # require_subdir: media_root itself must never be an rmtree target,
            # however the job's folder_path came to name it.
            resolve_under(self.settings.paths.media_root, folder, require_subdir=True)
        except UnsafePathError as exc:
            return DeleteDecision(False, f"path no longer safe: {exc}")

        if not folder.is_dir():
            return DeleteDecision(False, f"folder missing: {folder}")

        if cfg.require_stability_before_delete and not is_stable(folder, 0):
            return DeleteDecision(False, "folder still changing")

        if cfg.verify_size_before_delete:
            current = tree_size(folder)
            if current != expected_size:
                return DeleteDecision(
                    False,
                    "size changed during upload",
                    f"recorded {expected_size}, found {current}",
                )

        # Re-enumerate so we delete what we actually saw and not a path that was
        # swapped underneath us.
        current_files = sum(1 for _ in folder.rglob("*") if _.is_file())
        LOG.info(
            "deletion gate passed",
            extra={"job": job.id, "folder": folder.name, "files": current_files, "size": expected_size},
        )
        return DeleteDecision(True, "all preconditions satisfied")

    async def _cleanup(
        self,
        job: Job,
        folder: Path,
        scan_result,
        uploaded_bytes: int,
        detail: str,
    ) -> JobOutcome:
        """Delete or quarantine, then close the job."""
        parts_done = len([p for p in self.store.get_parts(job.id) if p.uploaded])

        decision = self.evaluate_deletion(job, folder, scan_result.folder_size)

        if not decision.allowed:
            # Anything short of certainty is preserved.
            LOG.warning(
                "local data preserved",
                extra={"job": job.id, "folder": folder.name, "reason": decision.reason, "detail": decision.detail},
            )
            self.store.set_terminal(job.id, JobState.DONE, f"uploaded; kept local: {decision.reason}")
            return JobOutcome(
                job.id,
                str(JobState.DONE),
                parts_uploaded=parts_done,
                bytes_uploaded=uploaded_bytes,
                deleted=False,
                detail=f"{detail} | kept local: {decision.reason}",
            )

        try:
            shutil.rmtree(folder)
        except OSError as exc:
            LOG.error("delete failed, quarantining", extra={"job": job.id, "error": str(exc)})
            moved = move_to_quarantine(folder, self.settings.deletion.quarantine_dir)
            self.store.set_terminal(job.id, JobState.QUARANTINED, f"delete failed: {exc}")
            return JobOutcome(
                job.id,
                str(JobState.QUARANTINED),
                parts_uploaded=parts_done,
                bytes_uploaded=uploaded_bytes,
                quarantined=True,
                detail=f"moved to {moved}",
            )

        if folder.exists():  # pragma: no cover - defensive
            LOG.error("folder still present after rmtree", extra={"job": job.id, "folder": str(folder)})
            self.store.set_terminal(job.id, JobState.DONE, "delete did not remove the folder")
            return JobOutcome(job.id, str(JobState.DONE), parts_uploaded=parts_done)

        LOG.info(
            "deleted local data after verified upload",
            extra={"job": job.id, "folder": folder.name, "parts": parts_done, "bytes": uploaded_bytes},
        )
        self.store.complete_job(job.id)
        return JobOutcome(
            job.id,
            str(JobState.DONE),
            parts_uploaded=parts_done,
            bytes_uploaded=uploaded_bytes,
            deleted=True,
            detail=detail,
        )

    def _premium(self) -> bool | None:
        """Cached Telegram Premium state, if a probe has recorded one."""
        raw = self.store.get_meta("telegram_tier")
        if not raw or "|" not in raw:
            return None
        flag = raw.split("|", 1)[1]
        if flag == "premium":
            return True
        if flag == "standard":
            return False
        return None