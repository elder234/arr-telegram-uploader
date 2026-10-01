"""Optional HTTP intake endpoint.

Uses only ``httpx`` so the core worker has no web framework dependency; this runs
on a small asyncio server in a side task when enabled.

Idempotent by construction: the same webhook event posted twice resolves to one
job because enqueue is keyed on the movie folder.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

from .inbox import IntakeRequest, parse_job_payload

LOG = logging.getLogger(__name__)

HEADER_SECRET = "x-uploader-secret"
MAX_BODY_BYTES = 64 * 1024


class WebhookServer:
    """Accepts ``POST /radarr`` and ``POST /job`` JSON payloads."""

    def __init__(self, store: object, secret: str, host: str, port: int) -> None:
        self.store = store
        self.secret = secret
        self.host = host
        self.port = port
        self._server: Any | None = None

    def _authorized(self, headers: Any) -> bool:
        if not self.secret:
            return False
        provided = headers.get(HEADER_SECRET) or headers.get(HEADER_SECRET.title())
        if not provided:
            return False
        # Constant-time compare so the endpoint does not leak the secret by
        # response timing.
        return hmac.compare_digest(str(provided), str(self.secret))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return

            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                await self._respond(writer, 400, {"error": "malformed request"})
                return
            method, path = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

            length = int(headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                await self._respond(writer, 413, {"error": "payload too large"})
                return

            body = await reader.readexactly(length) if length else b"{}"

            if method not in ("POST", "PUT"):
                await self._respond(writer, 405, {"error": "method not allowed"})
                return
            if not self._authorized(headers):
                LOG.warning("rejected unauthorized webhook", extra={"path": path})
                await self._respond(writer, 401, {"error": "unauthorized"})
                return

            await self._dispatch(path, body, writer)
        except asyncio.TimeoutError:
            LOG.warning("webhook request timed out")
        except Exception as exc:  # noqa: BLE001
            LOG.error("webhook handler failed", extra={"error": str(exc)})
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001, pragma: no cover
                pass

    async def _dispatch(self, path: str, body: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            await self._respond(writer, 400, {"error": f"invalid json: {exc}"})
            return

        if not isinstance(payload, dict):
            await self._respond(writer, 400, {"error": "expected a JSON object"})
            return

        if path.rstrip("/") in ("/health", "/healthz"):
            await self._respond(writer, 200, {"status": "ok"})
            return

        try:
            request: IntakeRequest = parse_job_payload(payload)
        except ValueError as exc:
            await self._respond(writer, 400, {"error": str(exc)})
            return

        try:
            job_id = self.store.upsert_job(request.folder_path, **request.as_job_fields())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            LOG.error("webhook enqueue failed", extra={"error": str(exc)})
            await self._respond(writer, 500, {"error": "could not enqueue"})
            return

        LOG.info("enqueued from webhook", extra={"job": job_id, "folder": request.folder_path})
        await self._respond(writer, 200, {"job_id": job_id, "status": "queued"})

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 405: "Method Not Allowed",
                  413: "Payload Too Large", 500: "Internal Server Error"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self.handle, self.host, self.port)
        LOG.info("webhook listening", extra={"host": self.host, "port": self.port})

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("webhook close raised", extra={"error": str(exc)})
        LOG.info("webhook stopped")