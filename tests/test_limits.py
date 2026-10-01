"""Tests for the Telegram ceiling and Premium probe.

Two defects are covered here:

* ``probe_account`` called ``get_me()`` and then spun up a *new* event loop to
  await the result. Inside the worker's running loop that raises ``RuntimeError:
  Cannot run the event loop while another loop is running``, which the caller
  swallowed -- so the tier was never recorded and every account, Premium or not,
  used the standard ceiling.
* The tier cache stored a monotonic timestamp while comparing against ``now=0``,
  so a stale entry never expired.
"""

from __future__ import annotations

import asyncio
import time

from arr_uploader.config import TelegramConfig, ceiling_bytes
from arr_uploader.telegram.limits import (
    TIER_CACHE_TTL_SECONDS,
    TierProbe,
    async_probe_account,
    async_resolve_ceiling_bytes,
    cached_tier,
    probe_account,
    resolve_ceiling_bytes,
    store_tier,
    suggested_ceiling_mb,
)


class _MetaStore:
    """Minimal get_meta/set_meta surface for the tier cache."""

    def __init__(self):
        self.data = {}
        self.reads = 0

    def get_meta(self, k):
        self.reads += 1
        return self.data.get(k)

    def set_meta(self, k, v):
        self.data[k] = v


def run(coro):
    return asyncio.run(coro)


class _Me:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


class _AsyncClient:
    def __init__(self, me):
        self._me = me
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        return self._me


class _SyncClient:
    def __init__(self, me):
        self._me = me

    def get_me(self):
        return self._me


class _RaisingClient:
    async def get_me(self):
        raise RuntimeError("network down")


def test_async_probe_reads_premium_true():
    probe = run(async_probe_account(_AsyncClient(_Me(premium=True))))
    assert probe.is_premium is True
    assert probe.ceiling_mb == 3800


def test_async_probe_reads_premium_false():
    probe = run(async_probe_account(_AsyncClient(_Me(premium=False))))
    assert probe.is_premium is False
    assert probe.ceiling_mb == 1900


def test_async_probe_inside_a_running_loop():
    """The exact shape the worker uses: a loop is already running.

    The old sync probe raised RuntimeError here and the worker swallowed it, so
    this test fails loudly if that comes back.
    """

    async def main():
        probe = await async_probe_account(_AsyncClient(_Me(premium=True)))
        assert probe.is_premium is True
        return probe

    assert asyncio.run(main()).ceiling_mb == 3800


def test_async_probe_is_preferred_on_the_client():
    """MTProto user sessions expose ``premium``."""
    probe = run(async_probe_account(_AsyncClient(_Me(premium=True))))
    assert probe.source == "me.premium"


def test_async_probe_missing_attribute_is_unknown():
    """Unknown is not False: assuming Premium would invite a 4 GiB rejection."""
    probe = run(async_probe_account(_AsyncClient(_Me(first_name="x"))))
    assert probe.is_premium is None
    assert probe.ceiling_mb == 1900


def test_async_probe_no_get_me():
    probe = run(async_probe_account(object()))
    assert probe.is_premium is None
    assert probe.source == "no-get_me"


def test_async_probe_error_is_swallowed():
    probe = run(async_probe_account(_RaisingClient()))
    assert probe.is_premium is None
    assert probe.source.startswith("error:")


def test_sync_probe_still_works_for_sync_clients():
    probe = probe_account(_SyncClient(_Me(premium=True)))
    assert probe.is_premium is True


def test_sync_probe_refuses_an_async_client():
    """It must not nest an event loop; it reports the situation instead."""
    client = _AsyncClient(_Me(premium=True))
    probe = probe_account(client)
    assert probe.is_premium is None
    assert probe.source == "async-client"
    # get_me() was invoked but never awaited, which is the whole point: the old
    # version tried to await it on a second loop and raised RuntimeError.
    assert client.calls == 0, "the coroutine must be discarded, not awaited"


def test_tier_cache_roundtrip():
    class _Store:
        def __init__(self):
            self.data = {}

        def get_meta(self, k):
            return self.data.get(k)

        def set_meta(self, k, v):
            self.data[k] = v

    store = _Store()
    now = time.time()
    store_tier(store, TierProbe(is_premium=True, source="me.premium"), now)
    assert cached_tier(store, now) is True


def test_tier_cache_expires():
    class _Store:
        def __init__(self):
            self.data = {}

        def get_meta(self, k):
            return self.data.get(k)

        def set_meta(self, k, v):
            self.data[k] = v

    store = _Store()
    now = time.time()
    store_tier(store, TierProbe(is_premium=True, source="me.premium"), now)

    stale = now + TIER_CACHE_TTL_SECONDS + 60
    assert cached_tier(store, stale) is None, "a stale tier must not be reused"


def test_resolve_ceiling_caches_and_then_expires():
    """End-to-end on the clock contract that mattered.

    store_tier writes wall-clock, so resolve_ceiling_bytes must read with
    wall-clock too. The old code passed a literal ``0.0``: the difference
    ``0.0 - cached_at`` is hugely negative, the TTL never tripped, and a stale
    tier was reused indefinitely. The probe below also confirms the cache is
    actually consulted rather than re-probing on every job.
    """
    store = _MetaStore()
    client = _AsyncClient(_Me(premium=True))
    cfg = TelegramConfig(part_ceiling_mb="auto", api_id=1, api_hash="h", chat_id=1)

    assert run(async_resolve_ceiling_bytes(cfg, store, client)) == 3800 * 1024 * 1024
    assert client.calls == 1, "first call probes and caches"

    assert run(async_resolve_ceiling_bytes(cfg, store, client)) == 3800 * 1024 * 1024
    assert client.calls == 1, "a fresh cache entry must not re-probe"

    # Age the entry past the TTL and confirm it is re-probed.
    stored_at = float(store.data["telegram_tier"].split("|", 1)[0])
    store.data["telegram_tier"] = (
        f"{stored_at - TIER_CACHE_TTL_SECONDS - 60:.0f}|premium"
    )
    assert run(async_resolve_ceiling_bytes(cfg, store, client)) == 3800 * 1024 * 1024
    assert client.calls == 2, "a stale entry must be re-probed, not reused"


def test_sync_resolve_ceiling_uses_a_sync_client():
    store = _MetaStore()
    client = _SyncClient(_Me(premium=True))
    cfg = TelegramConfig(part_ceiling_mb="auto", api_id=1, api_hash="h", chat_id=1)

    assert resolve_ceiling_bytes(cfg, store, client) == 3800 * 1024 * 1024


def test_sync_resolve_ceiling_falls_back_for_an_async_client():
    """A sync probe cannot await, so it must not claim Premium it never saw."""
    store = _MetaStore()
    client = _AsyncClient(_Me(premium=True))
    cfg = TelegramConfig(part_ceiling_mb="auto", api_id=1, api_hash="h", chat_id=1)

    assert resolve_ceiling_bytes(cfg, store, client) == 1900 * 1024 * 1024


def test_explicit_ceiling_skips_probing_entirely():
    store = _MetaStore()
    client = _AsyncClient(_Me(premium=True))
    cfg = TelegramConfig(part_ceiling_mb=500, api_id=1, api_hash="h", chat_id=1)

    assert run(async_resolve_ceiling_bytes(cfg, store, client)) == 500 * 1024 * 1024
    assert client.calls == 0, "an explicit ceiling must not touch the network"


def test_unknown_tier_is_cached_as_none():
    class _Store:
        def __init__(self):
            self.data = {}

        def get_meta(self, k):
            return self.data.get(k)

        def set_meta(self, k, v):
            self.data[k] = v

    store = _Store()
    now = time.time()
    store_tier(store, TierProbe(is_premium=None, source="attribute-absent"), now)
    assert cached_tier(store, now) is None


def test_ceiling_defaults_to_standard():
    cfg = TelegramConfig(part_ceiling_mb="auto", api_id=1, api_hash="h", chat_id=1)
    assert ceiling_bytes(cfg, is_premium=False) == 1900 * 1024 * 1024
    assert ceiling_bytes(cfg, is_premium=None) == 1900 * 1024 * 1024
    assert ceiling_bytes(cfg, is_premium=True) == 3800 * 1024 * 1024


def test_explicit_ceiling_overrides_the_probe():
    cfg = TelegramConfig(part_ceiling_mb=500, api_id=1, api_hash="h", chat_id=1)
    assert ceiling_bytes(cfg, is_premium=True) == 500 * 1024 * 1024


def test_suggested_ceiling_matches_probe():
    assert suggested_ceiling_mb(True) == 3800
    assert suggested_ceiling_mb(False) == 1900
    assert suggested_ceiling_mb(None) == 1900, "unknown must not advertise Premium"