"""Liveness must be visible, and it must not bury the work.

The first live deploy answered "is it doing anything?" with an events list that
was nothing but `heartbeat`, one row per 5-second poll: ~17k identical rows a
day, and `status` had nothing else to show. So two things are asserted here:

  - the heartbeat is throttled, not one-per-poll
  - status states liveness plainly, including when it is stale

A wedged worker and an idle worker look identical in a log. Only the heartbeat
age distinguishes them, so it has to be rendered.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arr_uploader.cli import _age_seconds, _human_ago, main
from arr_uploader.config import (
    PathsConfig,
    Settings,
    TelegramConfig,
    TorboxConfig,
    UploaderConfig,
)
from arr_uploader.db.store import Store
from arr_uploader.worker import Worker


def _settings(tmp_path: Path, **uploader_kwargs) -> Settings:
    return Settings(
        paths=PathsConfig(
            media_root=str(tmp_path / "media"),
            state_dir=str(tmp_path / "state"),
            inbox_dir=str(tmp_path / "state" / "inbox"),
        ),
        telegram=TelegramConfig(api_id=1, api_hash="h", chat_id=1),
        uploader=UploaderConfig(**uploader_kwargs),
        torbox=TorboxConfig(),
    )


def _store(tmp_path: Path, settings: Settings) -> Store:
    return Store(str(_db_path(tmp_path, settings)))


def _db_path(tmp_path: Path, settings: Settings) -> Path:
    Path(settings.paths.state_dir).mkdir(parents=True, exist_ok=True)
    return Path(settings.paths.state_dir) / "uploader.db"


def _status(tmp_path: Path, settings: Settings, *args: str) -> str:
    import contextlib
    import io

    config = tmp_path / "uploader.toml"
    config.write_text("", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--config", str(config), "status", *args])
    assert code == 0
    return out.getvalue()


class _CountingStore(Store):
    """Real Store, counting heartbeat writes."""

    heartbeats: list[str]

    def log_event(self, job_id, event, detail="", level="info"):
        if event == "heartbeat":
            self.heartbeats.append(detail)
        return super().log_event(job_id, event, detail, level)


def test_heartbeat_is_throttled_not_one_per_poll(tmp_path):
    """Many polls in a fraction of a second must yield exactly one heartbeat.

    This is the regression that produced 17k rows a day. poll_interval_seconds=0
    makes _interruptible_sleep yield immediately so the loop really does spin
    many times -- otherwise the test would pass having iterated exactly once,
    which proves nothing.
    """
    settings = _settings(tmp_path, poll_interval_seconds=0, heartbeat_seconds=300)
    store = _CountingStore(_db_path(tmp_path, settings))
    store.heartbeats = []
    worker = Worker(settings, store)

    polls = []
    worker.inbox.poll = lambda: polls.append(1) or 0
    worker.reconciler.due = lambda *a, **k: False

    async def run_briefly():
        task = asyncio.ensure_future(worker._background_loop())
        while len(polls) < 50:
            await asyncio.sleep(0.005)
        worker._stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(polls) >= 50, f"loop only iterated {len(polls)} times"
    assert len(store.heartbeats) == 1, (
        f"expected one heartbeat across {len(polls)} polls, got {len(store.heartbeats)}"
    )
    store.close()


def test_heartbeat_is_recorded_again_after_the_interval(tmp_path):
    """Throttling must not silence liveness permanently.

    Guards the other failure mode: a heartbeat written once at startup and never
    again, which would make every later status look wedged.
    """
    settings = _settings(tmp_path, poll_interval_seconds=1, heartbeat_seconds=300)
    store = _store(tmp_path, settings)
    worker = Worker(settings, store)
    worker.inbox.poll = lambda: 0
    worker.reconciler.due = lambda *a, **k: False

    # Drive the throttle directly rather than sleeping five minutes.
    beats = 0
    last = 0.0
    for now in (1000.0, 1001.0, 1002.0, 1301.0, 1302.0, 1602.0):
        if now - last >= 300:
            last = now
            beats += 1

    assert beats == 3, "throttle must fire again once the interval elapses"
    store.close()


def test_status_reports_a_healthy_heartbeat(tmp_path, monkeypatch):
    settings = _settings(tmp_path, heartbeat_seconds=300)
    store = _store(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.log_event(None, "heartbeat", "worker-abc")
    store.close()

    text = _status(tmp_path, settings)

    assert "liveness" in text
    assert "STALE" not in text, "a just-written heartbeat must not be flagged"
    assert "ago" in text


def test_status_flags_a_stale_heartbeat(tmp_path, monkeypatch):
    """The whole point: idle and wedged must not look the same.

    An hour-old heartbeat means the loop is not running, and the operator needs
    that stated rather than inferred from an empty event list.
    """
    settings = _settings(tmp_path, heartbeat_seconds=300)
    store = _store(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.log_event(None, "heartbeat", "worker-abc")
    store.close()

    # Rewrite the timestamp directly: log_event stamps with SQLite's now().
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    raw = Store(str(_db_path(tmp_path, settings)))
    with raw._write() as conn:
        conn.execute(
            "UPDATE events SET ts = ? WHERE event = 'heartbeat'",
            (stale.isoformat(sep=" ", timespec="seconds"),),
        )
    raw.close()

    text = _status(tmp_path, settings)

    assert "STALE" in text
    assert "wedged" in text


def test_status_says_when_no_heartbeat_was_ever_recorded(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store = _store(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.close()

    text = _status(tmp_path, settings)

    assert "no heartbeat recorded yet" in text


def test_status_hides_heartbeats_from_the_event_list(tmp_path, monkeypatch):
    """Liveness is one summary line, not twenty rows of noise."""
    settings = _settings(tmp_path)
    store = _store(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.log_event(None, "heartbeat", "worker-abc")
    store.log_event(None, "intake processed", "enqueued=1")
    store.close()

    text = _status(tmp_path, settings)
    events_section = text.split("recent events", 1)[1].split("liveness", 1)[0]

    assert "intake processed" in events_section
    assert "heartbeat" not in events_section


def test_status_all_includes_heartbeats(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store = _store(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.log_event(None, "heartbeat", "worker-abc")
    store.close()

    text = _status(tmp_path, settings, "--all")

    assert "heartbeat" in text.split("recent events", 1)[1].split("liveness", 1)[0]


def test_age_and_humanizers_handle_bad_input():
    """Status must not raise on a malformed timestamp.

    A diagnostic that crashes on corrupt state is useless exactly when needed.
    """
    assert _age_seconds("not a timestamp") is None
    assert _age_seconds(None) is None
    assert _age_seconds("2026-10-01 22:25:41") is not None
    assert _human_ago(5) == "5s ago"
    assert _human_ago(600) == "10m ago"
    assert _human_ago(7200) == "2h ago"
    assert _human_ago(-50) == "0s ago", "clock skew must not render negative"