"""TorBox intake: magnets in, files on disk out.

This is the half that was missing. The pipeline already knew how to upload a
movie and delete it after verification; nothing submitted a torrent or fetched
the bytes. This module closes that loop:

    watch dir -> magnet -> TorBox -> poll -> CDN url -> download to disk
              -> hand folder to the existing pipeline

Two design points worth stating:

* Nothing is deleted until the uploader has verified the upload. The fetch stage
  writes into ``fetch_dir``; only the pipeline's existing verified-delete gate
  removes data, and only from ``media_root``. A half-written download is never
  handed on.
* A magnet is journalled before submission. If TorBox accepts it and we crash
  before writing the id, the next start would create a duplicate torrent, so the
  journal records the intent first and ``checkcached`` covers the gap.
* **Only torrents this installation submitted are ever fetched.** ``mylist``
  returns the whole account, and a ready torrent we never asked for is the
  user's own library, not our job. An empty journal therefore fetches nothing,
  which is the safe direction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import TorboxConfig
from ..torbox import (
    FREE_PLAN_MAX_BYTES,
    Torrent,
    TorrentState,
    TorboxClient,
    TorboxError,
    hash_from_magnet,
    is_magnet,
)

LOG = logging.getLogger(__name__)

# A .torrent file is bencode; we only need its name, and parsing that from raw
# bytes avoids a dependency. Good enough for a staging filename.
_NAME_RE = re.compile(rb"\d+:name(\d+):")


class Journal:
    """Durable record of which torrent ids we own.

    This exists because ``mylist`` returns the entire account. Without a
    persistent list of ids we submitted, the poll loop would happily re-download
    the user's whole library on every cycle, and a restart would forget every
    id it had already handled.

    Writes are atomic (temp file + replace) because a truncated journal is worse
    than no journal: it would silently orphan torrents and re-fetch them.
    """

    SUBMITTED = "submitted"
    FETCHED = "fetched"

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            LOG.error("could not read torbox journal", extra={"path": str(self.path), "error": str(exc)})
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            LOG.error("torbox journal is corrupt, starting empty", extra={"path": str(self.path)})
            return
        entries = data.get("torrents") if isinstance(data, dict) else None
        if isinstance(entries, dict):
            self.entries = {str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = json.dumps({"version": 1, "torrents": self.entries}, indent=2, sort_keys=True)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def record_intent(self, info_hash: str, magnet: str, source: str) -> None:
        """Note the magnet before the API call, so a crash cannot duplicate it."""
        if not info_hash:
            return
        self.entries[f"hash:{info_hash}"] = {
            "info_hash": info_hash,
            "magnet": magnet,
            "source": source,
            "created": time.time(),
        }
        self._flush()

    def bind(self, torrent_id: int, **fields: Any) -> None:
        entry = {"state": self.SUBMITTED, "created": time.time()}
        entry.update(fields)
        self.entries[str(torrent_id)] = entry
        self._flush()

    def get(self, torrent_id: int) -> dict[str, Any] | None:
        return self.entries.get(str(torrent_id))

    def mark_fetched(self, torrent_id: int) -> None:
        entry = self.entries.setdefault(str(torrent_id), {})
        entry["state"] = self.FETCHED
        entry["fetched_at"] = time.time()
        self._flush()

    def unfetched_ids(self) -> set[int]:
        """Ids we submitted and have not yet handed to the pipeline."""
        return {
            int(key)
            for key, value in self.entries.items()
            if key.isdigit() and value.get("state") != self.FETCHED
        }

    def submitted_count(self) -> int:
        return len(self.unfetched_ids())


@dataclass(slots=True)
class PendingMagnet:
    """A magnet waiting on TorBox, keyed by info-hash so restarts are idempotent."""

    info_hash: str
    magnet: str
    torrent_id: int | None = None
    source: str = ""


@dataclass(slots=True)
class IntakeStats:
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    skipped_cached: int = 0
    errors: list[str] = field(default_factory=list)


def _read_torrent_name(path: Path) -> str:
    """Best-effort torrent name for logging. Never used as a path component."""
    try:
        blob = path.read_bytes()
    except OSError:
        return path.stem
    match = _NAME_RE.search(blob)
    if not match:
        return path.stem
    length = int(match.group(1))
    start = match.end()
    try:
        return blob[start : start + length].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return path.stem


class TorboxIntake:
    """Watches a directory of magnets and fetches finished torrents to disk."""

    def __init__(
        self,
        config: TorboxConfig,
        *,
        fetch_dir: str,
        client: TorboxClient | None = None,
        on_movie: Callable[[Path], Any] | None = None,
        inbox_dir: str = "",
        journal: Journal | None = None,
    ) -> None:
        self.config = config
        self.fetch_dir = Path(fetch_dir)
        self.client = client or TorboxClient(config)
        # Called with the completed folder. The pipeline consumes this.
        self._on_movie = on_movie
        # Where completed folders are announced. Empty means "do not hand off",
        # which is only correct for a manual fetch with no pipeline running.
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        self.stats = IntakeStats()
        # hash -> PendingMagnet. Keeps a restart from re-submitting.
        self._pending: dict[str, PendingMagnet] = {}
        self.journal = journal if journal is not None else Journal(self.fetch_dir / ".torbox-journal.json")

        if config.watch_dir:
            # Created rather than assumed: a configured-but-missing watch dir
            # would otherwise look like "no magnets" forever, and the operator
            # would be dropping files into a folder that was never created.
            try:
                Path(config.watch_dir).mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as exc:
                # ValueError too: a path with an embedded null raises that rather
                # than OSError, and it must not take the worker down.
                LOG.error("could not create torbox watch dir", extra={"dir": config.watch_dir, "error": str(exc)})

    # ------------------------------------------------------------- discovery

    def discover(self) -> list[tuple[Path, str]]:
        """Find magnet files and .torrent files in the watch dir.

        Files are renamed to ``.submitted`` after a successful submit so a
        restart does not submit the same magnet twice. A magnet TorBox already
        has is left in place, because it is still our record that we want it.
        """
        if not self.config.watch_dir:
            return []
        root = Path(self.config.watch_dir)
        if not root.is_dir():
            return []

        found: list[tuple[Path, str]] = []
        for entry in sorted(root.iterdir()):
            if entry.suffix == ".submitted":
                continue
            try:
                if entry.suffix == ".magnet":
                    text = entry.read_text(encoding="utf-8").strip()
                    if is_magnet(text):
                        found.append((entry, text))
                elif entry.suffix == ".torrent":
                    found.append((entry, ""))
            except OSError as exc:
                LOG.warning("could not read watch entry", extra={"path": str(entry), "error": str(exc)})
        return found

    # ---------------------------------------------------------------- submit

    async def submit_all(self, http: Any, *, dry_run: bool = False) -> IntakeStats:
        """Submit every discovered magnet that is not already tracked."""
        entries = self.discover()
        if not entries:
            LOG.debug("torbox watch dir is empty", extra={"dir": self.config.watch_dir})
            return self.stats

        LOG.info("torbox intake found magnets", extra={"count": len(entries)})

        for path, magnet in entries:
            if dry_run:
                LOG.info("would submit", extra={"path": path.name})
                continue
            try:
                await self._submit_one(http, path, magnet)
            except TorboxError as exc:
                self.stats.failed += 1
                self.stats.errors.append(f"{path.name}: {exc.detail}")
                LOG.error("torbox submit failed", extra={"path": path.name, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001 - one bad magnet must not stop intake
                self.stats.failed += 1
                self.stats.errors.append(f"{path.name}: {exc}")
                LOG.exception("unexpected torbox submit error", extra={"path": path.name})

        return self.stats

    async def _submit_one(self, http: Any, path: Path, magnet: str) -> None:
        if magnet:
            info_hash = hash_from_magnet(magnet)
        else:
            # A .torrent file: read its name only. Hashing requires parsing the
            # bencode infohash, which we deliberately do not do here; the id
            # returned by createtorrent is authoritative.
            info_hash = ""

        if info_hash and info_hash in self._pending:
            LOG.debug("magnet already tracked", extra={"hash": info_hash})
            return

        if info_hash:
            # A cached hash means TorBox already holds the content, so we can
            # avoid the create and the 60/hour uncached-create budget. But
            # "cached" is not "in our account": the cache is shared, and adopting
            # an id we do not own would leave the torrent unfetched forever,
            # because poll() only considers journal-bound ids. So each candidate
            # is confirmed against mylist before it is adopted.
            for cached_id in await self.client.cached_torrent_ids(http, info_hash):
                try:
                    in_account = await self.client.torrent_in_account(http, cached_id)
                except TorboxError as exc:
                    LOG.warning(
                        "cached torrent lookup failed, creating instead",
                        extra={"id": cached_id, "detail": exc.detail},
                    )
                    break
                if not in_account:
                    LOG.info(
                        "cached torrent is not in this account, creating instead",
                        extra={"id": cached_id, "hash": info_hash},
                    )
                    break
                # Adopt: bind the id so we own the download, then treat the
                # magnet as submitted. No create call is made.
                self.stats.skipped_cached += 1
                LOG.info(
                    "torbox already cached, adopting existing torrent",
                    extra={"id": cached_id, "hash": info_hash},
                )
                self.journal.bind(
                    cached_id,
                    info_hash=info_hash,
                    magnet=magnet,
                    source=str(path),
                    name=path.stem,
                )
                self._pending[info_hash] = PendingMagnet(
                    info_hash=info_hash,
                    magnet=magnet,
                    torrent_id=cached_id,
                    source=str(path),
                )
                self._mark_submitted(path)
                return

        magnet_value = magnet
        files: dict[str, Any] | None = None
        if not magnet:
            with path.open("rb") as fh:
                files = {"file": (path.name, fh, "application/x-bittorrent")}
            magnet_value = ""

        # Record before the call, not after. If TorBox accepts the create and we
        # die before writing the id, the next start re-reads this intent and
        # checkcached finds the torrent instead of creating a second one.
        self.journal.record_intent(info_hash, magnet, str(path))

        if magnet:
            torrent_id = await self.client.create_magnet(http, magnet)
        else:
            torrent_id = await self.client.create_torrent_file(http, files)

        self.stats.submitted += 1
        self.journal.bind(
            torrent_id,
            info_hash=info_hash,
            magnet=magnet,
            source=str(path),
            name=path.stem,
        )
        self._pending[info_hash or str(torrent_id)] = PendingMagnet(
            info_hash=info_hash,
            magnet=magnet,
            torrent_id=torrent_id,
            source=str(path),
        )
        self._mark_submitted(path)

    @staticmethod
    def _mark_submitted(path: Path) -> None:
        """Rename out of the way so a restart does not resubmit."""
        target = path.with_suffix(path.suffix + ".submitted")
        try:
            path.replace(target)
        except OSError as exc:
            # Not fatal: the in-memory map still prevents a double submit this
            # run, and a restart may duplicate one torrent.
            LOG.warning("could not mark magnet submitted", extra={"path": str(path), "error": str(exc)})

    # ------------------------------------------------------------------ poll

    async def poll(self, http: Any) -> list[Torrent]:
        """Return torrents *we submitted* that have become ready to fetch.

        ``mylist`` returns every torrent on the account, including the user's
        own library. Fetching those would re-download unrelated content on
        every cycle, so anything not in the journal is ignored -- even when it
        is complete and ready.
        """
        owned = self.journal.unfetched_ids()
        if not owned:
            LOG.debug("no owned torrents awaiting fetch")
            return []

        try:
            torrents = await self.client.list_torrents(http)
        except TorboxError as exc:
            LOG.error("torbox poll failed", extra={"detail": exc.detail})
            return []

        ready: list[Torrent] = []
        foreign = 0
        for torrent in torrents:
            if torrent.id not in owned:
                foreign += 1
                continue
            if torrent.state is TorrentState.ERROR:
                LOG.warning(
                    "torbox torrent in error state",
                    extra={"id": torrent.id, "name": torrent.name, "state": torrent.raw_state},
                )
                continue
            if torrent.ready:
                ready.append(torrent)
        if foreign:
            LOG.info(
                "ignored torrents this installation did not submit",
                extra={"count": foreign, "owned": len(owned)},
            )
        return ready

    # ----------------------------------------------------------------- fetch

    async def fetch(self, http: Any, torrent: Torrent) -> Path | None:
        """Download a finished torrent's files into ``fetch_dir``.

        Writes to ``.part`` and renames on completion, so a crash never leaves a
        truncated file that looks finished to the pipeline.
        """
        video = torrent.video_files
        subs = [f for f in torrent.files if f.is_subtitle]
        # Subtitles are sidecars the pipeline uploads next to the video, so they
        # have to be fetched too. Falling back to *all* files when there is no
        # video keeps an audio-only or unknown-extension release from silently
        # producing nothing.
        files = video + subs if video else torrent.files
        if not files:
            LOG.warning("torbox torrent has no files", extra={"id": torrent.id, "name": torrent.name})
            return None

        total = sum(f.size for f in files)
        if total > FREE_PLAN_MAX_BYTES:
            LOG.error(
                "torbox torrent exceeds the free plan size cap",
                extra={"id": torrent.id, "size": total, "cap": FREE_PLAN_MAX_BYTES},
            )
            return None

        target_dir = self.fetch_dir / self._safe_dirname(torrent)
        target_dir.mkdir(parents=True, exist_ok=True)

        written_any = False
        for item in files:
            try:
                url = await self.client.download_url(http, torrent.id, item.id)
            except TorboxError as exc:
                LOG.error("could not get torbox url", extra={"file": item.name, "detail": exc.detail})
                continue

            dest = target_dir / self._safe_filename(item.name)
            if await self._download_file(http, url, dest, item.size, torrent, item):
                written_any = True

        if not written_any:
            LOG.error("torbox fetch produced no files", extra={"id": torrent.id, "name": torrent.name})
            return None

        self.stats.completed += 1
        LOG.info(
            "torbox fetch complete",
            extra={"id": torrent.id, "name": torrent.name, "dir": str(target_dir)},
        )
        await self.handoff(target_dir, torrent)
        return target_dir

    async def handoff(self, folder: Path, torrent: Torrent) -> None:
        """Announce a completed folder to the upload pipeline.

        Writes the same JSON the Radarr webhook and Custom Script use, into the
        same inbox, so there is exactly one path into the store. Written to a
        temp file and renamed, so the watcher never reads a partial payload.
        """
        if self._on_movie is not None:
            await self._maybe_await(self._on_movie(folder))
        if self.inbox_dir is None:
            LOG.warning(
                "fetched folder has no inbox configured, nothing will upload it",
                extra={"dir": str(folder)},
            )
            return

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "folderPath": str(folder),
            "title": torrent.name,
            "source": "torbox",
            "torboxId": torrent.id,
        }
        target = self.inbox_dir / f"torbox-{torrent.id}-{abs(hash(str(folder))) % 10**8}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)
        LOG.info("handed off to pipeline", extra={"dir": str(folder), "inbox": target.name})

    async def _download_file(
        self,
        http: Any,
        url: str,
        dest: Path,
        expected: int,
        torrent: Torrent,
        item: Any,
    ) -> bool:
        """Stream one file. Size is checked; a short read is a failure."""
        part = dest.with_suffix(dest.suffix + ".part")
        downloaded = 0

        try:
            # stream the CDN url with our own client so the TorBox key is not
            # attached to the CDN request. The url itself already carries a token.
            async with http.stream("GET", url, timeout=None) as response:
                response.raise_for_status()
                declared = int(response.headers.get("content-length") or 0)
                if expected and declared and declared != expected:
                    LOG.warning(
                        "content-length disagrees with the file listing",
                        extra={"file": item.name, "declared": declared, "listed": expected},
                    )
                with part.open("wb") as fh:
                    async for chunk in response.aiter_bytes(chunk_size=4 * 1024 * 1024):
                        fh.write(chunk)
                        downloaded += len(chunk)
        except Exception as exc:  # noqa: BLE001 - transport errors are many and variable
            LOG.error(
                "torbox file download failed",
                extra={"file": item.name, "id": torrent.id, "error": str(exc), "got": downloaded},
            )
            part.unlink(missing_ok=True)
            return False

        if expected and downloaded != expected:
            LOG.error(
                "torbox file size mismatch",
                extra={"file": item.name, "id": torrent.id, "got": downloaded, "want": expected},
            )
            part.unlink(missing_ok=True)
            return False

        part.replace(dest)
        return True

    @staticmethod
    def _safe_dirname(torrent: Torrent) -> str:
        """Directory name that cannot escape ``fetch_dir``.

        A torrent name is attacker-controlled: it can contain ``..`` or a path
        separator, and we are about to mkdir and write under it.
        """
        raw = torrent.name or f"torrent-{torrent.id}"
        cleaned = re.sub(r"[^A-Za-z0-9 ._-]", "_", raw).strip(" .")
        cleaned = cleaned[:120] or f"torrent-{torrent.id}"
        # Guard against "..' -> after substitution a name of ".." can still occur.
        if set(cleaned) <= {"."}:
            cleaned = f"torrent-{torrent.id}"
        return cleaned

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9 ._-]", "_", Path(name).name).strip(" .")
        return cleaned[:200] or "file.bin"

    # ----------------------------------------------------------------- loop

    async def run_forever(self, http: Any, stop: Callable[[], bool]) -> None:
        """Poll and fetch until asked to stop."""
        LOG.info(
            "torbox intake starting",
            extra={"watch": self.config.watch_dir, "fetch": str(self.fetch_dir), "interval": self.config.poll_interval_seconds},
        )
        while not stop():
            try:
                await self.submit_all(http)
                for torrent in await self.poll(http):
                    if stop():
                        break
                    folder = await self.fetch(http, torrent)
                    if folder is None:
                        continue
                    # Marked only after the fetch succeeded and the folder was
                    # announced. A crash before this re-fetches, which is
                    # recoverable; marking first would strand the torrent with
                    # no files on disk and nothing to upload.
                    self.journal.mark_fetched(torrent.id)
                    if self.config.delete_after_fetch:
                        # Only ever after the bytes are on disk and handed off.
                        await self.client.delete_torrent(http, torrent.id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                LOG.exception("torbox intake cycle failed")

            try:
                await asyncio.wait_for(asyncio.sleep(self.config.poll_interval_seconds), timeout=self.config.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
            return await value
        return value