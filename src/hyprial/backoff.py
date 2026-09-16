"""Bounded exponential backoff arithmetic shared by retry loops.

The counters that feed these curves are persisted or otherwise long-lived and
have no upper bound (``codex`` settlement attempts are the reachable one; the
harness failure counter is reachable when the failure budget is disabled), so
evaluating ``base * 2 ** (n - 1)`` directly raises ``OverflowError`` once ``n``
passes ~1024 -- before the outer ``min(cap, ...)`` can discard the product.
The retry loops only ever want the capped value, so the exponent is clamped to
the smallest one that already reaches the cap and the multiplication never
leaves the finite range.  Small exponents are untouched.

This is a dependency-free leaf on purpose: actor runtime, harness streaming,
the harness actor, usage polling, and (through history) tooling all share one
arithmetic contract instead of each re-deriving a clamp.
"""

from __future__ import annotations

import math


def capped_exponential(base: float, cap: float, exponent: int) -> float:
    """Return ``min(cap, base * 2 ** exponent)`` without overflowing.

    ``base`` must be non-negative.  ``exponent`` may be negative, in which case
    the result is the plain ``base * 2 ** exponent`` (always finite).  For large
    ``exponent`` the exponent is clamped before the multiply, so the result is
    exactly ``cap`` for every exponent that would have overflowed.
    """

    if exponent <= 0:
        return min(cap, base * (2.0 ** exponent))
    if base <= 0.0:
        return min(cap, 0.0)
    if cap <= 0.0:
        return cap
    if math.isinf(cap):
        limit = exponent
    else:
        # Smallest exponent whose product reaches the cap, computed from the
        # two logarithms separately so a subnormal base against a large cap
        # cannot overflow the ratio; +1 keeps float rounding from under-clamping.
        limit = max(0, math.ceil(math.log2(cap) - math.log2(base))) + 1
    try:
        # ldexp is base * 2 ** exponent for every representable result, and
        # unlike ``2.0 ** exponent`` it does not overflow before the multiply.
        product = math.ldexp(base, min(exponent, limit))
    except OverflowError:
        # The only overflowing products are already past any finite cap.
        return cap
    return min(cap, product)


__all__ = ["capped_exponential"]
