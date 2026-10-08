from __future__ import annotations
from enum import StrEnum

class ConsumptionState(StrEnum):
    """Where one dispatched message stands in its recipient's consumption.

    The PAC workflow executor's ack-kind await reads this: a message the
    target consumed proves receipt-of-work; expiry is an observable terminal
    record, not silence (design-pac-workflow §4.3).
    """

    PENDING = "pending"      # inbox row present, unconsumed
    CONSUMED = "consumed"    # inbox row consumed, or pruned with a FETCHED record
    FAILED = "failed"        # retained row has a durable terminal failure tombstone
    EXPIRED = "expired"      # terminal record says expired (TTL eviction)
    UNKNOWN = "unknown"      # no row and no record (never seen, or purged)
