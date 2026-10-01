"""Tests for the retry policy in the uploader.

Behaviour borrowed from mirror-leech is worth pinning down: FloodWait is waited
out rather than counted as a failure, and retries back off with jitter so several
parts failing at once do not resynchronise into the same failure.
"""

from __future__ import annotations

import asyncio

from arr_uploader.config import TelegramConfig
from arr_uploader.media.partition import Slice
from arr_uploader.telegram.uploader import (
    FLOOD_MARGIN,
    PartUploadError,
    Uploader,
    backoff_delay,
)

import pytest


class FakeDocument:
    def __init__(self, file_id, file_size):
        self.file_id = file_id
        self.file_size = file_size
        self.attributes = []


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self, message_id, chat_id, document):
        self.id = message_id
        self.chat = FakeChat(chat_id)
        self.document = document
        self.video = None
        self.audio = None
        self.photo = None


class FakeFloodWait(Exception):
    """Stand-in for pyrogram's FloodWait, which carries .value."""

    def __init__(self, value: int) -> None:
        super().__init__(f"flood wait {value}")
        self.value = value


class ScriptedClient:
    """Replays a list of behaviours, then succeeds."""

    def __init__(self, behaviours, chat_id=-1001234567890):
        self.behaviours = list(behaviours)
        self.chat_id = chat_id
        self.sent = 0
        self._next_id = 500

    def stop_transmission(self):
        pass

    async def send_document(self, chat_id, document=None, caption=None, **_kw):
        behaviour = self.behaviours.pop(0) if self.behaviours else "ok"
        if isinstance(behaviour, Exception):
            raise behaviour
        if behaviour == "bad_size":
            size = 1
        else:
            size = len(document.read())

        self.sent += 1
        self._next_id += 1
        return FakeMessage(self._next_id, chat_id, FakeDocument(f"F{self._next_id}", size))


def make_uploader(client, sleep=None):
    waits = []

    async def recorder(seconds):
        waits.append(seconds)

    telegram = TelegramConfig(api_id=1, api_hash="h", chat_id=-1001234567890, session_file="/tmp/s")
    return Uploader(client, telegram, sleep=sleep or recorder), waits


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "Movie.mkv"
    path.write_bytes(b"x" * 4096)
    return str(path)


def run(coro):
    return asyncio.run(coro)


def test_successful_upload_returns_receipt(source):
    client = ScriptedClient([])
    uploader, _ = make_uploader(client)

    receipt = run(uploader.upload_part(
        source=source,
        sl=Slice(idx=1, offset=0, size=4096),
        name="Movie.mkv.001",
        part_count=1,
    ))

    assert receipt.verified
    assert receipt.file_size == 4096


def test_flood_wait_is_slept_not_failed(source):
    """Telegram telling us to wait is not an error; it is a schedule."""
    client = ScriptedClient([FakeFloodWait(30)])
    uploader, waits = make_uploader(client)

    receipt = run(uploader.upload_part(
        source=source,
        sl=Slice(idx=1, offset=0, size=4096),
        name="Movie.mkv.001",
        part_count=1,
    ))

    assert receipt.verified
    assert client.sent == 1, "the flood case must not consume an upload attempt"
    assert waits == [int(30 * FLOOD_MARGIN) + 1]


def test_repeated_flood_waits_keep_retrying(source):
    client = ScriptedClient([FakeFloodWait(5), FakeFloodWait(5), FakeFloodWait(5)])
    uploader, waits = make_uploader(client)

    receipt = run(uploader.upload_part(
        source=source,
        sl=Slice(idx=1, offset=0, size=4096),
        name="Movie.mkv.001",
        part_count=1,
    ))

    assert receipt.verified
    assert len(waits) == 3


def test_transient_error_is_retried_then_succeeds(source):
    client = ScriptedClient([RuntimeError("network blip")])
    uploader, _ = make_uploader(client)

    receipt = run(uploader.upload_part(
        source=source,
        sl=Slice(idx=1, offset=0, size=4096),
        name="Movie.mkv.001",
        part_count=1,
    ))

    assert receipt.verified
    assert client.sent == 1


def test_persistent_failure_raises(source):
    client = ScriptedClient([RuntimeError("nope")] * 10)
    uploader, _ = make_uploader(client)

    with pytest.raises(PartUploadError) as exc:
        run(uploader.upload_part(
            source=source,
            sl=Slice(idx=1, offset=0, size=4096),
            name="Movie.mkv.001",
            part_count=1,
        ))

    assert "failed after" in str(exc.value)


def test_size_mismatch_retries_then_raises(source):
    """A part that keeps verifying short must never be reported as uploaded."""
    client = ScriptedClient(["bad_size"] * 10)
    uploader, _ = make_uploader(client)

    with pytest.raises(PartUploadError):
        run(uploader.upload_part(
            source=source,
            sl=Slice(idx=1, offset=0, size=4096),
            name="Movie.mkv.001",
            part_count=1,
        ))


def test_backoff_grows_and_is_capped():
    early = backoff_delay(1, base=10, cap=1000)
    later = backoff_delay(5, base=10, cap=1000)
    capped = backoff_delay(50, base=10, cap=1000)

    assert early <= 10, "attempt 1 should not exceed the base delay"
    assert capped <= 1000, "delay must respect the cap"
    assert later >= early


def test_backoff_has_jitter():
    """Without jitter, parts failing together retry in lockstep."""
    samples = {backoff_delay(4, base=10, cap=1000) for _ in range(20)}
    assert len(samples) > 1, "backoff must be jittered"


def test_cancellation_stops_before_sending(source):
    client = ScriptedClient([])
    uploader, _ = make_uploader(client, sleep=None)
    uploader._should_cancel = lambda: True

    from arr_uploader.telegram.uploader import UploadCancelled

    with pytest.raises(UploadCancelled):
        run(uploader.upload_part(
            source=source,
            sl=Slice(idx=1, offset=0, size=4096),
            name="Movie.mkv.001",
            part_count=1,
        ))

    assert client.sent == 0, "nothing may be sent after cancellation"


def test_only_the_requested_range_is_sent(tmp_path):
    """A part must carry its own bytes and nothing from its neighbours."""
    source = tmp_path / "Big.mkv"
    source.write_bytes(b"A" * 2048 + b"B" * 2048 + b"C" * 2048)

    client = ScriptedClient([])
    uploader, _ = make_uploader(client)

    captured = {}

    async def capture(chat_id, document=None, caption=None, **_kw):
        payload = document.read()
        captured["bytes"] = payload
        return FakeMessage(1, chat_id, FakeDocument("F1", len(payload)))

    client.send_document = capture

    run(uploader.upload_part(
        source=str(source),
        sl=Slice(idx=2, offset=2048, size=2048),
        name="Big.mkv.002",
        part_count=3,
    ))

    assert captured["bytes"] == b"B" * 2048