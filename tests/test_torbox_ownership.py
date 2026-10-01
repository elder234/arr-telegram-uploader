"""Tests for the pieces that make TorBox intake safe to run unattended.

Three of these guard against silently taking over the user's account:

* poll() must never return a torrent we did not submit
* a restart must not re-download everything
* a fetched folder must actually reach the pipeline
"""

import asyncio
import json
from pathlib import Path

from arr_uploader.config import TorboxConfig
from arr_uploader.intake.torbox_intake import Journal, TorboxIntake
from arr_uploader.torbox import Torrent, TorrentState


def _torrent(tid, name="Movie.2024.1080p", state=TorrentState.COMPLETE):
    from arr_uploader.torbox import TorboxFile

    return Torrent(
        id=tid,
        name=name,
        hash="a" * 40,
        state=state,
        raw_state=state.value,
        progress=1.0,
        files=[TorboxFile(id=tid * 10, name="Movie.2024.1080p.mkv", size=100)],
    )


class FakeClient:
    def __init__(self, torrents):
        self._torrents = torrents
        self.deleted = []
        self.created = []

    async def list_torrents(self, http):
        return list(self._torrents)

    async def delete_torrent(self, http, torrent_id):
        self.deleted.append(torrent_id)

    async def is_cached(self, http, info_hash):
        return []

    async def create_magnet(self, http, magnet):
        self.created.append(magnet)
        return 555


def _config(tmp, **kw):
    return TorboxConfig(api_key="k" * 40, watch_dir=str(tmp / "magnets"), **kw)


def test_poll_ignores_torrents_we_never_submitted(tmp_path):
    """mylist returns the whole account.

    A ready torrent the user added by hand is their library, not our job.
    Fetching it would re-download unrelated content every single cycle.
    """
    cfg = _config(tmp_path)
    intake = TorboxIntake(cfg, fetch_dir=str(tmp_path / "fetch"), client=FakeClient([_torrent(999)]))

    result = asyncio.run(intake.poll(object()))

    assert result == [], "fetched a torrent this installation never submitted"


def test_poll_returns_only_our_own_unfetched_torrents(tmp_path):
    cfg = _config(tmp_path)
    intake = TorboxIntake(cfg, fetch_dir=str(tmp_path / "fetch"), client=FakeClient([_torrent(1), _torrent(2), _torrent(3)]))
    intake.journal.bind(1, info_hash="a")
    intake.journal.bind(2, info_hash="b")
    intake.journal.mark_fetched(2)

    ready = asyncio.run(intake.poll(object()))
    ids = {t.id for t in ready}

    assert ids == {1}, f"expected only the owned unfetched torrent, got {ids}"


def test_journal_survives_a_restart(tmp_path):
    """In-memory tracking dies with the process; the journal does not.

    Without this, a restart forgets every id and re-downloads the lot.
    """
    path = tmp_path / "j.json"
    first = Journal(path)
    first.bind(42, info_hash="deadbeef")

    second = Journal(path)

    assert second.unfetched_ids() == {42}
    assert second.get(42)["info_hash"] == "deadbeef"


def test_journal_marks_fetched_only_after_handoff(tmp_path):
    path = tmp_path / "j.json"
    j = Journal(path)
    j.bind(7)

    assert 7 in j.unfetched_ids()
    j.mark_fetched(7)
    assert 7 not in j.unfetched_ids()
    assert Journal(path).unfetched_ids() == set()


def test_corrupt_journal_does_not_orphan_torrents(tmp_path):
    """A truncated journal must not resurrect already-fetched work.

    It resets to empty, which means we fetch nothing -- the safe direction,
    since the alternative is re-downloading content.
    """
    path = tmp_path / "j.json"
    path.write_text("{not json", encoding="utf-8")

    assert Journal(path).unfetched_ids() == set()


def test_fetch_hands_folder_to_inbox(tmp_path):
    """Fetched files are useless until the pipeline is told about them."""
    cfg = _config(tmp_path)
    inbox = tmp_path / "inbox"
    intake = TorboxIntake(
        cfg,
        fetch_dir=str(tmp_path / "fetch"),
        inbox_dir=str(inbox),
        client=FakeClient([]),
    )
    folder = tmp_path / "fetch" / "Movie.2024.1080p"
    folder.mkdir(parents=True)

    asyncio.run(intake.handoff(folder, _torrent(1)))

    files = list(inbox.glob("*.json"))
    assert len(files) == 1, f"expected one job file, got {files}"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["folderPath"] == str(folder)
    assert payload["source"] == "torbox"
    assert payload["torboxId"] == 1


def test_handoff_leaves_no_partial_json(tmp_path):
    """The watcher reads whatever it finds; a half-written file is a bad folder."""
    cfg = _config(tmp_path)
    inbox = tmp_path / "inbox"
    intake = TorboxIntake(cfg, fetch_dir=str(tmp_path / "f"), inbox_dir=str(inbox), client=FakeClient([]))
    folder = tmp_path / "f" / "Movie"
    folder.mkdir(parents=True)

    asyncio.run(intake.handoff(folder, _torrent(1)))

    assert not list(inbox.glob("*.tmp")), "temp file was left behind"
    for f in inbox.glob("*.json"):
        json.loads(f.read_text(encoding="utf-8"))  # raises if truncated


def test_handoff_without_inbox_warns_instead_of_silently_dropping(tmp_path):
    cfg = _config(tmp_path)
    intake = TorboxIntake(cfg, fetch_dir=str(tmp_path / "f"), client=FakeClient([]))
    folder = tmp_path / "f" / "Movie"
    folder.mkdir(parents=True)

    asyncio.run(intake.handoff(folder, _torrent(1)))  # must not raise


def test_watch_dir_is_created_not_assumed(tmp_path):
    """A configured-but-missing watch dir would look like 'no magnets' forever."""
    watch = tmp_path / "nested" / "magnets"
    cfg = _config(tmp_path)
    cfg.watch_dir = str(watch)

    TorboxIntake(cfg, fetch_dir=str(tmp_path / "fetch"), client=FakeClient([]))

    assert watch.is_dir(), "watch dir was not created"


def test_uncreatable_watch_dir_does_not_raise(tmp_path):
    """An invalid watch dir must log, not stop uploads."""
    cfg = _config(tmp_path)
    cfg.watch_dir = "\0invalid"

    TorboxIntake(cfg, fetch_dir=str(tmp_path / "fetch"), client=FakeClient([]))  # must not raise