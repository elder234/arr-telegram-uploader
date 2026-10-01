"""Logging setup.

Structured JSON by default so container log shippers can parse it, with a
human-readable console mode for interactive debugging.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import LoggingConfig

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line, with extra fields inlined."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    default_fmt = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self.default_fmt, datefmt="%Y-%m-%dT%H:%M:%S%z")

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        }
        base = super().format(record)
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} | {rendered}"
        return base


def configure_logging(config: LoggingConfig) -> None:
    level = getattr(logging, str(config.level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter = JsonFormatter() if config.json else ConsoleFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if config.file:
        Path(config.file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # These libraries are extremely chatty at DEBUG and drown out our own events.
    for noisy in ("httpx", "httpcore", "pyrogram", "kurigram", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    logging.getLogger(__name__).debug("logging configured", extra={"level": level, "json": config.json})