"""Filesystem reconciler.

Safety net for movies whose intake was lost -- a Custom Script that failed to
write, a webhook that was down, a container that restarted mid-import.

Deliberately conservative:

* only considers folders unknown to the database;
* only considers folders whose mtime is older than ``min_age_seconds``, so an
  in-flight import is never picked up;
* never re-queues a folder that already reached a terminal state;
* respects the same stability gate the main path uses.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..media.scan import ScanError, parse_movie_identity, scan

LOG = logging.getLogger(__name__)

# Directories Radarr and friends create inside a movie folder. Compared
# case-insensitively: Radarr writes "Extras", not "extras".
_SKIP_NAMES = frozenset({".hidden", "subs", "extras", "featurettes", "trailers", "sample"})


class Reconciler:
    """Periodically sweeps the media root for unqueued movie folders."""

    def __init__(
        self,
        media_root: str | os.PathLike[str],
        store: object,
        *,
        min_age_seconds: float = 3600,
        stability_seconds: float = 30,
        max_folders_per_pass: int = 25,
    ) -> None:
        self.media_root = Path(media_root)
        self.store = store
        self.min_age_seconds = min_age_seconds
        self.stability_seconds = stability_seconds
        self.max_folders_per_pass = max_folders_per_pass
        self.last_run: float = 0.0

    def due(self, now: float, interval_seconds: float) -> bool:
        """Whether another pass is due.

        ``now`` must be a *monotonic* clock (``loop.time()``): it is only ever
        differenced against our own ``last_run``, which is also monotonic.
        """
        return (now - self.last_run) >= interval_seconds

    def sweep(self, now: float | None = None) -> int:
        """Enqueue any movie folder the database has never seen.

        ``now`` must be *wall-clock* (``time.time()``): it is differenced against
        filesystem ``st_mtime``, which is epoch-based. Passing a monotonic clock
        here silently marks every folder as freshly written and the reconciler
        never enqueues anything, so the two clocks are deliberately separate.
        """
        now = time.time() if now is None else now
        self.last_run = time.monotonic()

        if not self.media_root.is_dir():
            LOG.warning("reconciler media root missing", extra={"path": str(self.media_root)})
            return 0

        known = self.store.known_folders()  # type: ignore[attr-defined]
        created: list[Path] = []

        for path in sorted(self.media_root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if path.name.lower() in _SKIP_NAMES:
                continue
            if str(path) in known:
                continue

            try:
                if (now - path.stat().st_mtime) < self.min_age_seconds:
                    LOG.debug("folder too fresh to reconcile", extra={"folder": path.name})
                    continue
            except OSError:
                continue

            created.append(path)

        if not created:
            LOG.debug("reconciler found nothing", extra={"media_root": str(self.media_root)})
            return 0

        # Bound each pass so a large library does not produce one enormous burst.
        batch = created[: self.max_folders_per_pass]
        if len(created) > len(batch):
            LOG.info("reconciler truncating pass", extra={"found": len(created), "processing": len(batch)})

        enqueued = 0
        for folder in batch:
            # Confirm it actually holds a movie before claiming it.
            try:
                scan(folder, upload_subtitles=False)
            except ScanError as exc:
                LOG.debug("skipping non-movie folder", extra={"folder": folder.name, "reason": str(exc)})
                continue

            title, year = parse_movie_identity(folder.name)
            try:
                job_id = self.store.upsert_job(  # type: ignore[attr-defined]
                    str(folder),
                    title=title,
                    year=year,
                    source="reconcile",
                    priority=200,  # behind anything Radarr actively asked for
                )
            except Exception as exc:  # noqa: BLE001
                LOG.error("reconciler enqueue failed", extra={"folder": folder.name, "error": str(exc)})
                continue

            LOG.info(
                "reconciler enqueued",
                extra={"job": job_id, "folder": folder.name, "title": title, "year": year},
            )
            enqueued += 1

        return enqueued

    async def run(self, interval_seconds: float, should_stop: object = None) -> None:
        """Loop until *should_stop()* returns True, sweeping each interval.

        Takes an injectable predicate rather than calling ``asyncio.sleep``
        through a helper so tests can drive it synchronously.
        """
        import asyncio

        while True:
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001 - never die on a sweep failure
                LOG.error("reconciler sweep failed", extra={"error": str(exc)})

            if should_stop is not None and should_stop():  # type: ignore[operator]
                return

            await asyncio.sleep(interval_seconds)