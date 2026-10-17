"""Low-rate forwarding liveness for OrgFS announce attribution."""

from __future__ import annotations

import time
from typing import Any


_FORWARDING_TIMER_LIVENESS_INTERVAL_S = 60.0


def _emit(owner: Any, component: str, event: str, **fields: object) -> None:
    try:
        owner._log("info", component, event, **fields)
    except Exception:
        return


def note_forwarding_timer_tick(owner: Any) -> None:
    """Emit a low-rate beat without owning or changing forwarding state."""

    if not getattr(owner, "_trace_presence_enabled", False):
        return
    ticks = getattr(owner, "_forwarding_timer_ticks", 0) + 1
    owner._forwarding_timer_ticks = ticks
    now = time.monotonic()
    last = getattr(owner, "_forwarding_timer_liveness_at", None)
    if last is not None and now - last < _FORWARDING_TIMER_LIVENESS_INTERVAL_S:
        return
    owner._forwarding_timer_liveness_at = now
    _emit(
        owner,
        "zenoh",
        "forwarding.timer.live",
        ticks=ticks,
        intervalMs=None if last is None else int((now - last) * 1000),
    )
    state = getattr(owner, "_org_dial_state", None)
    answers = getattr(state, "answers", None) if state is not None else None
    if not answers:
        return
    for label, answer in sorted(answers.items()):
        direction, reason = answer if isinstance(answer, tuple) else (answer, None)
        _emit(
            owner,
            "org",
            "org.dial_direction.resolved",
            peer=label,
            direction=direction,
            reason=reason,
            steadyState=True,
        )
