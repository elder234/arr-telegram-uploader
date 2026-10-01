"""Kurigram client lifecycle.

Wraps session start/stop and exposes the narrow surface the uploader needs, so
the rest of the codebase never touches the Telegram library directly.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import TelegramConfig

LOG = logging.getLogger(__name__)


class TelegramUnavailable(RuntimeError):
    """Raised when the Telegram client cannot be constructed or connected."""


class TelegramClient:
    """Async MTProto client wrapper.

    A single client instance is shared for the process lifetime: sessions are
    stateful and reconnecting per upload is both slow and rate-limit bait.
    """

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._client: Any | None = None
        self._stopping = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def client(self) -> Any:
        if self._client is None:
            raise TelegramUnavailable("telegram client is not started")
        return self._client

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def start(self) -> None:
        async with self._lock:
            if self._client is not None:
                return

            try:
                from kurigram import Client
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise TelegramUnavailable(
                    "kurigram is not installed; run 'pip install -r requirements.txt'"
                ) from exc

            session_string = (self.config.session_string or "").strip()
            session_file = Path(self.config.session_file)

            LOG.info(
                "starting telegram client",
                extra={"session": "string" if session_string else "file"},
            )

            if session_string:
                # A session string carries the auth key, so no name or workdir is
                # needed. Passing the string as ``name`` would treat it as a
                # filename and try to persist it.
                client = Client(
                    session_string=session_string,
                    api_id=self.config.api_id,
                    api_hash=self.config.api_hash,
                )
            else:
                # File-backed session: the workdir must exist before Kurigram
                # writes into it, otherwise startup fails on a fresh install.
                session_file.parent.mkdir(parents=True, exist_ok=True)
                client = Client(
                    name=session_file.stem,
                    api_id=self.config.api_id,
                    api_hash=self.config.api_hash,
                    workdir=str(session_file.parent),
                )

            await client.start()
            me = await client.get_me()
            LOG.info(
                "telegram client ready",
                extra={"user": getattr(me, "username", None) or getattr(me, "first_name", None)},
            )
            self._client = client

    async def stop(self) -> None:
        """Ask in-flight uploads to stop transmitting, then disconnect."""
        self._stopping.set()
        client = self._client
        if client is None:
            return
        LOG.info("stopping telegram client")
        try:
            stopper = getattr(client, "stop_transmission", None)
            if stopper is not None:
                result = stopper()
                if hasattr(result, "__await__"):
                    await result
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            LOG.warning("stop_transmission failed", extra={"error": str(exc)})

        try:
            if getattr(client, "is_connected", False):
                await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("disconnect failed", extra={"error": str(exc)})
        finally:
            self._client = None

    async def get_me(self) -> Any:
        return await self.client.get_me()

    def stop_transmission(self) -> None:
        """Cooperative cancel: makes the current upload return early."""
        client = self._client
        if client is None:
            return
        try:
            client.stop_transmission()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("stop_transmission raised", extra={"error": str(exc)})

    async def download_to(self, target: str) -> str:
        """Placeholder kept out of the hot path; used only by deep verify."""
        raise TelegramUnavailable("downloads are performed via verify.deep_verify")