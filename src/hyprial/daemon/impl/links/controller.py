"""Pure link controller: k, W, cwnd, windows, abandoned samples (WP1).

Design: docs/design/design-send-links-2026-10-14.md §5; implementation spec:
docs/design/send-links-impl/wp1.md §1.2 with the review rulings B5–B9.  No
I/O, no clock, no randomness: every instant is a ``now_ms`` parameter and
every period jitter is a caller-supplied ``jitter`` in ``[-1, 1]`` so WP2 can
replay with a seeded RNG.

All design constants live in :class:`ControllerConstants`; nothing else in
this module (or in ``policy.py``) may spell a design constant inline.
"""

from __future__ import annotations

import math
from collections.abc import Set as AbstractSet, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final, Literal

from hyprial.daemon.impl.links.model import is_link_endpoint

__all__ = [
    "ControllerConstants",
    "ControllerInput",
    "ControllerOutput",
    "ControllerState",
    "DEFAULTS",
    "DwellSample",
    "LoopState",
    "ReplySample",
    "RttSample",
    "WindowStats",
    "abandon_threshold_ms",
    "censor",
    "compute_k",
    "evaluate",
    "initial_state",
    "period_ms",
    "rtt_window_stats",
    "shrink_proportional",
    "vegas_step",
    "window_stats",
]

#: ``W`` is seeded at its unconstrained value: ``compute_k(n, unbounded)``.
_UNBOUNDED_W: Final = 1 << 31


@dataclass(frozen=True, slots=True)
class ControllerConstants:
    """Every design constant of the controller, in one place (§5.5)."""

    alpha: float = 1.0
    beta: float = 3.0
    probe_gain: float = 1.25
    abandon_factor: float = 4.0
    window_factor: float = 10.0
    min_window_samples: int = 10
    jitter_ratio: float = 0.1
    ewma_gain: float = 0.125
    stale_rtt_factor: float = 2.0
    grad_streak: int = 2
    min_link_lifetime_periods: float = 1.0
    churn_limit_per_period: int = 0


DEFAULTS: Final = ControllerConstants()


def _require_positive_ms(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive int of milliseconds")
    return value


@dataclass(frozen=True, slots=True)
class DwellSample:
    """Inbound dwell ``T``: arrival to this node's own reply (§5.1).

    A censored sample is a request that never got a reply; it enters the
    statistics at its abandon threshold, not at the real wait (§5.1).
    """

    at_ms: int
    dwell_ms: int
    censored: bool = False

    def __post_init__(self) -> None:
        _require_positive_ms(self.dwell_ms, "dwell_ms")


@dataclass(frozen=True, slots=True)
class RttSample:
    """Round-trip ``RTT_l``: this node's request sent to a received reply."""

    peer: str
    at_ms: int
    rtt_ms: int
    censored: bool = False

    def __post_init__(self) -> None:
        _require_positive_ms(self.rtt_ms, "rtt_ms")


@dataclass(frozen=True, slots=True)
class ReplySample:
    """One reply, tagged by direction so node and per-link throughput differ.

    ``sent`` feeds the node's W throughput (§5.1, "replies this node sends");
    ``received`` on ``peer`` feeds that link's cwnd throughput (§5.4).  Never
    mix the two: they measure opposite directions (review B6).
    """

    at_ms: int
    peer: str | None = None
    direction: Literal["sent", "received"] = "sent"

    def __post_init__(self) -> None:
        if self.direction not in ("sent", "received"):
            raise ValueError(f"unknown reply direction: {self.direction!r}")
        if self.direction == "received":
            if self.peer is None or not is_link_endpoint(self.peer):
                raise ValueError(
                    f"a received reply needs a link peer, got {self.peer!r}"
                )


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Statistics of one sliding window (node W or one link's cwnd)."""

    n: int
    t_min_ms: int | None
    t_max_ms: int | None
    t_bar_ms: float | None
    mu_max_per_ms: float | None
    w_target: int | None
    window_ms: int | None


@dataclass(frozen=True, slots=True)
class LoopState:
    """One Vegas/CDG loop: the node's W or one link's cwnd."""

    value: int
    prev_t_min_ms: int | None
    grad_pos_streak: int
    next_eval_ms: int | None
    last_diff: float | None
    last_grad: int | None


@dataclass(frozen=True, slots=True)
class ControllerInput:
    """Everything one evaluation reads; assembled by WP2 from local state."""

    n_active: int
    dwell: Sequence[DwellSample]
    replies: Sequence[ReplySample]
    rtt: Sequence[RttSample]
    links: AbstractSet[str]
    unanswered: Mapping[str, int]
    held_peers: AbstractSet[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "links", frozenset(self.links))
        object.__setattr__(self, "held_peers", frozenset(self.held_peers))
        object.__setattr__(self, "dwell", tuple(self.dwell))
        object.__setattr__(self, "replies", tuple(self.replies))
        object.__setattr__(self, "rtt", tuple(self.rtt))
        object.__setattr__(self, "unanswered", MappingProxyType(dict(self.unanswered)))


@dataclass(frozen=True, slots=True)
class ControllerState:
    w: LoopState
    cwnd: Mapping[str, LoopState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cwnd", MappingProxyType(dict(self.cwnd)))


@dataclass(frozen=True, slots=True)
class ControllerOutput:
    n_active: int
    k: int
    w: int
    cwnd: Mapping[str, int]
    period_ms: int | None
    node: WindowStats
    links: Mapping[str, WindowStats]
    diff: float | None
    grad: int | None


def compute_k(n_active: int, w: int) -> int:
    """Topology budget ``k = clamp(max(2, ceil(ln N)), 2, W)`` (§5.2)."""

    if n_active < 0:
        raise ValueError("n_active must not be negative")
    if n_active <= 1:
        base = 2
    else:
        base = max(2, math.ceil(math.log(n_active)))
    return min(base, max(2, w))


def _cold_loop(value: int) -> LoopState:
    return LoopState(
        value=value,
        prev_t_min_ms=None,
        grad_pos_streak=0,
        next_eval_ms=None,
        last_diff=None,
        last_grad=None,
    )


def _stats_from(
    pairs: Sequence[tuple[int, int, bool]],
    reply_ats: Sequence[int],
    now_ms: int,
    c: ControllerConstants,
) -> WindowStats:
    """Two-step window over (at_ms, value, censored) triples.

    Step 1 seeds ``T_min`` from the most recent ``min_window_samples``
    non-future samples and fixes the window length at ``window_factor`` times
    it.  Step 2 recomputes every statistic inside ``[now - window, now]``;
    samples outside it are dropped rather than counted (review B8).
    """

    ordered = sorted(pairs, key=lambda item: item[0])
    if not ordered:
        return WindowStats(0, None, None, None, None, None, None)
    seed = min(value for _, value, _ in ordered[-c.min_window_samples :])
    window_ms = int(c.window_factor * seed)
    start = now_ms - window_ms
    window = [item for item in ordered if start <= item[0] <= now_ms]
    if not window:
        return WindowStats(0, None, None, None, None, None, window_ms)
    t_min = min(value for _, value, _ in window)
    # Censored samples sit at their own abandon threshold; letting them into
    # T_max would grow the next threshold 4x each round (review B9).
    measured = [value for _, value, censored in window if not censored]
    t_max = max(measured) if measured else None
    t_bar = float(window[0][1])
    for _, value, _ in window[1:]:
        t_bar = (1 - c.ewma_gain) * t_bar + c.ewma_gain * value
    in_window = [at for at in reply_ats if start <= at <= now_ms]
    buckets: dict[int, int] = {}
    for at in in_window:
        index = (at - start) // t_min
        buckets[index] = buckets.get(index, 0) + 1
    best = max(buckets.values()) if buckets else 0
    mu_max: float = best / t_min
    w_target = best if best else None
    return WindowStats(len(window), t_min, t_max, t_bar, mu_max, w_target, window_ms)


def window_stats(
    dwell: Sequence[DwellSample],
    replies: Sequence[ReplySample],
    now_ms: int,
    c: ControllerConstants = DEFAULTS,
) -> WindowStats:
    """Node-level window over inbound dwell and replies this node sent."""

    pairs = [
        (sample.at_ms, sample.dwell_ms, sample.censored)
        for sample in dwell
        if sample.at_ms <= now_ms
    ]
    sent = [
        sample.at_ms
        for sample in replies
        if sample.direction == "sent" and sample.at_ms <= now_ms
    ]
    return _stats_from(pairs, sent, now_ms, c)


def rtt_window_stats(
    rtt: Sequence[RttSample],
    replies: Sequence[ReplySample],
    peer: str,
    now_ms: int,
    c: ControllerConstants = DEFAULTS,
) -> WindowStats:
    """One link's window over that peer's RTT and replies received from it."""

    pairs = [
        (sample.at_ms, sample.rtt_ms, sample.censored)
        for sample in rtt
        if sample.peer == peer and sample.at_ms <= now_ms
    ]
    received = [
        sample.at_ms
        for sample in replies
        if sample.direction == "received"
        and sample.peer == peer
        and sample.at_ms <= now_ms
    ]
    return _stats_from(pairs, received, now_ms, c)


def period_ms(
    t_min_ms: int, jitter: float, c: ControllerConstants = DEFAULTS
) -> int:
    """The jittered evaluation period ``10 x T_min x (1 + 0.1 x jitter)``."""

    if not -1 <= jitter <= 1:
        raise ValueError(f"jitter must be within [-1, 1], got {jitter!r}")
    scaled = c.window_factor * t_min_ms * (1 + c.jitter_ratio * jitter)
    return max(1, round(scaled))


def abandon_threshold_ms(
    rtt_max_ms: int, c: ControllerConstants = DEFAULTS
) -> int:
    """The give-up threshold: ``abandon_factor`` times ``RTT_max`` (§5.1)."""

    return math.ceil(c.abandon_factor * rtt_max_ms)


def censor(open_since_ms: int, now_ms: int, threshold_ms: int) -> int | None:
    """The censored value of an unanswered request, or None if still young.

    The value is the threshold itself, never the real wait: counting the real
    wait would bias the baseline with exactly the outlier being capped.
    """

    if now_ms - open_since_ms >= threshold_ms:
        return threshold_ms
    return None


def vegas_step(
    loop: LoopState,
    stats: WindowStats,
    *,
    floor: int,
    now_ms: int,
    jitter: float,
    c: ControllerConstants = DEFAULTS,
) -> LoopState:
    """One Vegas/CDG step shared by W and every cwnd (§5.3)."""

    if stats.n < c.min_window_samples:
        return loop
    if loop.next_eval_ms is not None and now_ms < loop.next_eval_ms:
        return loop
    if stats.t_min_ms is None or stats.t_bar_ms is None or stats.t_bar_ms <= 0:
        return loop
    t_min = stats.t_min_ms
    t_bar = stats.t_bar_ms
    diff = loop.value * (1 - t_min / t_bar)
    grad = None if loop.prev_t_min_ms is None else t_min - loop.prev_t_min_ms
    grad_positive = grad is not None and grad > 0
    streak = loop.grad_pos_streak + 1 if grad_positive else 0
    value = loop.value
    if streak >= c.grad_streak:
        value = max(floor, math.floor(value * t_min / t_bar))
        streak = 0
    elif diff > c.beta:
        value = value - 1
    elif diff < c.alpha and not grad_positive:
        # Increase-only: a shrunk W_target must not cut W here; shrinking
        # belongs to the CDG and beta rules above.
        grown = value + 1
        if stats.w_target is None or grown <= math.ceil(c.probe_gain * stats.w_target):
            value = grown
    value = max(floor, value)
    return LoopState(
        value=value,
        prev_t_min_ms=t_min,
        grad_pos_streak=streak,
        next_eval_ms=now_ms + period_ms(t_min, jitter, c),
        last_diff=diff,
        last_grad=grad,
    )


def shrink_proportional(
    value: int, base_ms: int, observed_ms: float, *, floor: int
) -> int:
    """Shrink one loop in proportion to its own baseline (§5.4)."""

    shrunk = math.floor(value * base_ms / observed_ms)
    return max(floor, min(value, shrunk))


def initial_state(
    n_active: int, c: ControllerConstants = DEFAULTS
) -> ControllerState:
    """The cold-start state: W at its unconstrained value, no cwnd yet."""

    w = compute_k(n_active, _UNBOUNDED_W)
    return ControllerState(w=_cold_loop(w), cwnd={})


def _advance_cwnd(
    loop: LoopState,
    stats: WindowStats,
    peer: str,
    inp: ControllerInput,
    now_ms: int,
    jitter: float,
    c: ControllerConstants,
) -> LoopState:
    """Advance one link's cwnd: pressure shrink first, else a Vegas step."""

    rtt_min = stats.t_min_ms
    if rtt_min is None:
        return loop
    due = loop.next_eval_ms is None or now_ms >= loop.next_eval_ms
    if not due:
        return loop
    oldest = inp.unanswered.get(peer)
    oldest_age = None if oldest is None else now_ms - oldest
    held = peer in inp.held_peers
    stale = oldest_age is not None and oldest_age > c.stale_rtt_factor * rtt_min
    if held or stale:
        observed = float(rtt_min)
        if stats.t_bar_ms is not None:
            observed = max(observed, stats.t_bar_ms)
        if oldest_age is not None:
            observed = max(observed, float(oldest_age))
        if held:
            observed = max(observed, rtt_min * c.probe_gain)
        value = shrink_proportional(loop.value, rtt_min, observed, floor=1)
        diff = value * (1 - rtt_min / stats.t_bar_ms) if stats.t_bar_ms else None
        return LoopState(
            value=value,
            prev_t_min_ms=rtt_min,
            grad_pos_streak=0,
            next_eval_ms=now_ms + period_ms(rtt_min, jitter, c),
            last_diff=diff,
            last_grad=None,
        )
    return vegas_step(loop, stats, floor=1, now_ms=now_ms, jitter=jitter, c=c)


def evaluate(
    state: ControllerState,
    inp: ControllerInput,
    now_ms: int,
    jitter: float,
    c: ControllerConstants = DEFAULTS,
) -> tuple[ControllerState, ControllerOutput]:
    """Advance W and every cwnd once and report the displayed components."""

    # The topology floor is independent of the current W: W starts at k on a
    # cold node, and k must be able to grow when N_active grows.  Feeding the
    # old W back into compute_k made W=2 a permanent cap (card 713).
    topology_floor = compute_k(inp.n_active, _UNBOUNDED_W)
    node = window_stats(inp.dwell, inp.replies, now_ms, c)
    w_loop = vegas_step(
        state.w, node, floor=topology_floor, now_ms=now_ms, jitter=jitter, c=c
    )
    w = max(w_loop.value, topology_floor)
    if w != w_loop.value:
        w_loop = replace(w_loop, value=w)
    k = compute_k(inp.n_active, w)
    cwnd: dict[str, int] = {}
    link_stats: dict[str, WindowStats] = {}
    cwnd_loops: dict[str, LoopState] = {}
    for peer in sorted(inp.links):
        stats = rtt_window_stats(inp.rtt, inp.replies, peer, now_ms, c)
        loop = state.cwnd.get(peer) or _cold_loop(1)
        loop = _advance_cwnd(loop, stats, peer, inp, now_ms, jitter, c)
        loop = replace(loop, value=max(1, loop.value))
        cwnd_loops[peer] = loop
        cwnd[peer] = loop.value
        link_stats[peer] = stats
    period: int | None = None
    if w_loop.next_eval_ms is not None and node.t_min_ms is not None:
        period = period_ms(node.t_min_ms, jitter, c)
    new_state = ControllerState(w=w_loop, cwnd=cwnd_loops)
    output = ControllerOutput(
        n_active=inp.n_active,
        k=k,
        w=w,
        cwnd=MappingProxyType(cwnd),
        period_ms=period,
        node=node,
        links=MappingProxyType(link_stats),
        diff=w_loop.last_diff,
        grad=w_loop.last_grad,
    )
    return new_state, output
