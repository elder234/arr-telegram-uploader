"""Small Radarr REST helpers.

Only two operations matter here: marking a movie unmonitored and adding an
import exclusion, so Radarr stops treating an already-uploaded movie as wanted.

These run on an :class:`httpx.AsyncClient`. An earlier version declared these
``async`` but issued blocking calls, which meant the coroutine object was
returned where a ``Response`` was expected and every call failed with an
``AttributeError`` that a broad ``except`` turned into a silent no-op. Keep the
awaits below intact: the calls are genuinely asynchronous.

Failures are logged and swallowed, because the upload itself succeeded regardless
-- but at ERROR level and with a store event, so a misconfigured Radarr is
visible in ``arr-uploader status`` rather than looking like a success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import RadarrConfig

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class RadarrResult:
    unmonitored: bool = False
    excluded: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class RadarrClient:
    def __init__(self, config: RadarrConfig, event_log: Any | None = None) -> None:
        self.config = config
        # Optional callable(job_id, event, detail) so callers can record the
        # failure in the job's event log.
        self._event_log = event_log

    @property
    def configured(self) -> bool:
        return bool(self.config.url and self.config.api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.config.api_key, "Accept": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.config.url.rstrip('/')}{path}"

    def _record(self, job_id: int | None, event: str, detail: str) -> None:
        if self._event_log is None or job_id is None:
            return
        try:
            self._event_log(job_id, event, detail)
        except Exception as exc:  # noqa: BLE001 - logging must never raise
            LOG.debug("radarr event logging failed", extra={"error": str(exc)})

    async def _get(self, client: Any, path: str) -> Any:
        response = await client.get(self._url(path), headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def unmonitor(self, client: Any, movie_id: int, job_id: int | None = None) -> bool:
        """Set ``monitored=false`` so Radarr stops auto-searching for this movie."""
        try:
            movie = await self._get(client, f"/api/v3/movie/{movie_id}")
            if not isinstance(movie, dict):  # pragma: no cover - defensive
                raise ValueError(f"unexpected movie payload: {type(movie).__name__}")

            if movie.get("monitored") is False:
                LOG.info("movie already unmonitored", extra={"movie_id": movie_id})
                return True

            movie["monitored"] = False
            response = await client.put(
                self._url(f"/api/v3/movie/{movie_id}"),
                headers=self._headers(),
                json=movie,
            )
            response.raise_for_status()
            LOG.info("movie unmonitored", extra={"movie_id": movie_id})
            return True
        except Exception as exc:  # noqa: BLE001 - never fail the job for this
            LOG.error(
                "could not unmonitor movie; radarr will keep searching for it",
                extra={"movie_id": movie_id, "error": str(exc)[:300]},
            )
            self._record(job_id, "radarr.unmonitor_failed", f"movie={movie_id} error={exc}"[:400])
            return False

    async def exclude_from_import(
        self,
        client: Any,
        tmdb_id: int,
        title: str,
        job_id: int | None = None,
    ) -> bool:
        """Add an import exclusion so Radarr does not re-import the movie.

        Radarr matches exclusions on ``tmdbId``, falling back to title, so we
        prefer the numeric id when the intake supplied one.
        """
        try:
            existing = await self._get(client, "/api/v3/exclusions") or []
            for entry in existing if isinstance(existing, list) else []:
                if str(entry.get("tmdbId")) == str(tmdb_id):
                    LOG.info("exclusion already present", extra={"tmdb_id": tmdb_id})
                    return True

            payload = {"tmdbId": int(tmdb_id), "title": title or str(tmdb_id), "monitored": False}
            response = await client.post(
                self._url("/api/v3/exclusions"),
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            LOG.info("import exclusion added", extra={"tmdb_id": tmdb_id, "title": title})
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.error(
                "could not add import exclusion",
                extra={"tmdb_id": tmdb_id, "error": str(exc)[:300]},
            )
            self._record(job_id, "radarr.exclude_failed", f"tmdb={tmdb_id} error={exc}"[:400])
            return False

    async def finalize(
        self,
        client: Any,
        movie_id: int | None,
        tmdb_id: int | None,
        title: str,
        job_id: int | None = None,
    ) -> RadarrResult:
        """Apply the configured post-upload bookkeeping."""
        result = RadarrResult()

        if not self.configured:
            result.error = "radarr is not configured"
            LOG.info("skipping radarr finalization", extra={"reason": result.error})
            return result

        if self.config.unmonitor_after_upload and movie_id:
            result.unmonitored = await self.unmonitor(client, movie_id, job_id)

        if self.config.exclude_after_upload and tmdb_id:
            result.excluded = await self.exclude_from_import(client, tmdb_id, title, job_id)

        if not result.unmonitored and not result.excluded:
            # ``ok`` is derived from ``error``, so setting the error is enough.
            result.error = "no radarr action was applicable"
            self._record(job_id, "radarr.finalization_failed", result.error)
            LOG.error("radarr finalization did nothing", extra={"error": result.error})
        else:
            # One leg succeeding is a partial success, worth recording explicitly
            # so an operator can see which half needs attention.
            if self.config.unmonitor_after_upload and movie_id and not result.unmonitored:
                result.error = "unmonitor failed"
            elif self.config.exclude_after_upload and tmdb_id and not result.excluded:
                result.error = "import exclusion failed"
            self._record(
                job_id,
                "radarr.finalized",
                f"unmonitored={result.unmonitored} excluded={result.excluded}",
            )

        return result