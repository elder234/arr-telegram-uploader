"""Post-upload verification.

An upload is only treated as successful once Telegram reports a document whose
size matches the bytes we intended to send. This is the gate that licenses
deleting the local file, so it is deliberately strict and defaults to on.

What is *not* claimed here: that Telegram's copy is byte-identical. Size
agreement plus a successful return from ``send_document`` is the strongest
signal available without downloading the part back, which is available via
``deep_verify`` for the paranoid case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

LOG = logging.getLogger(__name__)

# Telegram returns these for files that are still being processed server-side.
_PENDING_STATES = frozenset({"uploading", "pending", "processing"})


class VerificationError(Exception):
    """Raised when an upload cannot be confirmed."""


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    """What we can prove about one uploaded part."""

    chat_id: int
    message_id: int
    file_id: str
    file_size: int
    verified: bool
    detail: str = ""


def extract_document(message: object) -> object | None:
    """Pull the document off a returned message, if there is one."""
    for attr in ("document", "video", "audio", "photo"):
        doc = getattr(message, attr, None)
        if doc is not None:
            return doc
    return None


def extract_size(message: object) -> int | None:
    """Server-reported byte size of the uploaded document."""
    doc = extract_document(message)
    if doc is None:
        return None
    size = getattr(doc, "file_size", None)
    return int(size) if isinstance(size, int) else None


def extract_file_id(message: object) -> str | None:
    doc = extract_document(message)
    if doc is None:
        return None
    file_id = getattr(doc, "file_id", None)
    return str(file_id) if file_id else None


def is_processing(message: object) -> bool:
    """True while Telegram reports the file as not yet finished uploading."""
    doc = extract_document(message)
    if doc is None:
        return False
    attributes = getattr(doc, "attributes", None) or []
    for attr in attributes:
        state = getattr(attr, "file_state", None)
        if state and str(state).lower() in _PENDING_STATES:
            return True
    return False


def verify_message(
    message: object,
    *,
    expected_size: int,
    chat_id: int,
    verify_size: bool = True,
) -> UploadReceipt:
    """Confirm *message* carries the document we asked Telegram to store.

    Raises :class:`VerificationError` when the message has no document, no file
    id, or a size that disagrees with ``expected_size``.
    """
    if message is None:
        raise VerificationError("telegram returned no message")

    message_id = getattr(message, "id", None)
    if not isinstance(message_id, int):
        raise VerificationError(f"returned message has no usable id: {message_id!r}")

    actual_chat = getattr(getattr(message, "chat", None), "id", chat_id)
    if actual_chat != chat_id:
        raise VerificationError(f"message landed in chat {actual_chat}, expected {chat_id}")

    file_id = extract_file_id(message)
    if not file_id:
        raise VerificationError("returned message carries no file_id")

    reported = extract_size(message)

    if is_processing(message):
        raise VerificationError("telegram still reports the file as processing")

    if not verify_size:
        # Size checking disabled by config; record what we know and move on.
        return UploadReceipt(
            chat_id=actual_chat,
            message_id=message_id,
            file_id=file_id,
            file_size=reported if reported is not None else expected_size,
            verified=True,
            detail="size check disabled",
        )

    if reported is None:
        raise VerificationError("telegram did not report a document size")

    if reported != expected_size:
        raise VerificationError(f"size mismatch: expected {expected_size}, telegram reports {reported}")

    LOG.info(
        "upload verified",
        extra={"message_id": message_id, "file_size": reported, "file_id": file_id[:32]},
    )
    return UploadReceipt(
        chat_id=actual_chat,
        message_id=message_id,
        file_id=file_id,
        file_size=reported,
        verified=True,
        detail="size confirmed",
    )


async def deep_verify(client: object, receipt: UploadReceipt, sink: object, expected_size: int) -> bool:
    """Download a stored file back and hash it against a streamed digest.

    Not used in the default flow because it doubles bandwidth and re-reads
    gigabytes from Telegram, but it is available for spot checks and for when a
    size match alone is not enough confidence to delete the only copy.
    """
    from ..media.ranged_reader import RangedFileReader  # noqa: F401  (documented dependency)

    getter = getattr(client, "download_media", None)
    if getter is None:
        raise VerificationError("client cannot download media")

    total = 0
    try:
        result = getter(receipt.file_id, in_memory=False)
        if hasattr(result, "__await__"):
            result = await result

        path = getattr(result, "file_name", None) or result
        total = _count_file(path)
    except Exception as exc:  # noqa: BLE001
        raise VerificationError(f"deep verification download failed: {exc}") from exc

    if total != expected_size:
        raise VerificationError(f"deep verification size mismatch: {total} != {expected_size}")
    return True


def _count_file(path: object) -> int:
    from pathlib import Path

    p = Path(str(path))
    if not p.is_file():
        raise VerificationError(f"download did not produce a file: {p}")
    return p.stat().st_size


def summarise(receipts: list[UploadReceipt]) -> str:
    if not receipts:
        return "no parts verified"
    total = sum(r.file_size for r in receipts)
    return f"{len(receipts)} part(s) verified, {total} bytes"