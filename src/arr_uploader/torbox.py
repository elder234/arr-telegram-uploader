"""TorBox REST client.

Written against the published OpenAPI spec (``https://api.torbox.app/openapi.json``),
not against guesses. The facts that shaped this file:

* Base is ``https://api.torbox.app/v1/api``. Auth is ``Authorization: Bearer <key>``.
* Every response uses one envelope: ``{success, error, detail, data}``. ``success``
  is the only reliable success signal -- the HTTP status can be 200 on a failure.
  ``detail`` is safe to show a user.
* ``createtorrent`` is ``multipart/form-data``, not JSON. Sending JSON silently
  creates nothing.
* ``mylist`` state is cached server-side for 600 seconds unless ``bypass_cache``
  is passed. Polling it in a tight loop returns the same stale object.
* ``requestdl`` links stay valid for 3 hours *to start* the transfer. A long
  download can finish well after that, so a slow leg is not a link-expiry error.
* ``createtorrent`` is limited to 60/hour for uncached items. Submitting every
  magnet without checking ``checkcached`` first will hit that.

State is normalised into :class:`TorrentState` so callers never have to know that
TorBox exposes qBittorrent's state strings plus its own.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .config import TorboxConfig

LOG = logging.getLogger(__name__)

# TorBox caps a single download on the free plan at 10 GiB. Exceeding it returns
# DOWNLOAD_TOO_LARGE, and we cannot do anything about it, so refuse to submit.
FREE_PLAN_MAX_BYTES = 10737418240

_MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)
_INFO_HASH_RE = re.compile(r"urn:btih:([0-9a-zA-Z]+)", re.IGNORECASE)


class TorboxError(RuntimeError):
    """A TorBox call failed. ``detail`` is the user-facing explanation."""

    def __init__(self, message: str, *, error: str = "", detail: str = "") -> None:
        super().__init__(detail or message)
        self.error = error
        self.detail = detail


class TorrentState(enum.Enum):
    """Normalised lifecycle states.

    TorBox returns qBittorrent-style strings (``metaDL``, ``checkingDL``,
    ``stalledUP``, ``uploading``) interleaved with its own (``cached``,
    ``Failed (Processing)``). Anything unrecognised maps to UNKNOWN rather than
    being treated as an error, because a new upstream state should not make the
    pipeline delete anything.
    """

    DOWNLOADING = "downloading"
    METADATA = "metadata"
    CACHED = "cached"
    COMPLETE = "complete"
    UPLOADING = "uploading"
    STALLED = "stalled"
    ERROR = "error"
    UNKNOWN = "unknown"


_TERMINAL_ERRORS = {
    "failed",
    "failed (processing)",
    "expired",
    "(reported) missing",
    "reported missing",
    "incomplete",
    "missingfileserror",
}


def state_from(raw: str | None, *, finished: bool = False, present: bool = True) -> TorrentState:
    """Map a raw ``download_state`` onto :class:`TorrentState`.

    ``download_finished`` is trusted ahead of the state string: TorBox keeps
    reporting ``uploading`` for a torrent that is in fact complete, and the
    upload-to-Telegram leg should not wait for seeding to finish.
    """
    text = (raw or "").strip().lower()

    if text in _TERMINAL_ERRORS:
        return TorrentState.ERROR
    if finished:
        # finished but the bytes are gone: treated as an error, because fetching
        # will fail and we would otherwise poll forever.
        return TorrentState.COMPLETE if present else TorrentState.ERROR
    if text == "cached":
        return TorrentState.CACHED
    if text in ("metadl", "meta_dl"):
        return TorrentState.METADATA
    if text == "downloading":
        return TorrentState.DOWNLOADING
    if text in ("uploading", "stalledup"):
        return TorrentState.UPLOADING
    if text in ("stalled", "stalled (no seeds)", "stoppeddl", "queued", "checkingdl"):
        return TorrentState.STALLED
    return TorrentState.UNKNOWN


@dataclass(slots=True)
class TorboxFile:
    id: int
    name: str
    size: int
    md5: str = ""
    short_name: str = ""

    @property
    def is_video(self) -> bool:
        return self.name.lower().endswith(
            (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".webm")
        )

    @property
    def is_subtitle(self) -> bool:
        return self.name.lower().endswith((".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup"))


@dataclass(slots=True)
class Torrent:
    id: int
    name: str
    hash: str = ""
    state: TorrentState = TorrentState.UNKNOWN
    raw_state: str = ""
    progress: float = 0.0
    size: int = 0
    download_speed: int = 0
    eta: int = 0
    files: list[TorboxFile] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when the bytes are on the server and can be requested."""
        return self.state in (TorrentState.COMPLETE, TorrentState.CACHED)

    @property
    def video_files(self) -> list[TorboxFile]:
        return [f for f in self.files if f.is_video]


def hash_from_magnet(magnet: str) -> str:
    """Extract the info-hash from a magnet link.

    Base32 (32 chars) is normalised to hex because that is what ``checkcached``
    and the list responses use, and comparing the two forms directly silently
    reports everything as uncached.
    """
    match = _INFO_HASH_RE.search(magnet)
    if not match:
        raise TorboxError("magnet link has no urn:btih info-hash", detail="magnet link is malformed")
    digest = match.group(1)
    if len(digest) == 32:
        try:
            import base64

            return base64.b32decode(digest.upper()).hex()
        except Exception as exc:  # noqa: BLE001 - malformed base32
            raise TorboxError(f"magnet base32 hash is invalid: {exc}") from exc
    if len(digest) == 40:
        return digest.lower()
    raise TorboxError(f"unexpected info-hash length: {len(digest)}")


def is_magnet(value: str) -> bool:
    return bool(_MAGNET_RE.match(value.strip()))


class TorboxClient:
    """Async TorBox client.

    Takes an ``httpx.AsyncClient`` rather than constructing one, so the caller
    owns the connection pool and shutdown. Every method genuinely awaits; see the
    note in :mod:`.radarr` for why that distinction matters.
    """

    def __init__(self, config: TorboxConfig, event_log: Any | None = None) -> None:
        self.config = config
        self._event_log = event_log

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key)

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        # The key goes in a header only. It must never appear in a URL, because
        # URLs end up in access logs and in httpx exception messages.
        return {"Authorization": f"Bearer {self.config.api_key}", "Accept": "application/json"}

    def _record(self, job_id: int | None, event: str, detail: str) -> None:
        if self._event_log is None or job_id is None:
            return
        try:
            self._event_log(job_id, event, detail)
        except Exception as exc:  # noqa: BLE001 - logging must never raise
            LOG.debug("torbox event logging failed", extra={"error": str(exc)})

    async def _call(
        self,
        client: Any,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a call and unwrap the ``{success, error, detail, data}`` envelope."""
        try:
            response = await client.request(
                method,
                self._url(path),
                headers=self._headers(),
                params=params,
                data=data,
                files=files,
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - httpx raises many transport types
            LOG.error("torbox request failed", extra={"path": path, "error": str(exc)})
            raise TorboxError(f"{method} {path} failed: {exc}", detail="could not reach TorBox") from exc

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - non-JSON body
            raise TorboxError(
                f"{method} {path} returned a non-JSON body",
                detail=f"HTTP {response.status_code}",
            ) from exc

        if not isinstance(payload, dict):
            raise TorboxError(f"{method} {path} returned a non-object body")

        if not payload.get("success"):
            error = str(payload.get("error") or "UNKNOWN_ERROR")
            detail = str(payload.get("detail") or "TorBox rejected the request")
            LOG.warning(
                "torbox rejected request",
                extra={"path": path, "error": error, "detail": detail},
            )
            raise TorboxError(f"{method} {path}: {error}", error=error, detail=detail)

        return payload.get("data")

    # ------------------------------------------------------------------ submit

    async def cached_torrent_ids(self, client: Any, info_hash: str) -> list[int]:
        """Ids TorBox reports as cached for this hash.

        Checked before every submit because uncached creates are capped at
        60/hour. The OpenAPI spec documents no response schema for
        ``checkcached`` (``{}``), so this parses defensively and yields only ids
        that are actually usable as integers rather than trusting the shape.

        A cached id is only a *candidate*. TorBox's cache is broader than any one
        account, so an id here is not proof the torrent is in ours -- see
        ``TorboxIntake.submit``, which confirms against mylist before adopting.
        """
        data = await self._call(
            client,
            "GET",
            "/torrents/checkcached",
            params={"hash": info_hash, "format": "object"},
        )
        ids: list[int] = []
        if isinstance(data, list):
            for item in data:
                torrent_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(torrent_id, int) and torrent_id not in ids:
                    ids.append(torrent_id)
        return ids

    async def torrent_in_account(
        self, client: Any, torrent_id: int, *, bypass_cache: bool = True
    ) -> bool:
        """Whether ``torrent_id`` is visible in our own mylist right now.

        Always bypasses the 600s server-side mylist cache: this exists to
        confirm a freshly created or freshly adopted torrent, and a cached
        answer of "absent" would be worse than no answer.
        """
        found = await self.list_torrents(client, torrent_id=torrent_id, bypass_cache=bypass_cache)
        return any(t.id == torrent_id for t in found)

    async def is_cached(self, client: Any, info_hash: str) -> list[str]:
        """Return file names TorBox already holds for this hash.

        Checked before every submit because uncached creates are capped at
        60/hour; a cached torrent skips that budget.
        """
        data = await self._call(
            client,
            "GET",
            "/torrents/checkcached",
            params={"hash": info_hash, "format": "object", "list_files": True},
        )
        names: list[str] = []
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                for item in first.get("files", []) or []:
                    name = item.get("name") if isinstance(item, dict) else None
                    if name:
                        names.append(str(name))
        return names

    async def create_magnet(
        self,
        client: Any,
        magnet: str,
        *,
        seed: int = 1,
        allow_zip: bool = False,
        job_id: int | None = None,
    ) -> int:
        """Submit a magnet link. Returns the torrent id.

        Sent as multipart because that is what the endpoint accepts; a JSON body
        returns success without creating anything.
        """
        if not is_magnet(magnet):
            raise TorboxError("not a magnet link", detail="expected a magnet:? URI")

        data = await self._call(
            client,
            "POST",
            "/torrents/createtorrent",
            data={"magnet": magnet, "seed": seed, "allow_zip": allow_zip},
            files={"file": ("", "")},
        )
        torrent_id = self._extract_id(data)
        LOG.info("torbox torrent created", extra={"torrent_id": torrent_id, "job_id": job_id})
        self._record(job_id, "torbox.created", f"torrent {torrent_id}")
        return torrent_id

    async def create_torrent_file(
        self,
        client: Any,
        files: dict[str, Any] | None,
        *,
        seed: int = 1,
        allow_zip: bool = False,
        job_id: int | None = None,
    ) -> int:
        """Submit a ``.torrent`` file. Same multipart endpoint as a magnet."""
        data = await self._call(
            client,
            "POST",
            "/torrents/createtorrent",
            data={"seed": seed, "allow_zip": allow_zip},
            files=files,
        )
        torrent_id = self._extract_id(data)
        LOG.info("torbox torrent created from file", extra={"torrent_id": torrent_id, "job_id": job_id})
        self._record(job_id, "torbox.created", f"torrent {torrent_id}")
        return torrent_id

    @staticmethod
    def _extract_id(data: Any) -> int:
        if isinstance(data, dict):
            for key in ("torrent_id", "id", "torrentId"):
                value = data.get(key)
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.isdigit():
                    return int(value)
        raise TorboxError(f"could not find a torrent id in the response: {data!r}")

    # -------------------------------------------------------------------- poll

    async def list_torrents(
        self,
        client: Any,
        *,
        torrent_id: int | None = None,
        limit: int | None = None,
        bypass_cache: bool | None = None,
    ) -> list[Torrent]:
        """List torrents, newest state first.

        ``bypass_cache`` is opt-in: the list is cached server-side for 600s, so
        requesting fresh state on every poll spends rate budget to read data that
        is usually identical. A per-call override exists for the cases where a
        cached answer would be actively wrong -- confirming a torrent we just
        submitted.
        """
        params: dict[str, Any] = {}
        if torrent_id is not None:
            params["id"] = torrent_id
        if limit is not None:
            params["limit"] = limit
        if bypass_cache or (bypass_cache is None and self.config.bypass_cache):
            params["bypass_cache"] = True

        data = await self._call(client, "GET", "/torrents/mylist", params=params or None)
        return [t for t in (self._parse(raw) for raw in self._iter_torrents(data)) if t]

    @staticmethod
    def _iter_torrents(data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("torrents", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    @staticmethod
    def _parse(raw: Any) -> Torrent | None:
        if not isinstance(raw, dict):
            return None
        torrent_id = raw.get("id")
        if not isinstance(torrent_id, int):
            return None

        files: list[TorboxFile] = []
        for item in raw.get("files", []) or []:
            if not isinstance(item, dict):
                continue
            file_id = item.get("id")
            if not isinstance(file_id, int):
                continue
            name = str(item.get("name") or item.get("short_name") or "")
            files.append(
                TorboxFile(
                    id=file_id,
                    name=name,
                    size=int(item.get("size") or 0),
                    md5=str(item.get("md5") or ""),
                    short_name=str(item.get("short_name") or ""),
                )
            )

        finished = bool(raw.get("download_finished"))
        present = bool(raw.get("download_present", True))
        raw_state = raw.get("download_state")
        return Torrent(
            id=torrent_id,
            name=str(raw.get("name") or ""),
            hash=str(raw.get("hash") or ""),
            state=state_from(raw_state, finished=finished, present=present),
            raw_state=str(raw_state or ""),
            progress=float(raw.get("progress") or 0.0),
            size=int(raw.get("size") or 0),
            download_speed=int(raw.get("download_speed") or 0),
            eta=int(raw.get("eta") or 0),
            files=files,
        )

    async def get_torrent(self, client: Any, torrent_id: int) -> Torrent | None:
        found = await self.list_torrents(client, torrent_id=torrent_id)
        return found[0] if found else None

    # ------------------------------------------------------------------ fetch

    async def download_url(self, client: Any, torrent_id: int, file_id: int | None = None) -> str:
        """Ask for a CDN URL.

        The key is passed as ``token`` here because that is the only form this
        endpoint accepts. That makes the URL sensitive: it is never logged, and
        callers must not put it in an error message or a store event.
        """
        params: dict[str, Any] = {"token": self.config.api_key, "torrent_id": torrent_id}
        if file_id is not None:
            params["file_id"] = file_id
        # redirect=false returns JSON with the URL. Following the redirect with
        # the key attached would put it in the CDN's logs instead.
        params["redirect"] = "false"

        data = await self._call(client, "GET", "/torrents/requestdl", params=params)
        url = self._extract_url(data)
        LOG.info(
            "torbox download url issued",
            extra={"torrent_id": torrent_id, "file_id": file_id, "redirect": False},
        )
        return url

    @staticmethod
    def _extract_url(data: Any) -> str:
        if isinstance(data, str) and data.startswith("http"):
            return data
        if isinstance(data, dict):
            for key in ("url", "download_url", "link"):
                value = data.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
        raise TorboxError(f"no CDN url in the response: {type(data).__name__}")

    async def delete_torrent(self, client: Any, torrent_id: int, *, job_id: int | None = None) -> bool:
        """Remove a torrent from the account. Never fatal if it fails."""
        try:
            await self._call(
                client,
                "POST",
                "/torrents/controltorrent",
                data={"torrent_id": torrent_id, "control": "delete"},
            )
        except TorboxError as exc:
            LOG.warning(
                "could not delete torbox torrent",
                extra={"torrent_id": torrent_id, "error": exc.error, "detail": exc.detail},
            )
            return False
        self._record(job_id, "torbox.deleted", f"torrent {torrent_id}")
        return True