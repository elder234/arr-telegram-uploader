"""`arr-uploader status` is the operator's window into the pipeline.

Pointed out after a deploy where the only way to see anything was
`docker logs -f` and the only way to learn state was the fact that a port was
not open. Neither answers "what is my worker actually doing right now", which is
the only question that matters when nothing appears to be happening.

A quiet log is ambiguous between working, stalled, and waiting. These assert the
command renders each of those states distinguishably, using a real Store rather
than a fake so the SQL and the Job/Part mapping are genuinely exercised.
"""

import json
from pathlib import Path

from arr_uploader.cli import main
from arr_uploader.config import PathsConfig, Settings, TelegramConfig, TorboxConfig
from arr_uploader.db.models import JobState, PartState
from arr_uploader.db.store import Store


def _config(tmp_path: Path, **torbox_kwargs) -> Settings:
    return Settings(
        paths=PathsConfig(
            media_root=str(tmp_path / "media"),
            state_dir=str(tmp_path / "state"),
            inbox_dir=str(tmp_path / "state" / "inbox"),
        ),
        telegram=TelegramConfig(api_id=1, api_hash="h", chat_id=1),
        torbox=TorboxConfig(**torbox_kwargs),
    )


def _write(tmp_path: Path, settings: Settings) -> Store:
    Path(settings.paths.state_dir).mkdir(parents=True, exist_ok=True)
    store = Store(str(Path(settings.paths.state_dir) / "uploader.db"))
    return store


def _run(tmp_path: Path, settings: Settings, *args: str) -> str:
    import contextlib
    import io

    config = tmp_path / "uploader.toml"
    config.write_text("", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--config", str(config), *args])
    assert code == 0, f"status exited {code}"
    return out.getvalue()


def test_status_renders_on_a_fresh_install(tmp_path, monkeypatch):
    """The empty case must be readable, not blank.

    "no jobs yet" is the first thing a new operator should see. Silence would be
    indistinguishable from a crash.
    """
    settings = _config(tmp_path)
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.close()

    text = _run(tmp_path, settings, "status")

    assert "uploader status" in text
    assert "no jobs yet" in text
    assert "nothing yet" in text


def test_status_shows_part_progress_and_size(tmp_path, monkeypatch):
    settings = _config(tmp_path)
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)

    job_id = store.upsert_job(str(tmp_path / "media" / "Some Movie (2024)"), title="Some Movie", year=2024)
    store.set_partition(job_id, 5 * 1024**3, 1900 * 1024**2, 3)
    store.replace_parts(job_id, [])
    store.set_state(job_id, JobState.UPLOADING)
    store.close()

    text = _run(tmp_path, settings, "status")

    assert "Some Movie" in text
    assert "uploading" in text
    # 5 GiB rendered readably rather than as a raw integer.
    assert "5.0 GiB" in text
    assert "not partitioned yet" in text or "/3 parts" in text


def test_status_surfaces_last_error_for_failed_jobs(tmp_path, monkeypatch):
    """A failed job with no visible reason is the worst case to debug.

    This is the whole reason the command lists last_error inline.
    """
    settings = _config(tmp_path)
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)

    job_id = store.upsert_job(str(tmp_path / "media" / "Broken Movie"), title="Broken Movie")
    store.set_state(job_id, JobState.FAILED, error="FloodWait: 3600s")
    store.close()

    text = _run(tmp_path, settings, "status")

    assert "failed" in text
    assert "FloodWait: 3600s" in text


def test_status_reports_torbox_intake_state(tmp_path, monkeypatch):
    """Intake must be visible without reading logs.

    A key that is set but a watch dir that is missing is exactly the
    misconfiguration that produced a crash loop earlier; status should show it.
    """
    watch = tmp_path / "magnets"
    watch.mkdir()
    staging = tmp_path / "state" / "fetch"
    staging.mkdir(parents=True)

    settings = _config(tmp_path, api_key="k" * 40, watch_dir=str(watch), staging_dir=str(staging))
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)

    (watch / "pending-one.magnet").write_text("magnet:?xt=urn:btih:" + "a" * 40, encoding="utf-8")
    (staging / ".torbox-journal.json").write_text(
        json.dumps({"torrents": {"1": {"state": "submitted"}, "2": {"state": "fetched"}}}),
        encoding="utf-8",
    )
    store.close()

    text = _run(tmp_path, settings, "status")

    assert "torbox intake : enabled" in text
    assert "magnets waiting: 1" in text
    assert "2 torrents, 1 awaiting fetch" in text


def test_status_reports_torbox_disabled(tmp_path, monkeypatch):
    settings = _config(tmp_path)
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.close()

    text = _run(tmp_path, settings, "status")

    assert "torbox intake : disabled" in text


def test_status_survives_a_corrupt_journal(tmp_path, monkeypatch):
    """Status must not crash on bad state; it is a diagnostic tool.

    It has to work precisely when something else is broken.
    """
    watch = tmp_path / "magnets"
    watch.mkdir()
    staging = tmp_path / "state" / "fetch"
    staging.mkdir(parents=True)
    (staging / ".torbox-journal.json").write_text("{not json", encoding="utf-8")

    settings = _config(tmp_path, api_key="k" * 40, watch_dir=str(watch), staging_dir=str(staging))
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.close()

    text = _run(tmp_path, settings, "status")

    assert "journal     : unreadable" in text


def test_status_json_keeps_the_old_payload(tmp_path, monkeypatch):
    """Scripts may depend on the machine-readable shape; do not break it."""
    settings = _config(tmp_path)
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)
    store.close()

    text = _run(tmp_path, settings, "status", "--json")

    payload = json.loads(text)
    assert "states" in payload
    assert "meta" in payload


def test_status_limit_bounds_output(tmp_path, monkeypatch):
    settings = _config(tmp_path)
    store = _write(tmp_path, settings)
    monkeypatch.setattr("arr_uploader.cli.load_settings", lambda *a, **k: settings)

    for i in range(5):
        store.upsert_job(str(tmp_path / "media" / f"Movie {i}"), title=f"Movie {i}")
    store.close()

    text = _run(tmp_path, settings, "status", "--limit", "2")

    assert text.count("  #") <= 2, "limit was not applied"
    assert "and 3 more" in text