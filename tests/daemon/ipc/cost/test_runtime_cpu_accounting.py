from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from hyprial.daemon.impl.ipc.ipc_stats import ipc_stats_payload
from hyprial.daemon.impl.state_persistence.alarm import CpuOwnerBudgetAlarm
from hyprial.kernel import (
    RUNTIME_OVERFLOW_OWNER,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    CallCostCounters,
    EffectCompleted,
    EffectLane,
    EffectRequest,
    RuntimeCpuCounters,
    runtime_cpu_counters,
)
from hyprial.kernel.impl.facts.cost_counters import _RUNTIME_EXCLUDED_OWNERS

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class ProbeEffect:
    value: int


@dataclass(frozen=True, slots=True)
class ProbeCommand:
    value: int


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("condition was not satisfied before timeout")


def _run_one_effect(
    counters: RuntimeCpuCounters,
    *,
    name: str,
    payload: object,
    execute: Callable[[object], object] | None = None,
) -> None:
    completed = threading.Event()
    lane: EffectLane[object, object] | None = None

    def finish(event: EffectCompleted[object]) -> AdmissionResult:
        assert lane is not None
        lane.acknowledge(event.operation_id, event.generation)
        completed.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(
        name=name,
        execute=(lambda value: value) if execute is None else execute,
        complete=finish,
        cpu_accounting=counters,
    )
    try:
        assert lane.submit(EffectRequest("probe", 1, payload)) is AdmissionResult.ACCEPTED
        assert completed.wait(2)
    finally:
        assert lane.close(2)


def test_effect_and_actor_choke_points_attribute_exactly_one_unit() -> None:
    effect_costs = RuntimeCpuCounters()
    _run_one_effect(effect_costs, name="probe-effect-3", payload=ProbeEffect(1))
    assert effect_costs.snapshot() == {
        "probe-effect/ProbeEffect": {
            "calls": 1,
            "threadCpuMs": pytest.approx(0.0, abs=10.0),
        }
    }

    actor_costs = RuntimeCpuCounters()
    handled = threading.Event()
    runtime = ActorRuntime(cpu_accounting=actor_costs)
    actor = runtime.start(
        ActorSpec(name="probe-actor", handler_factory=lambda: lambda _command: handled.set())
    )
    assert runtime.tell(actor, ProbeCommand(1)) is AdmissionResult.ACCEPTED
    assert handled.wait(1)
    _wait_until(lambda: runtime.snapshot(actor).in_flight == 0)
    assert runtime.drain(1).complete
    assert actor_costs.snapshot() == {
        "probe-actor/ProbeCommand": {
            "calls": 1,
            "threadCpuMs": pytest.approx(0.0, abs=10.0),
        }
    }


def test_declared_effect_lane_names_aggregate_numeric_instances() -> None:
    counters = RuntimeCpuCounters()
    _run_one_effect(counters, name="probe-effect-3", payload=ProbeEffect(1))
    _run_one_effect(counters, name="probe-effect-7", payload=ProbeEffect(2))
    assert set(counters.snapshot()) == {"probe-effect/ProbeEffect"}
    assert counters.snapshot()["probe-effect/ProbeEffect"]["calls"] == 2


def test_runtime_cardinality_is_capped_and_overflow_is_counted() -> None:
    counters = RuntimeCpuCounters(max_keys=4)
    for index in range(6):
        counters.record("bounded-owner", f"Work{index}", cpu_seconds=0.001)
    counters.record("late-owner", "Work", cpu_seconds=0.002)

    snapshot = counters.snapshot()
    assert len(snapshot) == 4
    # Overflow never lands in another owner's row (review 912 B2).
    assert snapshot[f"{RUNTIME_OVERFLOW_OWNER}/_other"] == {
        "calls": 4,
        "threadCpuMs": pytest.approx(5.0),
    }
    assert not any(key.startswith("late-owner/") for key in snapshot)
    assert "bounded-owner/_other" not in snapshot
    assert counters.overflow_folds == 4


@pytest.mark.parametrize(
    ("declared", "owner"),
    [
        ("pac-actor-effect-3", "pac-actor-effect"),
        ("turn:agent0", "turn"),
        ("interactive-carrier:some-agent", "interactive-carrier"),
        ("query-io:agent-7", "query-io"),
        ("orgfs-space-f5541506-436-state-work", "orgfs-space-*-state-work"),
        ("orgfs-checkout-0123456789ab-io", "orgfs-checkout-*-io"),
        ("orgfs-replica-f5541506-436-effects", "orgfs-replica-*-effects"),
        ("orgfs-directory-lifecycle-effects", "orgfs-directory-lifecycle-effects"),
        ("orgfs-blob-effects", "orgfs-blob-effects"),
    ],
)
def test_per_agent_and_per_space_owners_share_one_row(declared: str, owner: str) -> None:
    assert RuntimeCpuCounters.normalize_owner(declared) == owner


class _RaisingCounters(RuntimeCpuCounters):
    def record_normalized(self, owner: str, work_type: str, *, cpu_seconds: float) -> None:
        raise RuntimeError("accounting failed")


def test_failing_accounting_never_changes_effect_or_dispatch_outcome() -> None:
    counters = _RaisingCounters()
    events: list[EffectCompleted[object]] = []
    completed = threading.Event()
    lane: EffectLane[object, object] | None = None

    def finish(event: EffectCompleted[object]) -> AdmissionResult:
        assert lane is not None
        lane.acknowledge(event.operation_id, event.generation)
        events.append(event)
        completed.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(
        name="probe-effect", execute=lambda value: value, complete=finish,
        cpu_accounting=counters,
    )
    try:
        assert lane.submit(EffectRequest("probe", 1, 41)) is AdmissionResult.ACCEPTED
        assert completed.wait(2)
    finally:
        assert lane.close(2)
    assert [(event.result, event.error) for event in events] == [(41, None)]

    handled: list[int] = []
    both = threading.Event()

    def handler(command: ProbeCommand) -> None:
        handled.append(command.value)
        if len(handled) == 2:
            both.set()

    runtime = ActorRuntime(cpu_accounting=counters)
    actor = runtime.start(ActorSpec(name="probe-actor", handler_factory=lambda: handler))
    assert runtime.tell(actor, ProbeCommand(1)) is AdmissionResult.ACCEPTED
    assert runtime.tell(actor, ProbeCommand(2)) is AdmissionResult.ACCEPTED
    assert both.wait(1)
    assert handled == [1, 2]
    assert runtime.drain(1).complete


def test_excluded_owner_names_are_declared_by_real_components() -> None:
    names: set[str] = set()
    for path in (ROOT / "src" / "hyprial").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "name":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    names.add(node.value.value)
    # A renamed component would silently re-enter the generic choke point and
    # be counted twice; fail instead.
    assert set(_RUNTIME_EXCLUDED_OWNERS) <= names


def test_session_actor_keeps_existing_side_without_runtime_double_count() -> None:
    explicit = CallCostCounters(["ProbeCommand"], wall=False)
    before = runtime_cpu_counters.snapshot().get("session-authority/ProbeCommand")
    handled = threading.Event()

    def handle(command: object) -> None:
        started = time.thread_time()
        handled.set()
        explicit.record(
            type(command).__name__, cpu_seconds=time.thread_time() - started
        )

    runtime = ActorRuntime(cpu_accounting=runtime_cpu_counters)
    actor = runtime.start(
        ActorSpec(name="session-authority", handler_factory=lambda: handle)
    )
    assert runtime.tell(actor, ProbeCommand(1)) is AdmissionResult.ACCEPTED
    assert handled.wait(1)
    _wait_until(lambda: runtime.snapshot(actor).in_flight == 0)
    assert runtime.drain(1).complete

    assert explicit.snapshot()["ProbeCommand"]["calls"] == 1
    assert (
        runtime_cpu_counters.snapshot().get("session-authority/ProbeCommand")
        == before
    )


def test_cpu_burning_effect_covers_ninety_percent_of_process_cpu() -> None:
    counters = RuntimeCpuCounters()

    def burn(value: object) -> object:
        started = time.thread_time()
        while time.thread_time() - started < 0.05:
            pass
        return value

    process_before = time.process_time()
    runtime_before = sum(
        row["threadCpuMs"] for row in counters.snapshot().values()
    )
    _run_one_effect(
        counters,
        name="coverage-effect-0",
        payload=ProbeEffect(1),
        execute=burn,
    )
    process_delta_ms = (time.process_time() - process_before) * 1000
    runtime_delta_ms = (
        sum(row["threadCpuMs"] for row in counters.snapshot().values())
        - runtime_before
    )
    assert runtime_delta_ms >= process_delta_ms * 0.9


def test_ps_payload_exposes_runtime_folds_and_coverage_total() -> None:
    methods = CallCostCounters(["m"], wall=True)
    methods.record("m", cpu_seconds=0.001, wall_seconds=0.001)
    def empty() -> CallCostCounters:
        return CallCostCounters([], wall=False)
    runtime = RuntimeCpuCounters(max_keys=2)
    runtime.record("owner", "First", cpu_seconds=0.002)
    runtime.record("owner", "Second", cpu_seconds=0.003)

    payload = ipc_stats_payload(
        methods=methods,
        session_actor=empty(),
        agent_actor=empty(),
        effect_admission=empty(),
        session_persistence_io=empty(),
        state_persistence=empty(),
        runtime=runtime,
    )

    assert payload["runtime"] == runtime.snapshot()
    assert payload["runtimeOverflowFolds"] == 1
    assert payload["instrumentedCpuMs"] == pytest.approx(1.0)
    assert payload["coverageCpuMs"] == pytest.approx(6.0)


def test_cpu_owner_budget_alarm_is_windowed_deduplicated_and_ranks_work() -> None:
    now = [0.0]
    alarm = CpuOwnerBudgetAlarm(
        window_seconds=30.0,
        share=0.5,
        clock=lambda: now[0],
    )
    assert alarm.observe({}) == ()

    now[0] = 30.0
    events = alarm.observe(
        {
            "busy/Alpha": {"calls": 3, "threadCpuMs": 10_000.0},
            "busy/Beta": {"calls": 2, "threadCpuMs": 7_000.0},
            "busy/Gamma": {"calls": 1, "threadCpuMs": 1_000.0},
            "quiet/Work": {"calls": 1, "threadCpuMs": 2_000.0},
        }
    )
    assert len(events) == 1
    assert events[0] == {
        "owner": "busy",
        "windowMs": 30_000,
        "threadCpuMs": 18_000.0,
        "share": 0.6,
        "topWorkTypes": [
            {"workType": "Alpha", "calls": 3, "threadCpuMs": 10_000.0},
            {"workType": "Beta", "calls": 2, "threadCpuMs": 7_000.0},
            {"workType": "Gamma", "calls": 1, "threadCpuMs": 1_000.0},
        ],
    }

    now[0] = 31.0
    assert alarm.observe(
        {"busy/Alpha": {"calls": 4, "threadCpuMs": 19_000.0}}
    ) == ()

    # Still hot a window later: the 300 s cooldown holds it (review 912 S2).
    now[0] = 60.0
    assert alarm.observe(
        {
            "busy/Alpha": {"calls": 8, "threadCpuMs": 30_000.0},
            "busy/Beta": {"calls": 4, "threadCpuMs": 24_000.0},
        }
    ) == ()

    now[0] = 330.0
    assert len(
        alarm.observe(
            {
                "busy/Alpha": {"calls": 16, "threadCpuMs": 200_000.0},
                "busy/Beta": {"calls": 4, "threadCpuMs": 24_000.0},
            }
        )
    ) == 1


def test_cpu_owner_budget_alarm_never_names_the_overflow_row() -> None:
    now = [0.0]
    alarm = CpuOwnerBudgetAlarm(window_seconds=30.0, share=0.5, clock=lambda: now[0])
    assert alarm.observe({}) == ()
    now[0] = 30.0
    assert alarm.observe(
        {f"{RUNTIME_OVERFLOW_OWNER}/_other": {"calls": 9, "threadCpuMs": 29_000.0}}
    ) == ()


def test_cpu_owner_budget_alarm_stays_quiet_under_budget() -> None:
    now = [0.0]
    alarm = CpuOwnerBudgetAlarm(
        window_seconds=30.0,
        share=0.5,
        clock=lambda: now[0],
    )
    assert alarm.observe({}) == ()
    now[0] = 30.0
    assert alarm.observe(
        {"quiet/Work": {"calls": 2, "threadCpuMs": 14_999.0}}
    ) == ()
