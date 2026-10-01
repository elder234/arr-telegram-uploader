"""TorBox client and intake tests.

No network: ``FakeHTTP`` replays the documented envelopes. The point is to pin
the contract from the published OpenAPI spec -- multipart create, the
``success`` envelope, base32 hash normalisation, state mapping, and the path
safety that keeps a hostile torrent name from escaping the fetch directory.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

from arr_uploader.config import TorboxConfig
from arr_uploader.intake.torbox_intake import TorboxIntake
from arr_uploader.torbox import (
    FREE_PLAN_MAX_BYTES,
    TorboxClient,
    TorboxError,
    Torrent,
    TorrentState,
    hash_from_magnet,
    is_magnet,
    state_from,
)


# --------------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, payload: object, status: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Records requests and replays queued responses."""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        # Deliberately not logging headers: they carry the API key.
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "data": kwargs.get("data"),
                "files": kwargs.get("files"),
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)


def ok(data: object) -> FakeResponse:
    return FakeResponse({"success": True, "error": "", "detail": "ok", "data": data})


def fail(error: str, detail: str = "nope") -> FakeResponse:
    return FakeResponse({"success": False, "error": error, "detail": detail, "data": None})


# ----------------------------------------------------------------- the client


def test_hash_from_magnet_normalises_base32_to_hex():
    digest = bytes(range(20))
    magnet = "magnet:?xt=urn:btih:%s&dn=movie" % base64.b32encode(digest).decode()
    # TorBox reports hex. Comparing base32 against hex reports every torrent as
    # uncached, which then burns the 60/hour create budget.
    assert hash_from_magnet(magnet) == digest.hex()


def test_hash_from_magnet_accepts_hex_any_case():
    digest = bytes(range(20))
    assert hash_from_magnet(f"magnet:?xt=urn:btih:{digest.hex().upper()}") == digest.hex()


def test_hash_from_magnet_rejects_malformed():
    for bad in ("magnet:?dn=nohash", "magnet:?xt=urn:sha1:abc", "http://example.com"):
        try:
            hash_from_magnet(bad)
        except TorboxError:
            continue
        raise AssertionError(f"accepted a bad magnet: {bad}")


def test_is_magnet():
    assert is_magnet("magnet:?xt=urn:btih:abc")
    assert not is_magnet("https://example.com/file.torrent")


def test_state_from_prefers_finished_over_seeding():
    # TorBox keeps reporting "uploading" for a finished torrent. Waiting for the
    # seed ratio would stall the upload leg forever.
    assert state_from("uploading", finished=True) is TorrentState.COMPLETE
    assert state_from("uploading", finished=False) is TorrentState.UPLOADING


def test_state_from_treats_missing_bytes_as_error():
    # finished but the file is gone: fetching will fail, so do not poll forever.
    assert state_from("cached", finished=True, present=False) is TorrentState.ERROR


def test_state_from_maps_documented_errors():
    for raw in ("failed", "Failed (Processing)", "Expired", "(Reported) Missing", "Incomplete"):
        assert state_from(raw) is TorrentState.ERROR, raw


def test_state_from_unknown_is_not_an_error():
    # A new upstream state must not delete anything or raise.
    assert state_from("brandNewState") is TorrentState.UNKNOWN
    assert state_from(None) is TorrentState.UNKNOWN


def test_torrent_ready_only_when_bytes_exist():
    assert Torrent(id=1, name="x", state=TorrentState.COMPLETE).ready
    assert Torrent(id=1, name="x", state=TorrentState.CACHED).ready
    assert not Torrent(id=1, name="x", state=TorrentState.DOWNLOADING).ready
    assert not Torrent(id=1, name="x", state=TorrentState.METADATA).ready


def test_client_raises_on_success_false_even_with_http_200():
    async def run():
        http = FakeHTTP([fail("NO_AUTH", "bad key")])
        client = TorboxClient(TorboxConfig(api_key="k"))
        try:
            await client.list_torrents(http)
        except TorboxError as exc:
            return exc
        raise AssertionError("expected TorboxError")

    exc = asyncio.run(run())
    assert exc.error == "NO_AUTH"
    assert exc.detail == "bad key"


def test_client_sends_key_as_bearer_header_not_in_url():
    async def run():
        http = FakeHTTP([ok({"torrents": []})])
        client = TorboxClient(TorboxConfig(api_key="secret-key"))
        await client.list_torrents(http)
        return http

    http = asyncio.run(run())
    # A key in the URL leaks into access logs and httpx error messages.
    assert "secret-key" not in http.calls[0]["url"]
    assert http.calls[0]["params"] is None


def test_create_magnet_uses_multipart_with_empty_file_part():
    async def run():
        http = FakeHTTP([ok({"torrent_id": 77})])
        client = TorboxClient(TorboxConfig(api_key="k"))
        tid = await client.create_magnet(http, "magnet:?xt=urn:btih:" + "a" * 40)
        return tid, http

    tid, http = asyncio.run(run())
    assert tid == 77
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/torrents/createtorrent")
    # The endpoint is multipart/form-data; a JSON body creates nothing.
    assert call["files"] is not None
    assert call["data"]["magnet"].startswith("magnet:?")


def test_create_magnet_rejects_non_magnet_before_calling_api():
    async def run():
        http = FakeHTTP()
        client = TorboxClient(TorboxConfig(api_key="k"))
        try:
            await client.create_magnet(http, "https://not-a-magnet")
        except TorboxError as exc:
            return exc, len(http.calls)
        raise AssertionError("expected TorboxError")

    exc, call_count = asyncio.run(run())
    # str(exc) is the user-facing detail, not the internal message.
    assert "magnet:? URI" in str(exc)
    # Nothing should have hit the network.
    assert call_count == 0


def test_list_torrents_parses_files_and_state():
    payload = {
        "torrents": [
            {
                "id": 5,
                "name": "Movie.2024.1080p",
                "hash": "ab" * 20,
                "download_state": "uploading",
                "download_finished": True,
                "download_present": True,
                "progress": 1.0,
                "size": 14745600000,
                "files": [
                    {"id": 1, "name": "Movie.2024.1080p.mkv", "size": 14745600000, "md5": "deadbeef"},
                    {"id": 2, "name": "Movie.2024.1080p.srt", "size": 51200},
                    {"id": 3, "name": "readme.txt", "size": 100},
                ],
            }
        ]
    }

    async def run():
        http = FakeHTTP([ok(payload)])
        client = TorboxClient(TorboxConfig(api_key="k"))
        return await client.list_torrents(http)

    torrents = asyncio.run(run())
    assert len(torrents) == 1
    t = torrents[0]
    assert t.id == 5
    assert t.state is TorrentState.COMPLETE
    assert t.ready
    # Subtitles are recognised so they can be uploaded alongside the video.
    assert [f.name for f in t.video_files] == ["Movie.2024.1080p.mkv"]
    assert len(t.files) == 3


def test_download_url_requests_json_not_redirect():
    async def run():
        http = FakeHTTP([ok("https://cdn.torbox.app/f/abc")])
        client = TorboxClient(TorboxConfig(api_key="k"))
        url = await client.download_url(http, 5, 1)
        return url, http

    url, http = asyncio.run(run())
    assert url == "https://cdn.torbox.app/f/abc"
    params = http.calls[0]["params"]
    # This endpoint takes the key as a query param; it must not be logged.
    assert params["redirect"] == "false"
    assert params["torrent_id"] == 5
    assert params["file_id"] == 1


def test_free_plan_cap_is_the_documented_10gib():
    assert FREE_PLAN_MAX_BYTES == 10 * 1024**3


# ------------------------------------------------------------------ the intake


def _intake(tmp: Path, watch: Path, **kwargs) -> TorboxIntake:
    return TorboxIntake(
        TorboxConfig(api_key="k", watch_dir=str(watch)),
        fetch_dir=str(tmp / "fetch"),
        **kwargs,
    )


def test_safe_dirname_blocks_traversal():
    # A torrent name is attacker-controlled and we mkdir under it.
    for hostile in ("../../etc", "..", "../..", "....//....//x"):
        name = TorboxIntake._safe_dirname(Torrent(id=1, name=hostile))
        assert "/" not in name, hostile
        assert not Path(name).is_absolute(), hostile
        assert ".." not in name.split("."), hostile


def test_safe_dirname_falls_back_for_empty_names():
    assert TorboxIntake._safe_dirname(Torrent(id=9, name="")) == "torrent-9"
    assert TorboxIntake._safe_dirname(Torrent(id=9, name="...")) == "torrent-9"


def test_safe_filename_strips_directories():
    name = TorboxIntake._safe_filename("../../etc/passwd")
    assert "/" not in name
    assert Path(name).name == name


def test_discover_finds_magnets_and_ignores_submitted():
    with tempfile.TemporaryDirectory() as d:
        watch = Path(d)
        (watch / "a.magnet").write_text("magnet:?xt=urn:btih:" + "a" * 40, encoding="utf-8")
        (watch / "b.magnet").write_text("not a magnet", encoding="utf-8")
        (watch / "c.torrent").write_bytes(b"d4:infod4:name4:teste")
        (watch / "d.magnet.submitted").write_text("magnet:?xt=urn:btih:x", encoding="utf-8")

        found = _intake(Path(d), watch).discover()
        names = sorted(p.name for p, _ in found)
        # b.magnet is not a magnet; d is already submitted.
        assert names == ["a.magnet", "c.torrent"]


def test_submitted_magnet_is_renamed_so_restart_does_not_duplicate():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            watch = Path(d)
            src = watch / "a.magnet"
            src.write_text("magnet:?xt=urn:btih:" + "a" * 40, encoding="utf-8")

            http = FakeHTTP([ok([]), ok({"torrent_id": 42})])
            intake = _intake(Path(d), watch)
            await intake.submit_all(http)

            # A second cycle must not submit the same magnet again.
            before = len(http.calls)
            await intake.submit_all(http)
            assert len(http.calls) == before, "resubmitted a tracked magnet"
            assert src.with_suffix(".magnet.submitted").exists()
            assert intake.stats.submitted == 1

    asyncio.run(run())


def test_cached_magnet_skips_create():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            watch = Path(d)
            digest = bytes(range(20))
            magnet = "magnet:?xt=urn:btih:%s" % base64.b32encode(digest).decode()
            (watch / "a.magnet").write_text(magnet, encoding="utf-8")

            # checkcached says we already have it, then the create still returns
            # an id (cached creates are cheap), then poll returns nothing.
            http = FakeHTTP(
                [
                    ok([{"name": "Movie.mkv", "files": [{"name": "Movie.mkv"}]}]),
                    ok({"torrent_id": 8}),
                ]
            )
            intake = _intake(Path(d), watch)
            await intake.submit_all(http)
            assert intake.stats.skipped_cached == 1
            assert intake.stats.submitted == 1

    asyncio.run(run())


def test_submit_failure_is_recorded_not_raised():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            watch = Path(d)
            (watch / "bad.magnet").write_text("magnet:?xt=urn:btih:" + "b" * 40, encoding="utf-8")
            http = FakeHTTP([fail("PLAN_RESTRICTED_FEATURE", "upgrade required")])
            intake = _intake(Path(d), watch)
            stats = await intake.submit_all(http)
            # One bad magnet must not stop the intake loop.
            assert stats.failed == 1
            assert "upgrade required" in stats.errors[0]
            assert (watch / "bad.magnet").exists(), "failed magnet must stay for retry"

    asyncio.run(run())


def test_dry_run_makes_no_requests():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            watch = Path(d)
            (watch / "a.magnet").write_text("magnet:?xt=urn:btih:" + "a" * 40, encoding="utf-8")
            http = FakeHTTP()
            intake = _intake(Path(d), watch)
            await intake.submit_all(http, dry_run=True)
            assert http.calls == []

    asyncio.run(run())


def test_poll_skips_error_states():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            http = FakeHTTP(
                [
                    ok(
                        {
                            "torrents": [
                                {"id": 1, "download_state": "Failed", "download_finished": False},
                                {"id": 2, "download_state": "uploading", "download_finished": True},
                            ]
                        }
                    )
                ]
            )
            intake = TorboxIntake(TorboxConfig(api_key="k"), fetch_dir=str(Path(d) / "f"))
            return await intake.poll(http)

    ready = asyncio.run(run())
    assert [t.id for t in ready] == [2]


def test_fetch_refuses_torrent_over_the_free_plan_cap():
    from arr_uploader.torbox import TorboxFile

    async def run():
        with tempfile.TemporaryDirectory() as d:
            http = FakeHTTP()
            intake = TorboxIntake(TorboxConfig(api_key="k"), fetch_dir=str(Path(d) / "f"))
            huge = Torrent(
                id=1,
                name="Big",
                state=TorrentState.COMPLETE,
                files=[TorboxFile(id=1, name="big.mkv", size=FREE_PLAN_MAX_BYTES + 1)],
            )
            result = await intake.fetch(http, huge)
            assert result is None
            assert http.calls == [], "must not request a url it cannot download"

    asyncio.run(run())


def test_fetch_short_read_is_a_failure_and_leaves_no_file():
    from arr_uploader.torbox import TorboxFile

    async def run():
        with tempfile.TemporaryDirectory() as d:

            class StreamCtx:
                def __init__(self, exc: Exception) -> None:
                    self._exc = exc

                async def __aenter__(self):
                    raise self._exc

                async def __aexit__(self, *a):
                    return False

            class H(FakeHTTP):
                def stream(self, method, url, **kw):
                    return StreamCtx(RuntimeError("connection reset"))

            http = H([ok("https://cdn/f")])
            intake = TorboxIntake(TorboxConfig(api_key="k"), fetch_dir=str(Path(d) / "fetch"))
            torrent = Torrent(
                id=1,
                name="Movie",
                state=TorrentState.COMPLETE,
                files=[TorboxFile(id=1, name="Movie.mkv", size=1000)],
            )
            result = await intake.fetch(http, torrent)
            assert result is None, "a truncated fetch must not look like success"
            # Nothing partially written is left behind for the pipeline to find.
            assert not list((Path(d) / "fetch").rglob("*.mkv"))

    asyncio.run(run())