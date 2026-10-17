from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b

from hyprial.kernel import DEFAULT_POLICIES, EXTERNAL_IO, capped_exponential

DELIVERY_RETRY_CLAIM_LIMIT = 32

#: Recipient-offline recheck cadence for outbox and custody rows.
#: Deferral used to park an offline row at its own TTL deadline, which made
#: the row's only rescue a liveliness wake: a recipient that never produced
#: one died with ``attempts=0`` and nothing but the wake coordinator to save
#: it (PR #1463 finding 1 -- advisor->user reply, six same-shape deaths in
#: one window).  The pump now rechecks presence on this fixed short cadence;
#: the TTL contract itself is unchanged, expiry still wins when it comes
#: first, and a delivery attempt still only happens once presence says the
#: recipient is reachable.
OFFLINE_RECHECK_INTERVAL_MS = 60_000


def online_retry_next_attempt_ms(
    *,
    message_id: str,
    attempts: int,
    now_ms: int,
    expires_at_ms: int,
) -> int:
    """Return the shared, bounded online no-ACK schedule for one message.

    One stable jitter sample per message keeps the exponential sequence
    monotonic and makes both delivery authorities produce the same durable
    schedule.  Once the nominal curve reaches its ceiling, the exact ceiling
    wins: jitter must not turn a 10-second bound into a longer retry interval.
    """

    if attempts < 1:
        raise ValueError("online retry attempts must be positive")
    policy = DEFAULT_POLICIES[EXTERNAL_IO]
    nominal = capped_exponential(policy.base_backoff, policy.max_backoff, attempts - 1)
    digest = blake2b(message_id.encode("utf-8"), digest_size=8).digest()
    sample = int.from_bytes(digest, "big") / ((1 << 64) - 1)
    if nominal < policy.max_backoff:
        nominal *= 1 + policy.jitter_ratio * ((2 * sample) - 1)
    else:
        # Never exceed the policy ceiling, but keep saturated rows from
        # remaining in lockstep after a restart.
        nominal *= 1 - policy.jitter_ratio * sample
    delay_ms = max(1, round(min(policy.max_backoff, nominal) * 1_000))
    return min(expires_at_ms, now_ms + delay_ms)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retired injection token; PR 3 removes it with legacy persisted fields."""
