"""The WP2 link service package (spec §9.5); import path unchanged.

``from hyprial.daemon.impl.links.service import LinkService`` keeps working.
"""

from __future__ import annotations

from ._base import (
    ACTIVE_WINDOW_MS,
    RESERVATION_TTL_MS,
    SAMPLE_CAP,
    TICK_INTERVAL_MS,
    TTL_EXPIRED,
)
from .core import LinkService

__all__ = [
    "ACTIVE_WINDOW_MS",
    "RESERVATION_TTL_MS",
    "SAMPLE_CAP",
    "TICK_INTERVAL_MS",
    "TTL_EXPIRED",
    "LinkService",
]
