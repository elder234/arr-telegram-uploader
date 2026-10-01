"""Radarr inbox watcher.

Radarr's Custom Script runs in the Radarr container and should not depend on the
uploader being up. It writes a small JSON file into a shared inbox using the
write-temp-then-rename pattern, which is atomic on POSIX and on Windows' rename
semantics, so the watcher never sees a half-written payload.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class IntakeRequest:
    """A normalised request to process one movie folder."""

    folder_path: str
    title: str = ""
    year: int | None = None
    movie_id: int | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    priority: int = 100
    source: str = "inbox"

    def as_job_fields(self) -> dict[str, Any]:
        return {
            "movie_id": self.movie_id,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "year": self.year,
            "priority": self.priority,
            "source": self.source,
        }


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_job_payload(payload: dict[str, Any]) -> IntakeRequest:
    """Build an :class:`IntakeRequest` from webhook or inbox JSON.

    Accepts both the modern Radarr field names and its environment-variable
    spellings, because the Custom Script path supplies the latter.
    """
    folder = (
        payload.get("folderPath")
        or payload.get("folder_path")
        or payload.get("movieFolder")
        or payload.get("RADARR_MOVIE_PATH")
        or payload.get("path")
    )
    if not folder:
        raise ValueError("payload has no folder path")

    year = payload.get("year") or payload.get("RADARR_MOVIE_YEAR")
    title = payload.get("title") or payload.get("RADARR_MOVIE_TITLE") or ""

    raw_id = payload.get("movieId") or payload.get("RADARR_MOVIE_ID")

    # Radarr uses the IMDb id when the movie is keyed by IMDb, so a "tt..."
    # value in the numeric id field has to be routed to the right column.
    str_id = str(raw_id or "")
    imdb_id = payload.get("imdbId") or payload.get("RADARR_MOVIE_IMDBID") or None
    tmdb_id = payload.get("tmdbId") or payload.get("RADARR_MOVIE_TMDBID") or None
    movie_id = _coerce_int(raw_id)

    if str_id.startswith("tt") and not imdb_id:
        imdb_id = str_id
        movie_id = None
    elif str_id.isdigit() and not tmdb_id:
        tmdb_id = int(str_id)

    return IntakeRequest(
        folder_path=str(folder),
        title=str(title),
        year=_coerce_int(year),
        movie_id=movie_id,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        priority=_coerce_int(payload.get("priority")) or 100,
        source=str(payload.get("source") or "inbox"),
    )


def read_job_file(path: str | os.PathLike[str]) -> IntakeRequest:
    with Path(path).open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return parse_job_payload(payload)


class InboxWatcher:
    """Consumes ``*.json`` files from a directory.

    Files are renamed to ``.processing`` before being parsed, which claims them
    for this worker. A file left in ``.processing`` by a crash is reclaimed after
    a grace period so a crash mid-parse cannot strand a movie.
    """

    def __init__(
        self,
        inbox_dir: str | os.PathLike[str],
        store: object,
        *,
        reconcile_after_seconds: float = 300.0,
    ) -> None:
        self.dir = Path(inbox_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.reconcile_after_seconds = reconcile_after_seconds

    def poll(self) -> int:
        """Process every pending file once. Returns how many were enqueued."""
        self._reclaim_stale()
        accepted = 0

        for path in sorted(self.dir.glob("*.json")):
            claimed = path.with_suffix(path.suffix + ".processing")
            try:
                path.rename(claimed)
            except OSError as exc:  # pragma: no cover - racing worker
                LOG.debug("could not claim inbox file", extra={"path": str(path), "error": str(exc)})
                continue

            try:
                request = read_job_file(claimed)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                LOG.error(
                    "discarding malformed inbox file",
                    extra={"path": str(claimed), "error": str(exc)},
                )
                self._discard(claimed)
                continue

            try:
                job_id = self.store.upsert_job(request.folder_path, **request.as_job_fields())  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - intake must not kill the loop
                LOG.error(
                    "failed to enqueue from inbox",
                    extra={"path": str(claimed), "error": str(exc)},
                )
                self._discard(claimed)
                continue

            LOG.info(
                "enqueued from inbox",
                extra={"job": job_id, "folder": request.folder_path, "source": request.source},
            )
            self._discard(claimed)
            accepted += 1

        return accepted

    def _reclaim_stale(self) -> None:
        """Return abandoned .processing files to the pending set."""
        import time

        cutoff = time.time() - self.reconcile_after_seconds
        for path in self.dir.glob("*.json.processing"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.rename(path.with_suffix(""))
                    LOG.warning("reclaimed stale inbox file", extra={"path": str(path)})
            except OSError:  # pragma: no cover
                continue

    def _discard(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            LOG.warning("could not remove processed inbox file", extra={"path": str(path)})

    def write_inbox_file(self, payload: dict[str, Any]) -> Path:
        """Helper used by tests and by scripts running inside the container."""
        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"{os.getpid()}-{abs(hash(json.dumps(payload, sort_keys=True)))}.json"
        final = self.dir / name
        tmp = self.dir / f".{name}.tmp"

        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, final)  # atomic within the same directory
        return final