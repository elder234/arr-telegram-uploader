"""TorBox configuration tests.

Intake is opt-in: a fresh clone with no TorBox account must load cleanly, and a
half-configured intake must be rejected rather than silently doing nothing.
"""

from __future__ import annotations

from pathlib import Path

from arr_uploader.config import (
    ConfigError,
    PathsConfig,
    Settings,
    TelegramConfig,
    TorboxConfig,
    load_settings,
)

REPO = Path(__file__).resolve().parent.parent

TELEGRAM_ENV = {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "h",
    "TELEGRAM_CHAT_ID": "1",
}


def _settings() -> Settings:
    """Minimal valid settings, with no filesystem assumptions."""
    return Settings(
        telegram=TelegramConfig(api_id=1, api_hash="h", chat_id=1),
        paths=PathsConfig(media_root="/tmp/m", state_dir="/tmp/s"),
    )


def test_shipped_config_loads_with_no_torbox_account():
    """A fresh clone must not require TorBox to be configured."""
    shipped = REPO / "config" / "uploader.toml"
    assert shipped.is_file(), "config/uploader.toml is missing from the repo"

    settings = load_settings(shipped, env=dict(TELEGRAM_ENV))
    # Both halves empty: intake is off, and that is a valid state.
    assert settings.torbox.api_key == ""
    assert settings.torbox.watch_dir == ""


def test_intake_off_by_default_in_the_dataclass():
    assert TorboxConfig().api_key == ""
    assert TorboxConfig().watch_dir == ""


def test_api_key_without_watch_dir_is_rejected():
    settings = _settings()
    settings.torbox.api_key = "k"
    # A key with no watch dir submits nothing forever, which reads as broken.
    try:
        settings.validate()
    except ConfigError as exc:
        assert "must be set together" in str(exc)
    else:
        raise AssertionError("expected a key without a watch dir to be rejected")


def test_watch_dir_without_api_key_is_rejected():
    settings = _settings()
    settings.torbox.watch_dir = "/tmp/magnets"
    try:
        settings.validate()
    except ConfigError as exc:
        assert "must be set together" in str(exc)
    else:
        raise AssertionError("expected a watch dir without a key to be rejected")


def test_both_halves_set_is_accepted():
    settings = _settings()
    settings.torbox.api_key = "k"
    settings.torbox.watch_dir = "/tmp/magnets"
    settings.validate()


def test_watch_dir_cannot_be_the_media_root():
    # Magnets dropped into the library would be scanned as a movie folder.
    settings = _settings()
    settings.torbox.api_key = "k"
    settings.torbox.watch_dir = settings.paths.media_root
    try:
        settings.validate()
    except ConfigError as exc:
        assert "media_root" in str(exc)
    else:
        raise AssertionError("expected watch_dir == media_root to be rejected")


def test_poll_interval_has_a_floor():
    # mylist is cached server-side for 600s; polling faster burns rate budget to
    # read state that has not changed.
    assert TorboxConfig().poll_interval_seconds >= 30

    settings = _settings()
    settings.torbox.api_key = "k"
    settings.torbox.watch_dir = "/tmp/magnets"
    settings.torbox.poll_interval_seconds = 5
    try:
        settings.validate()
    except ConfigError as exc:
        assert "poll_interval_seconds" in str(exc)
    else:
        raise AssertionError("expected a sub-30s poll interval to be rejected")


def test_env_overrides_reach_the_torbox_config():
    settings = load_settings(
        REPO / "config" / "uploader.toml",
        env={
            **TELEGRAM_ENV,
            "TORBOX_API_KEY": "from-env",
            "TORBOX_WATCH_DIR": "/tmp/magnets",
        },
    )
    assert settings.torbox.api_key == "from-env"
    assert settings.torbox.watch_dir == "/tmp/magnets"


def test_fetch_dir_env_override():
    settings = load_settings(
        REPO / "config" / "uploader.toml",
        env={**TELEGRAM_ENV, "TORBOX_FETCH_DIR": "/tmp/fetched"},
    )
    assert settings.torbox.staging_dir == "/tmp/fetched"


def test_delete_after_fetch_defaults_off():
    # Deleting the torrent from TorBox frees server space but is irreversible,
    # so it must be an explicit choice.
    assert TorboxConfig().delete_after_fetch is False


def test_delete_after_fetch_env_accepts_boolean_words():
    """``bool("false")`` is ``True``.

    The override path used to cast strings with ``bool`` directly, so
    ``TORBOX_DELETE_AFTER_FETCH=false`` enabled deletion. That is the dangerous
    direction for an irreversible flag, so every string form is pinned here.
    """
    truthy = ["true", "1", "yes", "on", "TRUE"]
    falsy = ["false", "0", "no", "off", "FALSE"]
    for value in truthy:
        settings = load_settings(REPO / "config" / "uploader.toml", env={**TELEGRAM_ENV, "TORBOX_DELETE_AFTER_FETCH": value})
        assert settings.torbox.delete_after_fetch is True, value
    for value in falsy:
        settings = load_settings(REPO / "config" / "uploader.toml", env={**TELEGRAM_ENV, "TORBOX_DELETE_AFTER_FETCH": value})
        assert settings.torbox.delete_after_fetch is False, value


def test_env_example_documents_only_real_env_vars():
    """Every key in .env.example must actually be read.

    .env.example is the first thing a user copies. A key listed there that no
    code reads is a promise the code does not keep.
    """
    from arr_uploader.config import load_settings as _load

    config_source = (REPO / "src" / "arr_uploader" / "config.py").read_text(encoding="utf-8")
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    documented = [
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    ]
    assert documented, ".env.example lists no variables"
    for key in documented:
        assert f'"{key}"' in config_source, f"{key} is in .env.example but nothing reads it"