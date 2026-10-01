"""Tests for configuration loading and the size ceiling.

Validation is the last line of defence before a worker starts doing destructive
work, so these tests focus on what must be rejected.
"""

from __future__ import annotations

from arr_uploader.config import (
    CEILING_PREMIUM_MB,
    CEILING_STANDARD_MB,
    MIB,
    ConfigError,
    TelegramConfig,
    ceiling_bytes,
    load_settings,
)

import pytest

MINIMAL = """
[telegram]
api_id = 12345
api_hash = "abc123"
chat_id = -1001234567890
session_file = "/tmp/u.session"
"""


def write_config(tmp_path, body: str = MINIMAL):
    path = tmp_path / "uploader.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_minimal_config(tmp_path):
    settings = load_settings(write_config(tmp_path))
    assert settings.telegram.api_id == 12345
    assert settings.telegram.chat_id == -1001234567890


def test_missing_file_is_an_error_when_explicit(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path / "nope.toml")
    assert "not found" in str(exc.value)


def test_defaults_applied_for_absent_sections(tmp_path):
    settings = load_settings(write_config(tmp_path))
    assert settings.uploader.max_concurrent_jobs == 1
    assert settings.deletion.enabled is True
    assert settings.naming.template == "{basename}{ext}{part}"


def test_missing_api_id_rejected(tmp_path):
    body = MINIMAL.replace("api_id = 12345", "")
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path, body))
    assert "api_id" in str(exc.value)


def test_missing_chat_id_rejected(tmp_path):
    body = MINIMAL.replace("chat_id = -1001234567890", "")
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path, body))
    assert "chat_id" in str(exc.value)


def test_template_without_part_token_rejected(tmp_path):
    """Without {part} every part would share a name and could not be ordered."""
    body = MINIMAL + '\n[naming]\ntemplate = "{basename}"\n'
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path, body))
    assert "{part}" in str(exc.value)


def test_state_dir_inside_media_root_rejected(tmp_path):
    """State inside the library would make the reconciler upload our own
    database, and the deletion gate destroy it."""
    body = MINIMAL + '\n[paths]\nmedia_root = "/data/media"\nstate_dir = "/data/media/state"\n'
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path, body))
    assert "must not be inside" in str(exc.value)


def test_webhook_requires_secret(tmp_path):
    body = MINIMAL + "\n[webhook]\nenabled = true\n"
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path, body))
    assert "webhook.secret" in str(exc.value)


def test_env_overrides_toml(tmp_path):
    settings = load_settings(
        write_config(tmp_path),
        env={"TELEGRAM_CHAT_ID": "-100999", "MEDIA_ROOT": "/mnt/media"},
    )
    assert settings.telegram.chat_id == -100999
    assert settings.paths.media_root == "/mnt/media"


def test_invalid_env_int_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path), env={"TELEGRAM_CHAT_ID": "not-a-number"})
    assert "TELEGRAM_CHAT_ID" in str(exc.value)


def test_booleans_parsed(tmp_path):
    body = MINIMAL + "\n[deletion]\nenabled = false\n"
    settings = load_settings(write_config(tmp_path, body))
    assert settings.deletion.enabled is False


def test_all_errors_reported_together(tmp_path):
    """One run should list every problem, not just the first."""
    body = MINIMAL.replace("api_id = 12345", "").replace("chat_id = -1001234567890", "")
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(tmp_path, body))
    text = str(exc.value)
    assert "api_id" in text
    assert "chat_id" in text


# ------------------------------------------------------------------- ceiling


def test_ceiling_auto_uses_standard_tier():
    telegram = TelegramConfig(part_ceiling_mb="auto")
    assert ceiling_bytes(telegram, False) == CEILING_STANDARD_MB * MIB


def test_ceiling_auto_uses_premium_tier():
    telegram = TelegramConfig(part_ceiling_mb="auto")
    assert ceiling_bytes(telegram, True) == CEILING_PREMIUM_MB * MIB


def test_ceiling_unknown_tier_falls_back_to_standard():
    """An unprobed account must assume the smaller limit."""
    telegram = TelegramConfig(part_ceiling_mb="auto")
    assert ceiling_bytes(telegram, None) == CEILING_STANDARD_MB * MIB


def test_explicit_ceiling_overrides_tier():
    telegram = TelegramConfig(part_ceiling_mb=500)
    assert ceiling_bytes(telegram, True) == 500 * MIB
    assert ceiling_bytes(telegram, False) == 500 * MIB


def test_premium_ceiling_is_double_standard():
    assert CEILING_PREMIUM_MB == CEILING_STANDARD_MB * 2