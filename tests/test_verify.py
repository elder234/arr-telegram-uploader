"""Tests for the upload confirmation gate.

A part is only recorded as done once Telegram reports the document we asked it
to store. These tests pin down exactly how strict that is, because this gate is
what licenses deleting the only local copy.
"""

from __future__ import annotations

from arr_uploader.telegram.verify import (
    UploadReceipt,
    VerificationError,
    is_processing,
    summarise,
    verify_message,
)

import pytest


class FakeAttribute:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class FakeFileState:
    def __init__(self, state: str) -> None:
        self.file_state = state


class FakeDocument:
    def __init__(self, file_id="FILEID", file_size=100, state=None) -> None:
        self.file_id = file_id
        self.file_size = file_size
        self.attributes = [FakeFileState(state)] if state else []


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, message_id=42, chat_id=-100123, document=None) -> None:
        self.id = message_id
        self.chat = FakeChat(chat_id)
        self.document = document
        self.video = None
        self.audio = None
        self.photo = None


def test_size_match_is_accepted():
    message = FakeMessage(document=FakeDocument(file_size=100))
    receipt = verify_message(message, expected_size=100, chat_id=-100123)

    assert receipt.verified
    assert receipt.file_size == 100
    assert receipt.message_id == 42
    assert receipt.file_id == "FILEID"


def test_size_mismatch_is_rejected():
    """The critical case: a short or truncated upload must never pass."""
    message = FakeMessage(document=FakeDocument(file_size=99))

    with pytest.raises(VerificationError) as exc:
        verify_message(message, expected_size=100, chat_id=-100123)

    assert "mismatch" in str(exc.value)


def test_oversized_upload_is_rejected():
    message = FakeMessage(document=FakeDocument(file_size=101))
    with pytest.raises(VerificationError):
        verify_message(message, expected_size=100, chat_id=-100123)


def test_missing_document_is_rejected():
    with pytest.raises(VerificationError) as exc:
        verify_message(FakeMessage(document=None), expected_size=100, chat_id=-100123)
    assert "no file_id" in str(exc.value)


def test_none_message_is_rejected():
    with pytest.raises(VerificationError):
        verify_message(None, expected_size=100, chat_id=-100123)


def test_message_without_id_is_rejected():
    message = FakeMessage(document=FakeDocument())
    message.id = None
    with pytest.raises(VerificationError):
        verify_message(message, expected_size=100, chat_id=-100123)


def test_wrong_chat_is_rejected():
    """A message landing in the wrong chat means our bookkeeping is wrong."""
    message = FakeMessage(document=FakeDocument(file_size=100), chat_id=-999)
    with pytest.raises(VerificationError) as exc:
        verify_message(message, expected_size=100, chat_id=-100123)
    assert "chat" in str(exc.value)


def test_missing_size_is_rejected():
    doc = FakeDocument()
    doc.file_size = None
    with pytest.raises(VerificationError) as exc:
        verify_message(FakeMessage(document=doc), expected_size=100, chat_id=-100123)
    assert "size" in str(exc.value)


def test_still_processing_is_rejected():
    """Telegram accepting bytes is not the same as having finished storing them."""
    message = FakeMessage(document=FakeDocument(file_size=100, state="uploading"))

    assert is_processing(message)
    with pytest.raises(VerificationError) as exc:
        verify_message(message, expected_size=100, chat_id=-100123)
    assert "processing" in str(exc.value)


def test_video_field_accepted_as_document():
    """Pyrogram reports videos as `.video`; it still counts as a document."""
    message = FakeMessage(document=None)
    message.video = FakeDocument(file_size=100)

    receipt = verify_message(message, expected_size=100, chat_id=-100123)
    assert receipt.verified


def test_size_check_can_be_disabled():
    message = FakeMessage(document=FakeDocument(file_size=None))

    receipt = verify_message(message, expected_size=100, chat_id=-100123, verify_size=False)
    assert receipt.verified
    assert receipt.file_size == 100
    assert "disabled" in receipt.detail


def test_summarise():
    receipts = [
        UploadReceipt(chat_id=1, message_id=1, file_id="a", file_size=100, verified=True),
        UploadReceipt(chat_id=1, message_id=2, file_id="b", file_size=50, verified=True),
    ]
    assert "2 part(s)" in summarise(receipts)
    assert "150 bytes" in summarise(receipts)
    assert summarise([]) == "no parts verified"