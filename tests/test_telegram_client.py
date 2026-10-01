"""Tests for the client wrapper's construction.

The Telegram library is not installed here, so these assert on the arguments we
hand it rather than on a live connection. Three mistakes are guarded against:

* Passing a session *string* as ``name`` makes the library treat a base64 blob as
  a filename and try to persist it. A string session needs ``session_string``.
* The workdir must exist before the library writes there, otherwise a fresh
  install fails at startup.
* The import name. The distribution is ``kurigram`` but the module is
  ``pyrogram``; the fake is registered under the real name for that reason.
"""

from __future__ import annotations

import sys
import types

from arr_uploader.config import TelegramConfig
from arr_uploader.telegram.client import TelegramClient


class _Recorder:
    """Stands in for pyrogram.Client, recording its constructor kwargs."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.disconnected = False

    async def start(self):
        self.started = True
        return self

    async def get_me(self):
        return types.SimpleNamespace(username="tester", first_name="Test")

    async def disconnect(self):
        self.disconnected = True

    def stop_transmission(self):
        pass

    @property
    def is_connected(self):
        return True


def install_fake_kurigram(monkeypatch=None):
    """Register a fake ``pyrogram`` module exposing our recorder as Client.

    Registered as ``pyrogram``, not ``kurigram``, and that detail is the whole
    point. The distribution on PyPI is named kurigram, but it is a Pyrogram fork
    that installs a pyrogram/ package, so the import name is pyrogram. Faking
    ``kurigram`` here let a test suite pass for months against an import that
    could never resolve in production -- the fake agreed with the bug.
    """
    created: list[_Recorder] = []

    def factory(**kwargs):
        rec = _Recorder(**kwargs)
        created.append(rec)
        return rec

    module = types.ModuleType("pyrogram")
    module.Client = factory

    previous = sys.modules.get("pyrogram")
    sys.modules["pyrogram"] = module

    def restore():
        if previous is None:
            sys.modules.pop("pyrogram", None)
        else:
            sys.modules["pyrogram"] = previous

    return created, restore


def test_fake_is_registered_under_the_real_import_name():
    """Guards the fake itself.

    If this helper ever goes back to faking ``kurigram``, the suite stops
    exercising the real import path and green tests mean nothing again.
    """
    created, restore = install_fake_kurigram()
    try:
        assert "pyrogram" in sys.modules, "the fake must be importable as pyrogram"
        import importlib

        assert importlib.import_module("pyrogram").Client is not None
    finally:
        restore()
        assert created == []


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_session_string_is_passed_as_session_string(tmp_path):
    created, restore = install_fake_kurigram()
    try:
        cfg = TelegramConfig(
            api_id=1,
            api_hash="h",
            chat_id=1,
            session_string="AQAAAA-secret-blob",
            session_file=str(tmp_path / "u.session"),
        )
        run(TelegramClient(cfg).start())

        assert len(created) == 1
        kwargs = created[0].kwargs
        assert kwargs["session_string"] == "AQAAAA-secret-blob"
        assert "name" not in kwargs, "a base64 blob must not become a filename"
        assert "workdir" not in kwargs
    finally:
        restore()


def test_file_session_creates_its_workdir(tmp_path):
    created, restore = install_fake_kurigram()
    try:
        session_file = tmp_path / "nested" / "state" / "uploader.session"
        cfg = TelegramConfig(
            api_id=1, api_hash="h", chat_id=1, session_string="", session_file=str(session_file)
        )
        run(TelegramClient(cfg).start())

        kwargs = created[0].kwargs
        assert kwargs["name"] == "uploader", "the .session suffix is not part of the name"
        assert kwargs["workdir"] == str(session_file.parent)
        assert session_file.parent.is_dir(), "workdir must exist before startup"
        assert "session_string" not in kwargs
    finally:
        restore()


def test_whitespace_only_session_string_is_treated_as_absent(tmp_path):
    """An env var set to whitespace must not become a filename."""
    created, restore = install_fake_kurigram()
    try:
        session_file = tmp_path / "u.session"
        cfg = TelegramConfig(
            api_id=1, api_hash="h", chat_id=1, session_string="   ", session_file=str(session_file)
        )
        run(TelegramClient(cfg).start())

        kwargs = created[0].kwargs
        assert kwargs["name"] == "u"
        assert "session_string" not in kwargs
    finally:
        restore()


def test_api_credentials_are_forwarded(tmp_path):
    created, restore = install_fake_kurigram()
    try:
        cfg = TelegramConfig(
            api_id=4242,
            api_hash="deadbeef",
            chat_id=1,
            session_string="blob",
            session_file=str(tmp_path / "u.session"),
        )
        run(TelegramClient(cfg).start())
        assert created[0].kwargs["api_id"] == 4242
        assert created[0].kwargs["api_hash"] == "deadbeef"
    finally:
        restore()


def test_client_property_raises_before_start(tmp_path):
    cfg = TelegramConfig(
        api_id=1, api_hash="h", chat_id=1, session_string="blob", session_file=str(tmp_path / "u.session")
    )
    client = TelegramClient(cfg)
    try:
        client.client
    except Exception as exc:
        assert "not started" in str(exc)
    else:
        raise AssertionError("accessing .client before start() must raise")


def test_start_is_idempotent(tmp_path):
    created, restore = install_fake_kurigram()
    try:
        cfg = TelegramConfig(
            api_id=1, api_hash="h", chat_id=1, session_string="blob", session_file=str(tmp_path / "u.session")
        )
        client = TelegramClient(cfg)

        async def go():
            await client.start()
            await client.start()

        run(go())
        assert len(created) == 1, "a second start() must not build a second client"
    finally:
        restore()


def test_stop_clears_the_client(tmp_path):
    created, restore = install_fake_kurigram()
    try:
        cfg = TelegramConfig(
            api_id=1, api_hash="h", chat_id=1, session_string="blob", session_file=str(tmp_path / "u.session")
        )
        client = TelegramClient(cfg)

        async def go():
            await client.start()
            await client.stop()

        run(go())
        assert client.stopping is True
        try:
            client.client
        except Exception:
            pass
        else:
            raise AssertionError(".client must be unavailable after stop()")
    finally:
        restore()