"""WP1 controller tests: constants, windows, Vegas/CDG, degradation conditions.

Cases C1-C14, D1-D6 and the dependency/purity machine checks T-B1/T-B2 of
docs/design/send-links-impl/wp1.md §3.2.
"""

from __future__ import annotations

import ast
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import hyprial.daemon.impl.links.controller as controller
import hyprial.daemon.impl.links.model as model
import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.controller import (
    DEFAULTS,
    ControllerInput,
    ControllerOutput,
    ControllerState,
    DwellSample,
    LoopState,
    ReplySample,
    RttSample,
    WindowStats,
    abandon_threshold_ms,
    censor,
    compute_k,
    evaluate,
    initial_state,
    period_ms,
    rtt_window_stats,
    shrink_proportional,
    vegas_step,
    window_stats,
)

B = "agent:o:m:b"
C = "agent:o:m:c"

_LINKS_DIR = Path(controller.__file__).resolve().parent
_MODULES = ("model.py", "controller.py", "policy.py")
_ALLOWED_STDLIB = frozenset(
    {"__future__", "collections.abc", "dataclasses", "math", "typing", "types", "json"}
)
_ALLOWED_HYPRIAL = frozenset(
    {"hyprial.kernel", "hyprial.daemon.impl.links.ports"}
)
# Only the kernel's public face; ``hyprial.kernel.impl.*`` stays forbidden.
_ALLOWED_KERNEL_FACE = frozenset({"hyprial.kernel", "hyprial.kernel.interface"})
_INTRA = {
    "model.py": frozenset(),
    "controller.py": frozenset({"hyprial.daemon.impl.links.model"}),
    "policy.py": frozenset(
        {
            "hyprial.daemon.impl.links.model",
            "hyprial.daemon.impl.links.controller",
        }
    ),
}
_FORBIDDEN_TOKENS = (
    "inbox",
    "orgfs",
    "application",
    "dispatch",
    "delivery",
    "composition",
    "biz",
    "shell",
)
_IMPURE_MODULES = frozenset(
    {"time", "datetime", "random", "secrets", "os", "logging"}
)


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.lineno))
    return found


class _FloatSiteCollector(ast.NodeVisitor):
    """Record which class body each float literal sits in."""

    def __init__(self) -> None:
        self.classes: list[str] = []
        self.sites: list[tuple[str | None, int]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            owner = self.classes[-1] if self.classes else None
            self.sites.append((owner, node.lineno))


def _loop(
    value: int,
    *,
    prev_t_min: int | None = None,
    streak: int = 0,
    next_eval_ms: int | None = None,
) -> LoopState:
    return LoopState(
        value=value,
        prev_t_min_ms=prev_t_min,
        grad_pos_streak=streak,
        next_eval_ms=next_eval_ms,
        last_diff=None,
        last_grad=None,
    )


def _stats(
    *,
    t_min: int,
    t_bar: float,
    n: int = 10,
    w_target: int | None = None,
) -> WindowStats:
    return WindowStats(
        n=n,
        t_min_ms=t_min,
        t_max_ms=t_min,
        t_bar_ms=t_bar,
        mu_max_per_ms=None,
        w_target=w_target,
        window_ms=None,
    )


def _dwell(count: int, value: int, *, step: int = 100) -> tuple[DwellSample, ...]:
    return tuple(DwellSample(at_ms=i * step, dwell_ms=value) for i in range(count))


def test_c1_constants_live_only_in_controller_constants() -> None:
    defaults = DEFAULTS
    assert (defaults.alpha, defaults.beta) == (1, 3)
    assert defaults.probe_gain == 1.25
    assert defaults.abandon_factor == 4
    assert defaults.window_factor == 10
    assert defaults.min_window_samples == 10
    assert defaults.stale_rtt_factor == 2
    assert defaults.grad_streak == 2
    assert defaults.ewma_gain == 0.125 and defaults.jitter_ratio == 0.1
    assert defaults.min_link_lifetime_periods == 1.0
    assert defaults.churn_limit_per_period == 0
    with pytest.raises(FrozenInstanceError):
        defaults.alpha = 2  # type: ignore[misc]
    for module in (controller, policy):
        collector = _FloatSiteCollector()
        collector.visit(ast.parse(Path(module.__file__).read_text()))
        for owner, lineno in collector.sites:
            assert owner == "ControllerConstants", (
                f"{module.__name__} float literal outside "
                f"ControllerConstants at line {lineno}"
            )


def test_c2b_w_grows_to_the_topology_floor_after_a_cold_start() -> None:
    """W seeded at k=2 must not cap k forever after N_active grows."""

    state = initial_state(1)
    state, out = evaluate(
        state,
        ControllerInput(
            n_active=319,
            dwell=(),
            replies=(),
            rtt=(),
            links=frozenset(),
            unanswered={},
            held_peers=frozenset(),
        ),
        now_ms=1000,
        jitter=0.0,
    )

    assert out.k == 6
    assert out.w >= 6
    assert state.w.value >= 6


def test_c2_compute_k_reads_the_design_anchors() -> None:
    assert compute_k(23, 100) == 4
    assert compute_k(350, 100) == 6
    assert compute_k(0, 100) == 2
    assert compute_k(1, 100) == 2
    assert compute_k(3, 100) == 2
    assert compute_k(23, 3) == 3
    assert compute_k(1000, 2) == 2
    with pytest.raises(ValueError):
        compute_k(-1, 5)


def test_c3_window_uses_the_two_step_baseline_and_expires_samples() -> None:
    now = 20000
    recent = (16100, 16500, 17000, 17500, 18000, 18500, 19000, 19500, 19900, 20000)
    inside = DwellSample(at_ms=16000, dwell_ms=200)
    expired = DwellSample(at_ms=14900, dwell_ms=50)
    stats = window_stats(
        [*[DwellSample(at_ms=t, dwell_ms=500) for t in recent], inside, expired],
        (),
        now,
    )
    assert stats.window_ms == 5000
    assert stats.t_min_ms == 200
    assert stats.t_max_ms == 500

    old = tuple(DwellSample(at_ms=i, dwell_ms=100) for i in range(9))
    fresh = DwellSample(at_ms=10000, dwell_ms=400)
    expired_baseline = window_stats([*old, fresh], (), 10000)
    assert expired_baseline.t_min_ms == 400
    assert expired_baseline.n == 1
    assert expired_baseline.n < DEFAULTS.min_window_samples
    link = rtt_window_stats(
        [RttSample(peer=B, at_ms=s.at_ms, rtt_ms=s.dwell_ms) for s in (*old, fresh)],
        (),
        B,
        10000,
    )
    assert link.t_min_ms == 400 and link.n == 1


def test_c4_throughput_target_is_the_busiest_bucket() -> None:
    now = 9000
    dwell = tuple(DwellSample(at_ms=t, dwell_ms=1000) for t in range(0, 10000, 1000))
    replies = tuple(
        ReplySample(at_ms=t) for t in (100, 200, 300, 400, 500, 1100, 1200, 1300)
    )
    stats = window_stats(dwell, replies, now)
    assert stats.t_min_ms == 1000
    assert stats.mu_max_per_ms == pytest.approx(0.005)
    assert stats.w_target == 5
    idle = window_stats(dwell, (), now)
    assert idle.w_target is None
    assert idle.mu_max_per_ms == 0.0


def test_c5_t_bar_is_an_ewma_with_the_first_sample_as_seed() -> None:
    dwell = (
        DwellSample(at_ms=0, dwell_ms=1000),
        DwellSample(at_ms=1000, dwell_ms=2000),
        DwellSample(at_ms=2000, dwell_ms=3000),
    )
    stats = window_stats(dwell, (), 2000)
    expected = 1000.0
    expected += 0.125 * (2000 - expected)
    expected += 0.125 * (3000 - expected)
    assert stats.t_bar_ms == pytest.approx(expected)
    assert stats.t_bar_ms == pytest.approx(1359.375)


def test_c6_period_is_jittered_inside_nine_to_eleven_times_t_min() -> None:
    assert period_ms(1000, 0) == 10000
    assert period_ms(1000, 1) == 11000
    assert period_ms(1000, -1) == 9000
    with pytest.raises(ValueError):
        period_ms(1000, 1.5)


def test_c7_vegas_increases_once_and_respects_the_probe_cap() -> None:
    loop = _loop(4)
    grown = vegas_step(
        loop, _stats(t_min=1000, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert grown.value == 5
    assert grown.next_eval_ms == 10000
    capped = vegas_step(
        loop,
        _stats(t_min=1000, t_bar=1000, w_target=3),
        floor=4,
        now_ms=0,
        jitter=0.0,
    )
    assert capped.value == 4
    assert math.ceil(DEFAULTS.probe_gain * 3) == 4


def test_c8_vegas_decreases_when_diff_exceeds_beta() -> None:
    loop = _loop(8)
    shrunk = vegas_step(
        loop, _stats(t_min=500, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert shrunk.value == 7
    floored = vegas_step(
        _loop(4), _stats(t_min=500, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert floored.value == 4


def test_c9_cdg_shrinks_only_after_two_positive_gradient_periods() -> None:
    first = _loop(10, prev_t_min=900)
    one_period = vegas_step(
        first, _stats(t_min=1000, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert one_period.value == 10
    assert one_period.grad_pos_streak == 1
    second = _loop(10, prev_t_min=900, streak=1)
    two_periods = vegas_step(
        second, _stats(t_min=1000, t_bar=2000), floor=4, now_ms=0, jitter=0.0
    )
    assert two_periods.value == 5
    assert two_periods.grad_pos_streak == 0


def test_c10_not_due_returns_the_loop_untouched() -> None:
    loop = _loop(7, next_eval_ms=100)
    result = vegas_step(
        loop, _stats(t_min=1000, t_bar=1000), floor=1, now_ms=50, jitter=0.0
    )
    assert result is loop


def test_c11_cwnd_initial_floor_stale_shrink_and_removal() -> None:
    few = tuple(RttSample(peer=B, at_ms=t, rtt_ms=100) for t in (0, 100, 200))
    _, cold = evaluate(
        initial_state(23), ControllerInput(23, (), (), few, {B}, {}), 300, 0.0
    )
    assert cold.cwnd[B] == 1

    full = tuple(RttSample(peer=B, at_ms=t, rtt_ms=100) for t in range(0, 1000, 100))
    state = ControllerState(w=initial_state(23).w, cwnd={B: _loop(2)})
    _, stale = evaluate(
        state, ControllerInput(23, (), (), full, {B}, {B: 1000 - 250}), 1000, 0.0
    )
    assert stale.cwnd[B] == 1
    assert shrink_proportional(2, 100, 250, floor=1) == 1

    _, boundary = evaluate(
        state, ControllerInput(23, (), (), full, {B}, {B: 1000 - 200}), 1000, 0.0
    )
    assert boundary.cwnd[B] != 1  # exactly 2x RTT_min does not trigger a shrink

    _, removed = evaluate(
        state, ControllerInput(23, (), (), full, set(), {}), 1000, 0.0
    )
    assert removed.cwnd == {}


def test_c11b_hold_shrink_is_once_per_period_and_deterministic() -> None:
    rtt = tuple(RttSample(peer=B, at_ms=t, rtt_ms=100) for t in range(0, 1000, 100))
    state = ControllerState(w=initial_state(23).w, cwnd={B: _loop(2)})
    inp = ControllerInput(23, (), (), rtt, {B}, {}, held_peers={B})
    first_state, first = evaluate(state, inp, 1000, 0.0)
    assert first.cwnd[B] == 1
    # The same tick re-evaluated any number of times yields the same state:
    # holding pressure shrinks at most once per link period.
    for _ in range(100):
        repeated_state, repeated = evaluate(state, inp, 1000, 0.0)
        assert (repeated_state, repeated) == (first_state, first)
    assert first_state.cwnd[B].next_eval_ms == 1000 + period_ms(100, 0.0)
    _, next_period = evaluate(
        first_state, inp, first_state.cwnd[B].next_eval_ms or 0, 0.0
    )
    assert next_period.cwnd[B] == 1
    # A reclaimed peer (gone from ``links``) can never be revived by late
    # samples or hold events still present in the input.
    dropped_state, dropped = evaluate(
        first_state, ControllerInput(23, (), (), rtt, set(), {}, held_peers={B}), 1000, 0.0
    )
    assert dropped.cwnd == {} and dict(dropped_state.cwnd) == {}


def test_c12_cwnd_is_per_link_and_never_globally_capped() -> None:
    peers = [f"agent:o:m:{name}" for name in "bcdef"]
    _, out = evaluate(
        initial_state(23),
        ControllerInput(23, (), (), (), set(peers), {}),
        1000,
        0.0,
    )
    assert set(out.cwnd) == set(peers)
    assert all(value >= 1 for value in out.cwnd.values())
    assert sum(out.cwnd.values()) == 5
    assert out.w < sum(out.cwnd.values())

    rtt = tuple(RttSample(peer=B, at_ms=t, rtt_ms=100) for t in range(0, 1000, 100))
    _, mixed = evaluate(
        initial_state(23),
        ControllerInput(23, (), (), rtt, {B, C}, {}),
        1000,
        0.0,
    )
    assert mixed.cwnd[B] == 2
    assert mixed.cwnd[C] == 1


def test_c13_evaluate_is_a_deterministic_complete_snapshot() -> None:
    dwell = _dwell(10, 1000)
    replies = tuple(ReplySample(at_ms=t) for t in (0, 200, 400, 600, 800))
    rtt = tuple(RttSample(peer=B, at_ms=t, rtt_ms=100) for t in range(0, 1000, 100))
    inp = ControllerInput(23, dwell, replies, rtt, {B}, {})
    state = initial_state(23)
    first_state, first = evaluate(state, inp, 1000, 0.0)
    second_state, second = evaluate(state, inp, 1000, 0.0)
    assert (first_state, first) == (second_state, second)
    for name in (
        "n_active",
        "k",
        "w",
        "cwnd",
        "period_ms",
        "node",
        "links",
        "diff",
        "grad",
    ):
        assert hasattr(first, name)
    assert first.n_active == 23
    assert first.w >= first.k
    assert set(first.links) == {B}


def test_c14_abandon_threshold_and_censored_value() -> None:
    assert abandon_threshold_ms(250) == 1000
    assert censor(0, 999, 1000) is None
    assert censor(0, 1000, 1000) == 1000
    assert censor(0, 5000, 1000) == 1000


def _drive_d1(
    *,
    ticks: int = 3200,
    tick_ms: int = 200,
    n_active: int = 23,
    capacity: int = 6,
) -> list[tuple[int, int, ControllerOutput]]:
    """A synthetic node whose service time degrades once W exceeds capacity."""

    state = initial_state(n_active)
    dwell: list[DwellSample] = []
    replies: list[ReplySample] = []
    accumulator = 0.0
    now = 0
    history: list[tuple[int, int, ControllerOutput]] = []
    for _ in range(ticks):
        w = state.w.value
        service = 1000 if w <= capacity else 1000 * (1 + (w - capacity))
        dwell.append(DwellSample(at_ms=now, dwell_ms=service))
        accumulator += min(w, capacity) / service * tick_ms
        while accumulator >= 1.0:
            replies.append(ReplySample(at_ms=now, direction="sent"))
            accumulator -= 1.0
        window_dwell = [s for s in dwell if s.at_ms >= now - 50000]
        window_replies = [s for s in replies if s.at_ms >= now - 50000]
        inp = ControllerInput(
            n_active=n_active,
            dwell=window_dwell,
            replies=window_replies,
            rtt=(),
            links=set(),
            unanswered={},
        )
        previous = state.w.value
        state, out = evaluate(state, inp, now, 0.0, DEFAULTS)
        history.append((now, previous, out))
        now += tick_ms
    return history


def test_d1_service_rate_loss_does_not_run_w_away() -> None:
    history = _drive_d1()
    for _, previous, out in history:
        assert out.w >= out.k
        assert out.w - previous <= 1
        if out.w > previous:
            assert out.w - previous == 1
            assert out.grad is None or out.grad <= 0
            if out.node.w_target is not None:
                assert out.w <= math.ceil(DEFAULTS.probe_gain * out.node.w_target)
    tail = history[-1000:]
    assert all(4 <= out.w <= 8 for _, _, out in tail)
    values = {out.w for _, _, out in tail}
    assert min(values) < max(values)  # oscillates around capacity, no runaway climb


def _drive_d2(
    *,
    ticks: int = 800,
    tick_ms: int = 1000,
    step_at: int = 20000,
) -> list[tuple[int, ControllerOutput]]:
    """Latency steps from 1000ms to 30000ms with no queueing (T_bar ~ T_min)."""

    state = initial_state(23)
    dwell: list[DwellSample] = []
    replies: list[ReplySample] = []
    accumulator = 0.0
    now = 0
    rows: list[tuple[int, ControllerOutput]] = []
    for _ in range(ticks):
        service = 1000 if now < step_at else 30000
        w = state.w.value
        dwell.append(DwellSample(at_ms=now, dwell_ms=service))
        accumulator += min(w, 6) / service * tick_ms
        while accumulator >= 1.0:
            replies.append(ReplySample(at_ms=now, direction="sent"))
            accumulator -= 1.0
        window_dwell = [s for s in dwell if s.at_ms >= now - 400000]
        window_replies = [s for s in replies if s.at_ms >= now - 400000]
        inp = ControllerInput(23, window_dwell, window_replies, (), set(), {})
        state, out = evaluate(state, inp, now, 0.0, DEFAULTS)
        rows.append((now, out))
        now += tick_ms
    return rows


def test_d2_non_congestive_slowness_never_pins_w_below_k() -> None:
    rows = _drive_d2()
    assert all(out.w >= out.k for _, out in rows)
    switched = [(now, out) for now, out in rows if out.node.t_min_ms == 30000]
    assert switched
    start = switched[0][0]
    recovered = [
        now
        for now, out in rows
        if now >= start and out.diff is not None and out.diff < DEFAULTS.alpha
    ]
    assert recovered
    assert recovered[0] - start <= 10 * 30000


def _drive_d3(*, ticks: int = 200, tick_ms: int = 100, switch_at: int = 2000):
    state = initial_state(23)
    rtt: list[RttSample] = []
    replies: list[ReplySample] = []
    now = 0
    rows = []
    for _ in range(ticks):
        for peer in (B, C):
            if peer == C and now < switch_at:
                continue
            rtt.append(
                RttSample(
                    peer=peer, at_ms=now, rtt_ms=100 if now < switch_at else 400
                )
            )
            replies.append(ReplySample(at_ms=now, peer=peer, direction="received"))
        state, out = evaluate(
            state, ControllerInput(23, (), tuple(replies), tuple(rtt), {B, C}, {}),
            now, 0.0, DEFAULTS,
        )
        rows.append(
            (now, out.links[B].t_min_ms, out.links[C].t_min_ms, out.cwnd[B], out.cwnd[C])
        )
        now += tick_ms
    return rows


def test_d3_latecomer_baseline_expires_for_both_links() -> None:
    rows = _drive_d3()
    shared = [row for row in rows if row[1] == 400 and row[2] == 400]
    assert shared
    assert shared[0][0] <= 2000 + 10000
    tail = rows[-20:]
    assert all(abs(row[3] - row[4]) <= 1 for row in tail)


def test_d4_jitter_desynchronises_identical_controllers() -> None:
    inp = ControllerInput(23, _dwell(10, 1000), (), (), set(), {})
    state = initial_state(23)
    plus_state, plus = evaluate(state, inp, 1000, 0.7)
    minus_state, minus = evaluate(state, inp, 1000, -0.7)
    assert plus.period_ms == 10700
    assert minus.period_ms == 9300
    assert plus_state.w.next_eval_ms != minus_state.w.next_eval_ms
    assert 0.9 * 10000 <= plus.period_ms <= 1.1 * 10000
    assert 0.9 * 10000 <= minus.period_ms <= 1.1 * 10000
    first_state, first = evaluate(state, inp, 1000, 0.7)
    second_state, second = evaluate(state, inp, 1000, 0.7)
    assert (first_state, first) == (second_state, second)


def test_d5_censored_samples_raise_the_baseline_without_faking_throughput() -> None:
    now = 1500
    fast = tuple(DwellSample(at_ms=t, dwell_ms=100) for t in range(600, 1600, 100))
    censored = tuple(DwellSample(at_ms=t, dwell_ms=400, censored=True) for t in range(100, 600, 100))
    without = window_stats(fast, (), now)
    with_censored = window_stats([*fast, *censored], (), now)
    assert without.t_bar_ms == pytest.approx(100.0)
    assert with_censored.t_bar_ms > without.t_bar_ms
    assert censor(0, 400, abandon_threshold_ms(100)) == 400
    state = initial_state(23)
    _, clean = evaluate(state, ControllerInput(23, fast, (), (), set(), {}), now, 0.0)
    _, biased = evaluate(
        state, ControllerInput(23, (*fast, *censored), (), (), set(), {}), now, 0.0
    )
    assert biased.w <= clean.w


def test_d6_cold_start_keeps_w_at_k_and_cwnd_at_one() -> None:
    state = initial_state(23)
    assert state.w.value == 4
    assert state.w.value == compute_k(23, state.w.value)
    assert state.cwnd == {}
    few = _dwell(5, 1000, step=10)
    few_rtt = tuple(RttSample(peer=B, at_ms=t * 100, rtt_ms=100) for t in range(5))
    for _ in range(5):
        state, out = evaluate(
            state, ControllerInput(23, few, (), few_rtt, {B}, {}), 1000, 0.0
        )
        assert out.w == 4
        assert out.cwnd[B] == 1
        assert out.period_ms is None
        assert out.diff is None and out.grad is None
    _, started = evaluate(
        state,
        ControllerInput(23, _dwell(10, 1000), (), few_rtt, {B}, {}),
        1000,
        0.0,
    )
    assert 4 <= started.w <= 5
    assert started.period_ms == 10000


def test_t_b1_links_modules_only_depend_on_their_allowed_faces() -> None:
    for name in _MODULES:
        path = _LINKS_DIR / name
        assert path.is_file()
        for module, lineno in _imported_modules(path):
            assert not any(token in module for token in _FORBIDDEN_TOKENS), (
                f"forbidden import {module} in {name}:{lineno}"
            )
            if module in _ALLOWED_STDLIB or module in _ALLOWED_HYPRIAL:
                continue
            if module in _ALLOWED_KERNEL_FACE:
                continue
            if module in _INTRA[name]:
                continue
            raise AssertionError(f"forbidden import {module} in {name}:{lineno}")
    assert model.__name__ == "hyprial.daemon.impl.links.model"


def test_t_b2_links_modules_are_pure() -> None:
    for name in _MODULES:
        path = _LINKS_DIR / name
        for module, lineno in _imported_modules(path):
            assert module.split(".")[0] not in _IMPURE_MODULES, (
                f"impure import {module} in {name}:{lineno}"
            )
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "print"}, (
                    f"impure call {node.func.id} in {name}:{node.lineno}"
                )
    assert policy.__name__ == "hyprial.daemon.impl.links.policy"


def _b6_series() -> tuple[tuple[DwellSample, ...], tuple[RttSample, ...]]:
    times = range(0, 10000, 1000)
    dwell = tuple(DwellSample(at_ms=t, dwell_ms=1000) for t in times)
    rtt = tuple(
        RttSample(peer=peer, at_ms=t, rtt_ms=1000) for peer in (B, C) for t in times
    )
    return dwell, rtt


_B6_SENT = (
    ReplySample(at_ms=100, peer=B, direction="sent"),
    ReplySample(at_ms=200, peer=B, direction="sent"),
    ReplySample(at_ms=300, peer=C, direction="sent"),
    ReplySample(at_ms=1100, peer=B, direction="sent"),
)
_B6_RECEIVED = (
    *(ReplySample(at_ms=t, peer=B, direction="received") for t in (100, 200, 300, 400, 500)),
    ReplySample(at_ms=1100, peer=C, direction="received"),
    ReplySample(at_ms=1150, peer=C, direction="received"),
)


def test_c15_node_and_link_throughput_count_only_their_own_direction() -> None:
    dwell, rtt = _b6_series()
    mixed = (*_B6_SENT, *_B6_RECEIVED)
    now = 9000
    node = window_stats(dwell, mixed, now)
    # window starts at -1000, buckets are 1000 ms: sent 100/200/300 share one.
    assert node.w_target == 3
    assert node.mu_max_per_ms == pytest.approx(3 / 1000)
    link_b = rtt_window_stats(rtt, mixed, B, now)
    assert link_b.w_target == 5
    assert link_b.mu_max_per_ms == pytest.approx(5 / 1000)
    link_c = rtt_window_stats(rtt, mixed, C, now)
    assert link_c.w_target == 2
    assert link_c.mu_max_per_ms == pytest.approx(2 / 1000)


def test_c16_received_replies_never_move_the_node_w_target() -> None:
    dwell, _ = _b6_series()
    now = 9000
    base = window_stats(dwell, _B6_SENT, now)
    with_received = window_stats(dwell, (*_B6_SENT, *_B6_RECEIVED), now)
    assert base.w_target == 3
    assert with_received.w_target == base.w_target
    assert with_received.mu_max_per_ms == base.mu_max_per_ms


def test_c17_sent_replies_never_move_a_link_cwnd_baseline() -> None:
    _, rtt = _b6_series()
    now = 9000
    base = rtt_window_stats(rtt, _B6_RECEIVED, B, now)
    with_sent = rtt_window_stats(rtt, (*_B6_RECEIVED, *_B6_SENT), B, now)
    assert base.w_target == 5
    assert with_sent.w_target == base.w_target
    assert with_sent.mu_max_per_ms == base.mu_max_per_ms


def test_c18_censored_samples_never_feed_the_abandon_threshold() -> None:
    now = 1500
    fast = tuple(DwellSample(at_ms=t, dwell_ms=100) for t in range(600, 1600, 100))
    threshold = abandon_threshold_ms(100)
    first = tuple(
        DwellSample(at_ms=t, dwell_ms=threshold, censored=True)
        for t in range(100, 600, 100)
    )
    second = tuple(
        DwellSample(at_ms=t, dwell_ms=abandon_threshold_ms(threshold), censored=True)
        for t in range(650, 1500, 200)
    )
    plain = window_stats(fast, (), now)
    after_one = window_stats([*fast, *first], (), now)
    after_two = window_stats([*fast, *first, *second], (), now)
    assert plain.t_max_ms == 100
    assert after_one.t_max_ms == plain.t_max_ms
    assert after_two.t_max_ms == plain.t_max_ms
    assert abandon_threshold_ms(after_two.t_max_ms) == threshold == 400
    # Only censored samples: no uncensored baseline, treated as cold start.
    only = window_stats(first, (), 600)
    assert only.t_max_ms is None
    link = rtt_window_stats(
        [
            RttSample(peer=B, at_ms=s.at_ms, rtt_ms=s.dwell_ms, censored=True)
            for s in first
        ],
        (),
        B,
        600,
    )
    assert link.t_max_ms is None


def test_c19_increase_branch_never_lowers_w() -> None:
    stats = _stats(t_min=1000, t_bar=1000, w_target=2)
    grown = vegas_step(_loop(10), stats, floor=4, now_ms=0, jitter=0.0)
    assert math.ceil(DEFAULTS.probe_gain * 2) == 3
    assert grown.value == 10
    allowed = vegas_step(
        _loop(10), _stats(t_min=1000, t_bar=1000, w_target=20), floor=4,
        now_ms=0, jitter=0.0,
    )
    assert allowed.value == 11


def test_c20_beta_boundary_holds_inside_alpha_beta_and_shrinks_above() -> None:
    # diff = value * (1 - t_min / t_bar); here t_min / t_bar = 1/2.
    at_alpha_beta = vegas_step(
        _loop(6), _stats(t_min=500, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert at_alpha_beta.last_diff == pytest.approx(DEFAULTS.beta)
    assert at_alpha_beta.value == 6
    inside = vegas_step(
        _loop(4), _stats(t_min=500, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert DEFAULTS.alpha < inside.last_diff <= DEFAULTS.beta
    assert inside.value == 4
    above = vegas_step(
        _loop(7), _stats(t_min=500, t_bar=1000), floor=4, now_ms=0, jitter=0.0
    )
    assert above.last_diff > DEFAULTS.beta
    assert above.value == 6


def test_c21_uncensored_sample_equal_to_a_censored_one_still_counts() -> None:
    now = 1300
    base = [DwellSample(at_ms=1000 + 10 * i, dwell_ms=100) for i in range(10)]
    samples = [
        *base,
        DwellSample(at_ms=1200, dwell_ms=400, censored=True),
        DwellSample(at_ms=1200, dwell_ms=400),
    ]
    assert window_stats(samples, (), now).t_max_ms == 400
    link = [RttSample(peer=B, at_ms=s.at_ms, rtt_ms=s.dwell_ms, censored=s.censored)
            for s in samples]
    assert rtt_window_stats(link, (), B, now).t_max_ms == 400


def test_c22_alpha_boundary_does_not_grow_but_just_below_does() -> None:
    # diff = value * (1 - t_min / t_bar); value 2 and a 1/2 ratio gives alpha.
    at_alpha = vegas_step(
        _loop(2, prev_t_min=500), _stats(t_min=500, t_bar=1000),
        floor=1, now_ms=0, jitter=0.0,
    )
    assert at_alpha.last_diff == DEFAULTS.alpha
    assert at_alpha.last_grad == 0
    assert at_alpha.value == 2
    below = vegas_step(
        _loop(2, prev_t_min=501), _stats(t_min=501, t_bar=1000),
        floor=1, now_ms=0, jitter=0.0,
    )
    assert below.last_diff < DEFAULTS.alpha
    assert below.last_grad == 0
    assert below.value == 3


def test_c23_window_start_endpoint_is_inclusive() -> None:
    now = 1200
    recent = [DwellSample(at_ms=1100 + 10 * i, dwell_ms=100) for i in range(10)]
    # window = 10 * 100 = 1000 ms, so the start is exactly 200.
    on_edge = window_stats([*recent, DwellSample(at_ms=200, dwell_ms=400)], (), now)
    assert on_edge.window_ms == 1000
    assert on_edge.t_max_ms == 400 and on_edge.n == 11
    outside = window_stats([*recent, DwellSample(at_ms=199, dwell_ms=400)], (), now)
    assert outside.t_max_ms == 100 and outside.n == 10
    link = [RttSample(peer=B, at_ms=s.at_ms, rtt_ms=s.dwell_ms) for s in recent]
    edge = rtt_window_stats(
        [*link, RttSample(peer=B, at_ms=200, rtt_ms=400)], (), B, now
    )
    assert edge.t_max_ms == 400
