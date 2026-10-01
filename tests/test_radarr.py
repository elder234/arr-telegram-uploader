"""Tests for the Radarr client.

The bug these guard against: every method was declared ``async`` but issued
blocking ``client.get(...)`` calls. Against an ``httpx.AsyncClient`` that returns
a coroutine, ``raise_for_status()`` raised ``AttributeError``, and the broad
``except`` converted it into a silent ``False``. Radarr then kept searching for
movies that had already been uploaded, with no error anywhere.
"""

from __future__ import annotations

import asyncio
import json

from arr_uploader.config import RadarrConfig
from arr_uploader.radarr import RadarrClient, RadarrResult


class _Response:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self._payload


class _AsyncStub:
    """Minimal async httpx.AsyncClient stand-in.

    Every method is a coroutine, so a stub that is not awaited raises loudly
    rather than silently returning a coroutine object -- which is exactly how the
    original defect presented in production.
    """

    def __init__(self, movie=None, exclusions=None, fail: bool = False) -> None:
        self.movie = movie if movie is not None else {"id": 7, "monitored": True, "title": "Sintel"}
        self.exclusions = exclusions if exclusions is not None else []
        self.fail = fail
        self.calls: list[tuple[str, str, object]] = []

    @staticmethod
    def _path(url: str) -> str:
        # Strip scheme://host, which "http://r:7878" does not cleanly separate.
        without_scheme = url.split("://", 1)[-1]
        return "/" + without_scheme.split("/", 1)[-1] if "/" in without_scheme else "/"

    async def _respond(self, verb: str, path: str, json_body=None):
        self.calls.append((verb, path, json_body))
        if self.fail:
            raise RuntimeError("connection refused")
        if path.startswith("/api/v3/exclusions"):
            return _Response(self.exclusions)
        return _Response(self.movie)

    async def get(self, url, headers=None):
        return await self._respond("GET", self._path(url), None)

    async def put(self, url, headers=None, json=None):
        return await self._respond("PUT", self._path(url), json)

    async def post(self, url, headers=None, json=None):
        return await self._respond("POST", self._path(url), json)


def cfg(**over):
    base = {"url": "http://r:7878", "api_key": "k", "unmonitor_after_upload": True, "exclude_after_upload": True}
    base.update(over)
    return RadarrConfig(**base)


def run(coro):
    return asyncio.run(coro)


def test_unmonitor_sets_monitored_false():
    stub = _AsyncStub()
    result = run(RadarrClient(cfg()).unmonitor(stub, 7))
    assert result is True
    verbs = {c[0] for c in stub.calls}
    assert verbs == {"GET", "PUT"}
    put_body = next(c[2] for c in stub.calls if c[0] == "PUT")
    assert put_body["monitored"] is False


def test_unmonitor_already_false_skips_write():
    stub = _AsyncStub(movie={"id": 7, "monitored": False})
    assert run(RadarrClient(cfg()).unmonitor(stub, 7)) is True
    assert [c[0] for c in stub.calls] == ["GET"]


def test_unmonitor_failure_returns_false_and_logs_event():
    events = []
    client = RadarrClient(cfg(), event_log=lambda j, e, d: events.append((j, e)))
    assert run(client.unmonitor(_AsyncStub(fail=True), 7, job_id=42)) is False
    assert any(e[0] == 42 and e[1] == "radarr.unmonitor_failed" for e in events)


def test_exclude_adds_payload():
    stub = _AsyncStub()
    assert run(RadarrClient(cfg()).exclude_from_import(stub, 12345, "Sintel")) is True
    post = next(c for c in stub.calls if c[0] == "POST")
    assert post[2]["tmdbId"] == 12345
    assert post[2]["title"] == "Sintel"


def test_exclude_detects_existing_entry():
    stub = _AsyncStub(exclusions=[{"tmdbId": 12345, "title": "Sintel"}])
    assert run(RadarrClient(cfg()).exclude_from_import(stub, 12345, "Sintel")) is True
    assert not [c for c in stub.calls if c[0] == "POST"]


def test_finalize_both_legs():
    stub = _AsyncStub()
    result = run(RadarrClient(cfg()).finalize(stub, 7, 12345, "Sintel"))
    assert result.unmonitored is True
    assert result.excluded is True
    assert result.ok is True


def test_finalize_partial_failure_is_reported():
    events = []
    client = RadarrClient(cfg(), event_log=lambda j, e, d: events.append((j, e, d)))
    stub = _AsyncStub()
    result = run(client.finalize(stub, 7, None, "Sintel", job_id=9))
    assert result.unmonitored is True
    # No tmdb_id supplied, so the exclusion leg was not applicable at all.
    assert result.excluded is False
    assert result.error == ""
    assert any(e[1] == "radarr.finalized" for e in events)


def test_finalize_no_ids_reports_error():
    result = run(RadarrClient(cfg()).finalize(_AsyncStub(), None, None, "Sintel"))
    assert isinstance(result, RadarrResult)
    assert result.error == "no radarr action was applicable"
    assert result.ok is False


def test_finalize_unconfigured_is_a_no_op():
    client = RadarrClient(cfg(url="", api_key=""))
    result = run(client.finalize(_AsyncStub(), 7, 1, "t"))
    assert result.error == "radarr is not configured"


def test_calls_are_actually_awaited():
    """Guards the specific regression: a non-awaited call returns a coroutine.

    If someone reverts to the blocking form, ``raise_for_status`` is invoked on a
    coroutine and this test fails rather than the failure being swallowed.
    """
    stub = _AsyncStub()
    client = RadarrClient(cfg())
    response = run(client._get(stub, "/api/v3/movie/7"))
    assert isinstance(response, dict)
    assert response["id"] == 7
    assert json.dumps(response)  # real JSON, not a coroutine repr


def test_headers_carry_the_api_key():
    class _HeaderSpy(_AsyncStub):
        def __init__(self):
            super().__init__()
            self.seen = []

        async def get(self, url, headers=None):
            self.seen.append(headers)
            return _Response(self.movie)

    spy = _HeaderSpy()
    run(RadarrClient(cfg(api_key="secret"))._get(spy, "/api/v3/movie/7"))
    assert spy.seen[0]["X-Api-Key"] == "secret"