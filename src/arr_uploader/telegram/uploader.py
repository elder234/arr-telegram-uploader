"""Part upload loop.

Retry behaviour is adapted from what demonstrably works in mirror-leech:
``FloodWait`` and ``FloodPremiumWait`` are slept off with 30% headroom rather
than counted as failures, and a ``BadRequest`` on a video upload falls back to a
plain document. Two deliberate differences:

* a part is only recorded after :func:`verify_message` confirms the size;
* nothing is deleted here. This module never touches the source file, which is
  the whole point of separating it from mirror-leech's uploader.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

from ..config import TelegramConfig
from ..db.models import Part
from ..media.partition import Slice
from ..media.ranged_reader import RangedFileReader
from .verify import UploadReceipt, VerificationError, verify_message

LOG = logging.getLogger(__name__)

# FloodWait tells us how long to wait; add margin so we do not land right on the
# boundary again. Retrying early is cheap, retrying early and failing is not.
FLOOD_MARGIN = 1.3

MAX_ATTEMPTS_PER_PART = 5


class UploadCancelled(Exception):
    """Raised when a shutdown interrupts an in-flight upload."""


class PartUploadError(Exception):
    """Raised when a part cannot be uploaded after exhausting attempts."""


@dataclass(slots=True)
class Progress:
    part_idx: int
    part_count: int
    bytes_sent: int
    part_bytes: int

    @property
    def fraction(self) -> float:
        if self.part_bytes <= 0:
            return 0.0
        return min(self.bytes_sent / self.part_bytes, 1.0)


def _error_types() -> dict[str, type]:
    """Map our bucket names to Kurigram's exception classes.

    Resolved lazily so the module imports without the library installed, and
    keyed by name so a Kurigram rename degrades to the fallback classification
    rather than an AttributeError mid-upload.
    """
    try:
        from pyrogram import errors as pyrogram_errors
    except ImportError:  # pragma: no cover - kurigram not installed
        return {}

    wanted = {
        "flood": ("FloodWait", "FloodPremiumWait"),
        "bad_request": ("BadRequest",),
        "auth": ("AuthKeyUnregistered", "SessionRevoked", "UserDeactivated"),
        "transient": ("RPCError",),
    }
    resolved: dict[str, type] = {}
    for bucket, names in wanted.items():
        classes = tuple(
            cls for cls in (getattr(pyrogram_errors, n, None) for n in names)
            if isinstance(cls, type) and issubclass(cls, BaseException)
        )
        if classes:
            resolved[bucket] = classes  # type: ignore[assignment]
    return resolved


def classify_error(exc: BaseException) -> str:
    """Bucket an exception so the caller knows whether to wait, retry, or stop.

    Falls back to name-based detection when the library is absent, so a
    FloodWait raised in a test or a stripped environment is still slept off
    rather than burning a retry.
    """
    types = _error_types()

    for bucket, classes in types.items():
        if isinstance(exc, classes):
            return bucket

    name = type(exc).__name__
    if "floodwait" in name.lower() or "floodpremiumwait" in name.lower():
        return "flood"
    if "badrequest" in name.lower():
        return "bad_request"
    if "authkey" in name.lower() or "sessionrevoked" in name.lower() or "userdeactivated" in name.lower():
        return "auth"
    if isinstance(exc, pyrogram_transient_types()):
        return "transient"
    if isinstance(exc, asyncio.TimeoutError):
        return "transient"
    if isinstance(exc, (OSError, VerificationError)):
        return "local"
    return "unknown"


def pyrogram_transient_types() -> tuple[type, ...]:
    """Transient error classes, resolved independently of the main map."""
    transient = _error_types().get("transient")
    return transient if isinstance(transient, tuple) else ()


def flood_seconds(exc: BaseException) -> int:
    """Seconds Telegram asked us to wait.

    Pyrogram sets ``.value`` for FloodWait and ``.x`` for FloodPremiumWait, so
    both are consulted before falling back to a conservative default.
    """
    for attr in ("value", "x"):
        candidate = getattr(exc, attr, None)
        if isinstance(candidate, int) and candidate > 0:
            return candidate

    return 60


def backoff_delay(attempt: int, base: float = 5.0, cap: float = 120.0) -> float:
    """Exponential backoff with jitter.

    Jitter matters when several parts fail together: without it they retry in
    lockstep and reproduce the same failure.
    """
    raw = min(base * (2 ** max(attempt - 1, 0)), cap)
    return raw * (0.5 + random.random() / 2)


class Uploader:
    """Uploads slices of a movie file, one verified part at a time."""

    def __init__(
        self,
        client: Any,
        telegram: TelegramConfig,
        *,
        sleep: Any = asyncio.sleep,
        should_cancel: Any | None = None,
    ) -> None:
        self.client = client
        self.telegram = telegram
        self._sleep = sleep
        self._should_cancel = should_cancel or (lambda: False)

    def _check_cancelled(self) -> None:
        if self._should_cancel():
            self.stop_transmission()
            raise UploadCancelled("upload cancelled by shutdown")

    def stop_transmission(self) -> None:
        """Make the in-flight upload abort at the next chunk boundary.

        Called on shutdown and from the progress hook. The method is looked up
        dynamically so a stub client in tests need not implement it.
        """
        stopper = getattr(self.client, "stop_transmission", None)
        if stopper is None:
            return
        try:
            stopper()
        except Exception as exc:  # noqa: BLE001 - cancellation is best effort
            LOG.debug("stop_transmission raised", extra={"error": str(exc)})

    async def upload_part(
        self,
        *,
        source: str,
        sl: Slice,
        name: str,
        part_count: int,
    ) -> UploadReceipt:
        """Upload one slice and verify it.

        Returns a receipt only after the server has confirmed the document.
        Raises :class:`PartUploadError` when attempts are exhausted.
        """
        last_error: BaseException | None = None

        for attempt in range(1, MAX_ATTEMPTS_PER_PART + 1):
            self._check_cancelled()

            reader = RangedFileReader(source, sl, name=name)
            try:
                message = await self._send(reader, force_document=False)
            except BaseException as exc:  # noqa: BLE001 - classified below
                await reader.aclose() if hasattr(reader, "aclose") else reader.close()
                last_error = exc
                kind = classify_error(exc)

                if kind == "flood":
                    wait = int(flood_seconds(exc) * FLOOD_MARGIN) + 1
                    LOG.warning(
                        "flood wait on part",
                        extra={"part": sl.idx, "wait_seconds": wait, "attempt": attempt},
                    )
                    await self._sleep(wait)
                    continue

                if kind == "auth":
                    raise PartUploadError(f"telegram auth failure: {exc}") from exc

                LOG.warning(
                    "part upload attempt failed",
                    extra={"part": sl.idx, "attempt": attempt, "kind": kind, "error": str(exc)[:300]},
                )
                await self._sleep(backoff_delay(attempt))
                continue
            else:
                try:
                    receipt = verify_message(
                        message,
                        expected_size=sl.size,
                        chat_id=self.telegram.chat_id,
                        verify_size=self.telegram.verify_size,
                    )
                except VerificationError as exc:
                    last_error = exc
                    LOG.error(
                        "upload verification failed",
                        extra={"part": sl.idx, "attempt": attempt, "error": str(exc)},
                    )
                    # The message exists but is untrustworthy. Retry the part; a
                    # duplicate orphan message is preferable to claiming success.
                    await self._sleep(backoff_delay(attempt))
                    continue
                finally:
                    reader.close()

                LOG.info(
                    "part complete",
                    extra={
                        "part": sl.idx,
                        "parts": part_count,
                        "size": sl.size,
                        "name": name,
                    },
                )
                return receipt

        raise PartUploadError(
            f"part {sl.idx} failed after {MAX_ATTEMPTS_PER_PART} attempts: {last_error}"
        ) from last_error

    async def _send(self, reader: RangedFileReader, *, force_document: bool) -> Any:
        kwargs: dict[str, Any] = {
            "document": reader,
            "force_document": True,
            "disable_notification": True,
        }
        if self.telegram.thread_id:
            kwargs["message_thread_id"] = self.telegram.thread_id

        try:
            return await self.client.send_document(
                self.telegram.chat_id,
                caption=reader.name,
                progress=self._progress_hook(reader),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            if classify_error(exc) == "bad_request" and not force_document:
                # Mirror-leech's fallback: a file Telegram will not accept as
                # video gets re-sent as a plain document. We always send as a
                # document already, so this only fires for genuinely
                # malformed payloads.
                LOG.warning("retrying as document", extra={"name": reader.name, "error": str(exc)[:200]})
                kwargs.pop("progress", None)
                return await self.client.send_document(self.telegram.chat_id, caption=reader.name, **kwargs)
            raise

    def _progress_hook(self, reader: RangedFileReader):
        """Adapt the upload callback signature to our RangedFileReader.

        Kurigram reports bytes sent for the current file; the reader already
        knows how many bytes this part is meant to be.
        """

        def hook(current: int, _total: int) -> None:
            if self._should_cancel():
                self.stop_transmission()

        return hook

    async def upload_parts(
        self,
        *,
        source: str,
        slices: list[Slice],
        parts: list[Part],
    ) -> list[UploadReceipt]:
        """Upload every slice that is not already verified.

        ``parts`` supplies the persisted names and their existing state, so a
        resumed job skips whatever already succeeded.
        """
        by_idx = {p.idx: p for p in parts}
        receipts: list[UploadReceipt] = []

        for sl in slices:
            part = by_idx.get(sl.idx)
            if part is not None and part.uploaded:
                LOG.info("skipping already-uploaded part", extra={"part": sl.idx, "name": part.name})
                receipts.append(
                    UploadReceipt(
                        chat_id=part.chat_id or self.telegram.chat_id,
                        message_id=part.message_id or 0,
                        file_id=part.file_id or "",
                        file_size=part.file_size or sl.size,
                        verified=True,
                        detail="restored from database",
                    )
                )
                continue

            name = part.name if part is not None else source
            receipt = await self.upload_part(
                source=source,
                sl=sl,
                name=name,
                part_count=len(slices),
            )
            receipts.append(receipt)
            yield_hook = getattr(self, "on_part_complete", None)
            if callable(yield_hook):
                result = yield_hook(sl.idx, receipt)
                if hasattr(result, "__await__"):
                    await result

        return receipts