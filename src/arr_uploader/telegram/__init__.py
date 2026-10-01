"""Telegram transport: MTProto client, ceiling resolution, upload, verification.

Retry and flood behaviour follows the patterns that work in production for
mirror-leech (FloodWait with 30% headroom, BadRequest falling back to document
upload) but is written here against our own :mod:`.verify` gate: a part is not
recorded as done until its size is confirmed.
"""

from .limits import TierProbe, async_resolve_ceiling_bytes, resolve_ceiling_bytes

__all__ = ["TierProbe", "async_resolve_ceiling_bytes", "resolve_ceiling_bytes"]