"""Resolving the per-part size ceiling.

The ceiling depends on the account's Telegram Premium status, which we probe once
and cache. Until the probe completes we assume the standard limit, because a
smaller part is always acceptable on both tiers -- overshooting is the only
failure that matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import MIB, CEILING_PREMIUM_MB, CEILING_STANDARD_MB, TelegramConfig, ceiling_bytes

LOG = logging.getLogger(__name__)

TIER_CACHE_KEY = "telegram_tier"
TIER_CACHE_TTL_SECONDS = 6 * 3600


@dataclass(frozen=True, slots=True)
class TierProbe:
    """Result of inspecting the logged-in account."""

    is_premium: bool | None
    source: str

    @property
    def ceiling_mb(self) -> int:
        return CEILING_PREMIUM_MB if self.is_premium else CEILING_STANDARD_MB


def _premium_from(me: object) -> bool | None:
    """Read Premium off a ``get_me()`` result.

    ``premium`` is exposed on MTProto user sessions. Bot sessions do not, which
    is why an unrecognised shape yields ``None`` rather than ``False``: we would
    rather assume the smaller ceiling than discover a 4 GiB rejection mid-upload.
    """
    for attr in ("premium", "is_premium"):
        value = getattr(me, attr, None)
        if isinstance(value, bool):
            return value
    return None


async def async_probe_account(client: object) -> TierProbe:
    """Probe Premium from an async client.

    Use this anywhere an event loop is already running. The previous synchronous
    ``probe_account`` called ``get_me()`` and then spun up a *new* event loop with
    ``run_until_complete``, which raises ``RuntimeError: Cannot run the event loop
    while another loop is running``. The worker swallowed that, so the tier was
    never recorded and Premium users were capped at the standard ceiling forever.
    """
    getter = getattr(client, "get_me", None)
    if getter is None:
        return TierProbe(is_premium=None, source="no-get_me")

    try:
        me = getter()
        if hasattr(me, "__await__"):
            me = await me
    except Exception as exc:  # noqa: BLE001 - probe must never be fatal
        LOG.warning("premium probe failed, assuming standard ceiling", extra={"error": str(exc)})
        return TierProbe(is_premium=None, source=f"error:{type(exc).__name__}")

    premium = _premium_from(me)
    if premium is None:
        return TierProbe(is_premium=None, source="attribute-absent")
    return TierProbe(is_premium=premium, source="me.premium")


def probe_account(client: object) -> TierProbe:
    """Synchronous probe, for callers with no running loop.

    A client whose ``get_me()`` returns an awaitable cannot be probed from here;
    use :func:`async_probe_account` instead rather than nesting a loop.
    """
    getter = getattr(client, "get_me", None)
    if getter is None:
        return TierProbe(is_premium=None, source="no-get_me")

    try:
        me = getter()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("premium probe failed, assuming standard ceiling", extra={"error": str(exc)})
        return TierProbe(is_premium=None, source=f"error:{type(exc).__name__}")

    if hasattr(me, "__await__"):
        me.close() if hasattr(me, "close") else None
        LOG.warning(
            "probe_account received an async client; use async_probe_account",
            extra={"hint": "call async_probe_account from inside the loop"},
        )
        return TierProbe(is_premium=None, source="async-client")

    premium = _premium_from(me)
    if premium is None:
        return TierProbe(is_premium=None, source="attribute-absent")
    return TierProbe(is_premium=premium, source="me.premium")


def cached_tier(store: object, now: float) -> bool | None:
    """Read the cached tier, or ``None`` when absent or stale."""
    raw = store.get_meta(TIER_CACHE_KEY)  # type: ignore[attr-defined]
    if not raw:
        return None
    try:
        cached_at, flag = raw.split("|", 1)
        if now - float(cached_at) > TIER_CACHE_TTL_SECONDS:
            return None
        return None if flag == "unknown" else flag == "premium"
    except (ValueError, TypeError):
        return None


def store_tier(store: object, probe: TierProbe, now: float) -> None:
    flag = {True: "premium", False: "standard", None: "unknown"}[probe.is_premium]
    store.set_meta(TIER_CACHE_KEY, f"{now:.0f}|{flag}")  # type: ignore[attr-defined]
    LOG.info("cached telegram tier", extra={"tier": flag, "source": probe.source})


def resolve_ceiling_bytes(
    telegram: TelegramConfig,
    store: object | None = None,
    client: object | None = None,
) -> int:
    """Byte ceiling for one part, for callers with no running event loop.

    Order of preference: an explicit integer in config, then a fresh account
    probe (cached), then the standard limit.

    ``client`` must expose a *synchronous* ``get_me``. For an async client use
    :func:`async_resolve_ceiling_bytes`; a sync probe cannot await one, and will
    fall back to the standard ceiling rather than probe.
    """
    if _ceiling_is_explicit(telegram):
        return ceiling_bytes(telegram, None)

    import time as _time

    is_premium = cached_tier(store, _time.time()) if store else None

    if is_premium is None and client is not None and store is not None:
        probe = probe_account(client)
        store_tier(store, probe, _time.time())
        is_premium = probe.is_premium

    LOG.debug("resolved ceiling", extra={"premium": is_premium, "bytes": ceiling_bytes(telegram, is_premium)})
    return ceiling_bytes(telegram, is_premium)


async def async_resolve_ceiling_bytes(
    telegram: TelegramConfig,
    store: object | None = None,
    client: object | None = None,
) -> int:
    """Byte ceiling for one part, usable from inside a running event loop.

    Same preference order as :func:`resolve_ceiling_bytes`, but the probe is
    awaited rather than run on a nested loop.
    """
    if _ceiling_is_explicit(telegram):
        return ceiling_bytes(telegram, None)

    import time as _time

    is_premium = cached_tier(store, _time.time()) if store else None

    if is_premium is None and client is not None and store is not None:
        probe = await async_probe_account(client)
        store_tier(store, probe, _time.time())
        is_premium = probe.is_premium

    LOG.debug("resolved ceiling", extra={"premium": is_premium, "bytes": ceiling_bytes(telegram, is_premium)})
    return ceiling_bytes(telegram, is_premium)


def _ceiling_is_explicit(telegram: TelegramConfig) -> bool:
    """True when config pins the ceiling, so no probe is needed.

    An integer is explicit by definition; only the literal ``"auto"`` defers to
    the Premium probe.
    """
    value = telegram.part_ceiling_mb
    if isinstance(value, str):
        return value.strip().lower() != "auto"
    return True


def suggested_ceiling_mb(premium: bool | None) -> int:
    """Human-readable ceiling, for logs and the CLI."""
    return CEILING_PREMIUM_MB if premium else CEILING_STANDARD_MB


CEILING_BYTES_STANDARD = CEILING_STANDARD_MB * MIB
CEILING_BYTES_PREMIUM = CEILING_PREMIUM_MB * MIB