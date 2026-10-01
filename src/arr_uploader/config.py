"""Configuration loading.

Precedence: environment variables override ``config/uploader.toml``. Validation
is strict and fails fast -- a worker that starts with a half-configured
Telegram destination is worse than one that refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

MIB = 1024 * 1024

# Telegram's per-document ceilings, in MiB. MTProto user sessions get 2 GiB, or
# 4 GiB with Telegram Premium.
CEILING_STANDARD_MB = 1900
CEILING_PREMIUM_MB = 3800
# The public Bot API is far smaller; we do not support it for media uploads.
CEILING_BOT_API_MB = 49


class ConfigError(Exception):
    """Raised when configuration is missing or internally inconsistent."""


@dataclass(slots=True)
class PathsConfig:
    media_root: str = "/data/media"
    state_dir: str = "/srv/arr/state/uploader"
    inbox_dir: str = "/srv/arr/state/uploader/inbox"


@dataclass(slots=True)
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    session_string: str = ""
    session_file: str = "/srv/arr/state/uploader/uploader.session"
    chat_id: int = 0
    thread_id: int = 0
    part_ceiling_mb: str = "auto"
    verify_size: bool = True


@dataclass(slots=True)
class NamingConfig:
    template: str = "{basename}{ext}{part}"
    part_index_width: int = 3
    max_length: int = 240
    upload_subtitles: bool = True


@dataclass(slots=True)
class UploaderConfig:
    max_concurrent_jobs: int = 1
    poll_interval_seconds: int = 5
    stability_seconds: int = 30
    max_attempts: int = 8
    backoff_base_seconds: int = 30
    backoff_cap_seconds: int = 3600
    drain_on_shutdown: bool = True
    # How often a liveness heartbeat is recorded in the events table. Must stay
    # far above poll_interval_seconds: one row per poll filled the table with
    # ~17k identical heartbeats a day and buried every real event, so `status`
    # showed nothing but noise.
    heartbeat_seconds: int = 300


@dataclass(slots=True)
class TorboxConfig:
    api_key: str = ""
    base_url: str = "https://api.torbox.app/v1/api"
    timeout_seconds: int = 30
    # magnet watch folder, one magnet per file
    watch_dir: str = ""
    # how often to poll torrent state
    poll_interval_seconds: int = 60
    # mylist is cached server-side for 600s; bypass costs more of our rate budget
    bypass_cache: bool = False
    # magnet/direct .torrent files land here before being submitted
    staging_dir: str = ""
    # set true to ask TorBox to delete the torrent after we have the files
    delete_after_fetch: bool = False


@dataclass(slots=True)
class DownloadConfig:
    concurrency: int = 1


@dataclass(slots=True)
class DeletionConfig:
    enabled: bool = True
    verify_size_before_delete: bool = True
    require_stability_before_delete: bool = True
    quarantine_dir: str = "/srv/arr/state/uploader/quarantine"


@dataclass(slots=True)
class RadarrConfig:
    url: str = "http://radarr:7878"
    api_key: str = ""
    unmonitor_after_upload: bool = True
    exclude_after_upload: bool = True
    timeout_seconds: int = 15


@dataclass(slots=True)
class ReconcilerConfig:
    enabled: bool = True
    interval_seconds: int = 900
    min_age_seconds: int = 3600


@dataclass(slots=True)
class WebhookConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8099
    secret: str = ""


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    json: bool = True
    file: str = ""


@dataclass(slots=True)
class Settings:
    paths: PathsConfig = field(default_factory=PathsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    uploader: UploaderConfig = field(default_factory=UploaderConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    torbox: TorboxConfig = field(default_factory=TorboxConfig)
    deletion: DeletionConfig = field(default_factory=DeletionConfig)
    radarr: RadarrConfig = field(default_factory=RadarrConfig)
    reconciler: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> None:
        problems: list[str] = []

        if not self.telegram.api_id:
            problems.append("telegram.api_id is required (TELEGRAM_API_ID)")
        if not self.telegram.api_hash:
            problems.append("telegram.api_hash is required (TELEGRAM_API_HASH)")
        if not self.telegram.chat_id:
            problems.append("telegram.chat_id is required (TELEGRAM_CHAT_ID)")
        if not self.telegram.session_string and not self.telegram.session_file:
            problems.append("telegram needs either session_string or session_file")

        if self.naming.part_index_width < 1:
            problems.append("naming.part_index_width must be >= 1")
        if self.naming.max_length < 16:
            problems.append("naming.max_length must be >= 16")
        if "{part}" not in self.naming.template:
            problems.append("naming.template must contain {part} so parts are ordered")

        ceiling = self.telegram.part_ceiling_mb
        if isinstance(ceiling, str) and ceiling.lower() != "auto":
            if not ceiling.isdigit() or int(ceiling) < 10:
                problems.append("telegram.part_ceiling_mb must be 'auto' or an integer MiB value")

        # State must not live inside the media library, or the reconciler would
        # try to upload its own database and the worker would delete it.
        try:
            media = Path(self.paths.media_root).resolve()
            state = Path(self.paths.state_dir).resolve()
            if state == media or state.is_relative_to(media):
                problems.append(
                    f"paths.state_dir ({state}) must not be inside paths.media_root ({media})"
                )
        except OSError:
            problems.append("paths.media_root or paths.state_dir is not resolvable")

        if self.uploader.max_concurrent_jobs < 1:
            problems.append("uploader.max_concurrent_jobs must be >= 1")
        if self.uploader.stability_seconds < 0:
            problems.append("uploader.stability_seconds must be >= 0")
        if self.download.concurrency < 1:
            problems.append("download.concurrency must be >= 1")

        # A watch dir with no key would silently never submit anything, and an
        # empty watch dir with a key is just a client that is never asked to do
        # anything. Require both together so a half-configured intake fails loudly.
        if bool(self.torbox.api_key) != bool(self.torbox.watch_dir):
            problems.append(
                "torbox.api_key and torbox.watch_dir must be set together "
                f"(api_key={'set' if self.torbox.api_key else 'empty'}, "
                f"watch_dir={self.torbox.watch_dir or 'empty'})"
            )
        if self.torbox.watch_dir and Path(self.torbox.watch_dir) == Path(self.paths.media_root):
            problems.append("torbox.watch_dir must not be paths.media_root")
        if self.torbox.poll_interval_seconds < 30:
            # mylist is cached server-side for 600s; polling faster just burns
            # the 300/min budget to read stale state.
            problems.append("torbox.poll_interval_seconds must be >= 30")
        if self.webhook.enabled and not self.webhook.secret:
            problems.append("webhook.secret is required when webhook.enabled is true")

        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))


def _parse_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]

    with path.open("rb") as fh:
        return tomllib.load(fh)


def _coerce(value: str, target_type: Any) -> Any:
    if target_type is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if target_type is int:
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"expected an integer, got {value!r}") from exc
    return value


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Instantiate dataclass *cls*, ignoring unknown keys, coercing env types."""
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            continue
        target = known[key].type
        if isinstance(target, str):
            target = {"int": int, "bool": bool, "str": str}.get(target, str)
        kwargs[key] = _coerce(value, target) if isinstance(value, str) else value
    return cls(**kwargs)


def load_settings(config_path: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None) -> Settings:
    """Load TOML config, then apply environment overrides."""
    env = dict(os.environ if env is None else env)

    path = Path(config_path or env.get("UPLOADER_CONFIG", "config/uploader.toml"))
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = _parse_toml(path)
    elif config_path is not None:
        raise ConfigError(f"config file not found: {path}")

    sections = {
        "paths": PathsConfig,
        "telegram": TelegramConfig,
        "naming": NamingConfig,
        "uploader": UploaderConfig,
        "download": DownloadConfig,
        "torbox": TorboxConfig,
        "deletion": DeletionConfig,
        "radarr": RadarrConfig,
        "reconciler": ReconcilerConfig,
        "webhook": WebhookConfig,
        "logging": LoggingConfig,
    }

    built: dict[str, Any] = {}
    for name, cls in sections.items():
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"config section [{name}] must be a table")
        built[name] = _build(cls, section)

    # Environment overrides. Deliberately a small explicit allowlist rather than
    # a generic transformer, so a stray env var can never reshape the config.
    overrides: dict[str, tuple[str, dict[str, Any], str]] = {
        "TELEGRAM_API_ID": ("telegram", {"api_id": int}, "telegram.api_id"),
        "TELEGRAM_API_HASH": ("telegram", {"api_hash": str}, "telegram.api_hash"),
        "TELEGRAM_SESSION_STRING": ("telegram", {"session_string": str}, "telegram.session_string"),
        "TELEGRAM_CHAT_ID": ("telegram", {"chat_id": int}, "telegram.chat_id"),
        "TELEGRAM_THREAD_ID": ("telegram", {"thread_id": int}, "telegram.thread_id"),
        "TELEGRAM_PART_CEILING_MB": ("telegram", {"part_ceiling_mb": str}, "telegram.part_ceiling_mb"),
        "TORBOX_FETCH_DIR": ("torbox", {"staging_dir": str}, "torbox.staging_dir"),
        "TORBOX_DELETE_AFTER_FETCH": ("torbox", {"delete_after_fetch": bool}, "torbox.delete_after_fetch"),
        "MEDIA_ROOT": ("paths", {"media_root": str}, "paths.media_root"),
        "STATE_DIR": ("paths", {"state_dir": str}, "paths.state_dir"),
        "INBOX_DIR": ("paths", {"inbox_dir": str}, "paths.inbox_dir"),
        "QUARANTINE_DIR": ("deletion", {"quarantine_dir": str}, "deletion.quarantine_dir"),
        "TORBOX_API_KEY": ("torbox", {"api_key": str}, "torbox.api_key"),
        "TORBOX_BASE_URL": ("torbox", {"base_url": str}, "torbox.base_url"),
        "TORBOX_WATCH_DIR": ("torbox", {"watch_dir": str}, "torbox.watch_dir"),
        "TORBOX_STAGING_DIR": ("torbox", {"staging_dir": str}, "torbox.staging_dir"),
        "RADARR_URL": ("radarr", {"url": str}, "radarr.url"),
        "RADARR_API_KEY": ("radarr", {"api_key": str}, "radarr.api_key"),
        "WEBHOOK_SECRET": ("webhook", {"secret": str}, "webhook.secret"),
        "LOG_LEVEL": ("logging", {"level": str}, "logging.level"),
    }

    for env_key, (section, keys, label) in overrides.items():
        raw_value = env.get(env_key)
        if raw_value is None or raw_value == "":
            continue
        attr, caster = next(iter(keys.items()))
        try:
            # bool("false") is True, so booleans must go through _coerce, which
            # understands the string forms. Casting directly silently turned
            # TORBOX_DELETE_AFTER_FETCH=false into True.
            setattr(built[section], attr, _coerce(raw_value, caster) if caster is bool else caster(raw_value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{env_key} could not be applied to {label}: {exc}") from exc

    settings = Settings(**built)
    settings.validate()
    return settings


def ceiling_bytes(telegram: TelegramConfig, is_premium: bool | None) -> int:
    """Resolve the per-part byte ceiling.

    ``None`` means the tier has not been probed yet; we fall back to the
    standard ceiling, which is safe on both tiers because Premium accounts may
    also send smaller files.
    """
    raw = telegram.part_ceiling_mb
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        mib = CEILING_PREMIUM_MB if is_premium else CEILING_STANDARD_MB
    else:
        mib = int(raw)
    return mib * MIB