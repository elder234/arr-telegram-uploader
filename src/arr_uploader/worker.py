"""The worker loop.

One claim at a time by default. ``max_concurrent_jobs`` above 1 exists but is not
recommended: parallel uploads split the same uplink without raising throughput
and make FloodWait more likely, which costs more than the concurrency gains.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import socket
import sys
import time
import uuid
from typing import Any

from .config import Settings
from .db.models import JobState
from .db.store import Store
from .intake.inbox import InboxWatcher
from .intake.reconciler import Reconciler
from .intake.webhook import WebhookServer
from .pipeline import Pipeline, PipelineError
from .telegram.client import TelegramClient
from .telegram.limits import async_probe_account, store_tier

LOG = logging.getLogger(__name__)

LEASE_SECONDS = 4 * 3600


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    """Polls the database for jobs and drives them through the pipeline."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.worker_id = _worker_id()
        self._stop = asyncio.Event()
        self.telegram = TelegramClient(settings.telegram)
        self.inbox = InboxWatcher(settings.paths.inbox_dir, store)
        self.reconciler = Reconciler(
            settings.paths.media_root,
            store,
            min_age_seconds=settings.reconciler.min_age_seconds,
            stability_seconds=settings.uploader.stability_seconds,
        )
        self.webhook = (
            WebhookServer(
                store,
                settings.webhook.secret,
                settings.webhook.host,
                settings.webhook.port,
            )
            if settings.webhook.enabled
            else None
        )
        self.pipeline = Pipeline(
            settings,
            store,
            # Proxy the client so a shutdown request reaches Telegram.
            _ClientProxy(self.telegram),
            should_cancel=self.should_stop,
        )

        self.processed = 0
        self.failed = 0

    # ------------------------------------------------------------- lifecycle

    def request_stop(self) -> None:
        LOG.info("shutdown requested")
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> None:
        self._install_signal_handlers()
        LOG.info(
            "worker starting",
            extra={
                "worker_id": self.worker_id,
                "pid": os.getpid(),
                # Named because "kurigram is not installed" from a wrong
                # interpreter is otherwise indistinguishable from a genuinely
                # missing dependency, and sys.path says which one is running.
                "python": sys.executable,
                "version": platform.python_version(),
                "prefix": sys.prefix,
            },
        )

        try:
            await self.telegram.start()
        except Exception as exc:  # noqa: BLE001
            LOG.error("could not start telegram client", extra={"error": str(exc)})
            raise

        await self._probe_tier()

        if self.webhook is not None:
            try:
                await self.webhook.start()
            except Exception as exc:  # noqa: BLE001 - webhook is optional
                LOG.error("webhook failed to start", extra={"error": str(exc)})
                self.webhook = None

        background = [asyncio.create_task(self._background_loop(), name="background")]
        # Both halves of torbox config or neither; config.validate() enforces it,
        # so an empty api_key here means intake is deliberately off.
        if self.settings.torbox.api_key and self.settings.torbox.watch_dir:
            background.append(asyncio.create_task(self._torbox_loop(), name="torbox-intake"))
        else:
            LOG.info("torbox intake disabled", extra={"reason": "api_key and watch_dir not both set"})

        try:
            await self._claim_loop()
        finally:
            if self.settings.uploader.drain_on_shutdown:
                LOG.info("draining in-flight upload")
            for task in background:
                task.cancel()
            if self.webhook is not None:
                await self.webhook.stop()
            await self.telegram.stop()

        LOG.info("worker stopped", extra={"processed": self.processed, "failed": self.failed})

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                signal.signal(sig, lambda *_a: self.request_stop())

    async def _probe_tier(self) -> None:
        """Record the account's Premium status once at startup.

        Uses the async probe: the sync one cannot be called from inside a
        running loop, and its RuntimeError used to be swallowed here.
        """
        try:
            probe = await async_probe_account(_ClientProxy(self.telegram))
            # time.time(), not loop.time(): store_tier's TTL is wall-clock.
            store_tier(self.store, probe, time.time())
            LOG.info(
                "telegram tier resolved",
                extra={
                    "premium": probe.is_premium,
                    "source": probe.source,
                    "ceiling_mb": probe.ceiling_mb,
                },
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("tier probe skipped", extra={"error": str(exc)})

    # ------------------------------------------------------------------ loops

    async def _torbox_loop(self) -> None:
        """Run TorBox intake alongside the worker.

        TorBox intake was previously reachable only from the one-shot ``fetch``
        command, which meant the container never actually submitted or fetched
        anything on its own. The handoff is the inbox file, so the claim loop
        below picks the job up with no further plumbing.
        """
        import httpx

        from .intake.torbox_intake import TorboxIntake

        intake = TorboxIntake(
            self.settings.torbox,
            fetch_dir=self.settings.torbox.staging_dir or self.settings.paths.state_dir,
            inbox_dir=self.settings.paths.inbox_dir,
        )
        timeout = httpx.Timeout(self.settings.torbox.timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                await intake.run_forever(http, self.should_stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bad key must not kill uploads
            LOG.error("torbox intake stopped", extra={"error": str(exc)})

    async def _background_loop(self) -> None:
        """Intake, reconciliation, and job expiry upkeep."""
        heartbeat_every = max(
            1, int(self.settings.uploader.heartbeat_seconds)
        )
        last_heartbeat = 0.0

        while not self.should_stop():
            try:
                enqueued = self.inbox.poll()
                if enqueued:
                    LOG.info("intake processed", extra={"enqueued": enqueued})

                # due() is a monotonic-vs-monotonic comparison; sweep() needs
                # wall-clock because it compares against filesystem mtime. Passing
                # loop.time() to sweep() made every folder look brand new and the
                # reconciler silently enqueued nothing.
                if self.settings.reconciler.enabled and self.reconciler.due(
                    asyncio.get_running_loop().time(),
                    self.settings.reconciler.interval_seconds,
                ):
                    enqueued = self.reconciler.sweep()
                    LOG.info(
                        "reconciler pass complete",
                        extra={
                            "enqueued": enqueued,
                            "wall_clock": round(time.time(), 1),
                            "min_age_seconds": self.settings.reconciler.min_age_seconds,
                        },
                    )

                # Throttled: this is liveness evidence, not an event. Recording it
                # every poll drowned the events table and made `status` useless.
                now = time.time()
                if now - last_heartbeat >= heartbeat_every:
                    last_heartbeat = now
                    self.store.log_event(None, "heartbeat", self.worker_id)
            except Exception as exc:  # noqa: BLE001 - intake must not kill the worker
                LOG.error("background loop error", extra={"error": str(exc)})

            await self._interruptible_sleep(self.settings.uploader.poll_interval_seconds)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep, but wake early when shutdown is requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _claim_loop(self) -> None:
        """Claim and process jobs one at a time."""
        while not self.should_stop():
            job = self.store.claim_next_job(self.worker_id, LEASE_SECONDS)

            if job is None:
                await self._interruptible_sleep(self.settings.uploader.poll_interval_seconds)
                continue

            try:
                outcome = await self.pipeline.run_job(job, self.worker_id)
                self.processed += 1
                LOG.info(
                    "job finished",
                    extra={
                        "job": job.id,
                        "state": outcome.final_state,
                        "parts": outcome.parts_uploaded,
                        "bytes": outcome.bytes_uploaded,
                        "deleted": outcome.deleted,
                    },
                )
            except PipelineError as exc:
                self.failed += 1
                self._schedule_retry(job, exc)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
                self.failed += 1
                LOG.exception("unhandled job failure", extra={"job": job.id})
                self._schedule_retry(job, exc)

    def _schedule_retry(self, job: Any, exc: BaseException) -> None:
        """Back off exponentially, then give up rather than retry forever."""
        base = self.settings.uploader.backoff_base_seconds
        cap = self.settings.uploader.backoff_cap_seconds
        delay = min(base * (2 ** max(job.attempts, 0)), cap)

        state = self.store.fail_job(
            job.id,
            f"{type(exc).__name__}: {exc}",
            delay_seconds=delay,
            max_attempts=self.settings.uploader.max_attempts,
        )

        if state == JobState.FAILED:
            LOG.error("job permanently failed, local data retained", extra={"job": job.id})
            # Note: we deliberately do not clean up here. An unrecoverable job
            # keeps its folder so the operator can inspect or re-run it.
        else:
            LOG.info(
                "job scheduled for retry",
                extra={"job": job.id, "delay_seconds": delay, "state": str(state)},
            )


class _ClientProxy:
    """Adapts :class:`TelegramClient` to the surface the uploader expects.

    Keeps the library-specific lifecycle in one module while letting
    ``Uploader`` and ``verify`` work against a small, mockable interface.
    """

    def __init__(self, inner: TelegramClient) -> None:
        self._inner = inner

    @property
    def inner(self) -> TelegramClient:
        return self._inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner.client, item)

    def get_me(self) -> Any:
        return self._inner.get_me()

    def stop_transmission(self) -> None:
        self._inner.stop_transmission()


async def run_worker(settings: Settings) -> None:
    store = Store(settings.paths.state_dir + "/uploader.db")
    worker = Worker(settings, store)
    try:
        await worker.run()
    finally:
        store.close()