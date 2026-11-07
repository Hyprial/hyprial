"""Routine schema goldens + source retry discipline + service duty-cycle tests.

Every guarantee has a failure-path test: removing the guarantee must turn a
test red (hard constraint ④).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hyprial.biz.impl.routine.execution.registry import DEFAULT_TASK_TIMEOUT_SECONDS
from hyprial.biz.impl.routine.contracts.schema import RoutineSchemaError, load_routine_text
from hyprial.biz.impl.routine.execution.service import RoutineService, RoutineServiceError
from hyprial.biz.impl.routine.templates import render_template
from hyprial.biz.impl.routine.execution.source import (
    SOURCE_ERROR_CAP,
    SourcePermanentError,
    SourceTask,
    SourceTransientError,
    query_taskwarrior,
    route_decision,
)
from hyprial.biz.impl.routine.storage.store import RoutineStore

VALID = """
version: 1
name: hq-selfdrive
schedule: {interval: 15m}
source: {kind: taskwarrior, filter: "+selfdrive -blocked"}
policy:
  routes:
    - {tag: "route:worker", target_from: "tagarg"}
    - {tag: "route:self", target: "self"}
    - {tag: "route:human", escalate_to: "user:allen"}
  default: escalate
  task_template: "【自驱 {{nonce}}】{{task.description}} 完成回 DONE {{nonce}}"
limits:
  max_in_flight: 3
  circuit_breaker: {window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm"}
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""


def test_valid_routine_parses() -> None:
    spec = load_routine_text(VALID)
    assert spec.name == "hq-selfdrive"
    assert spec.interval_seconds == 900.0
    assert spec.source_filter == "+selfdrive -blocked"
    assert [r.kind for r in spec.routes] == ["target", "self", "escalate"]
    assert spec.limits.max_in_flight == 3
    assert spec.limits.circuit_breaker.window_runs == 5


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ("name: x\nschedule: {interval: 15m}\nsource: {kind: taskwarrior, filter: f}\npolicy: {routes: [{tag: a, target: self}], task_template: t}\non_task_timeout: {action: escalate, escalate_to: u}", "version"),
        (VALID.replace("interval: 15m", "interval: 10s"), ">= 60"),
        (VALID.replace("kind: taskwarrior", "kind: crystal-ball"), "source.kind"),
        (VALID.replace("target_from: \"tagarg\"", "target_from: \"magic\""), "target_from"),
        (VALID.replace("- {tag: \"route:self\", target: \"self\"}", "- {tag: \"route:self\"}"), "exactly one"),
        (VALID.replace("default: escalate", "default: guess"), "default"),
        (VALID.replace("{{task.description}}", "{{task.mystery}}"), "unknown template variable"),
        (VALID.replace("on_task_timeout: {action: escalate, escalate_to: \"user:allen\"}\n", ""), "on_task_timeout is required"),
        (VALID.replace("window_runs: 5", "window_runs: 1"), "window_runs"),
        (VALID.replace("escalate_ratio: 1.0", "escalate_ratio: 2"), "escalate_ratio"),
        (VALID.replace("routes:", "paths:"), "unknown field"),
    ],
)
def test_invalid_routines_fail_loud(document: str, fragment: str) -> None:
    with pytest.raises(RoutineSchemaError, match=re_escape(fragment)):
        load_routine_text(document)


def re_escape(text: str) -> str:
    import re

    return re.escape(text).replace(r"\ ", " ")


# ── source retry discipline ────────────────────────────────────────────────


def test_missing_task_binary_is_permanent_zero_retry() -> None:
    with pytest.raises(SourcePermanentError):
        query_taskwarrior("+selfdrive", task_bin="/nonexistent/task-binary")


def test_transient_error_on_nonzero_exit() -> None:
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"boom")):
        with pytest.raises(SourceTransientError, match="exited 1"):
            query_taskwarrior("+selfdrive")


def test_query_parses_tasks() -> None:
    payload = json.dumps(
        [{"uuid": "u1", "description": "do thing", "tags": ["selfdrive", "route:self"]}]
    ).encode()
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, payload, b"")):
        tasks = query_taskwarrior("+selfdrive")
    assert tasks == [SourceTask(uuid="u1", description="do thing", tags=("selfdrive", "route:self"))]


def test_route_decision_is_a_lookup() -> None:
    spec = load_routine_text(VALID)
    assert route_decision(("selfdrive", "route:worker:cc-1"), spec.routes, "escalate") == ("target", "cc-1")
    assert route_decision(("selfdrive", "route:self"), spec.routes, "escalate") == ("self", None)
    assert route_decision(("selfdrive", "route:human"), spec.routes, "escalate") == ("escalate", "user:allen")
    assert route_decision(("selfdrive",), spec.routes, "escalate") == ("escalate", None)
    assert route_decision(("selfdrive",), spec.routes, "self") == ("self", None)


# ── service duty cycle ─────────────────────────────────────────────────────


class FakePac:
    """The PAC dispatch seam: one graph per task occurrence.

    Keyed exactly like ``hyprial.routine.pac_dispatch``: a replay of the same
    task returns the existing graph, and a task that already settled is
    reported, never dispatched again.
    """

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.states: dict[str, str] = {}
        self.keys: dict[tuple[str, str, int | None], str] = {}
        self.created_at: dict[str, int] = {}
        self.closed: list[dict[str, str]] = []

    def start_idempotent(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        task_text: str,
        target: str,
        escalate_to: str,
        timeout_seconds: float,
        sender: str,
        role: str = "dispatch",
        occurrence_slot_ms: int | None = None,
        rearm_after_created_at_ms: int = 0,
    ) -> dict[str, object]:
        key = (routine_name, task_uuid, occurrence_slot_ms)
        existing = self.keys.get(key)
        if existing is not None:
            return {"graphId": existing, "state": self.states[existing]}
        prior = next(
            (
                graph_id
                for graph_id in reversed(self.created_at)
                if self.started[int(graph_id.rsplit("-", 1)[1])]["routineName"]
                == routine_name
                and self.started[int(graph_id.rsplit("-", 1)[1])]["taskUuid"]
                == task_uuid
                and self.created_at[graph_id] > rearm_after_created_at_ms
                and self.states[graph_id] == "escalated"
            ),
            None,
        )
        if prior is not None:
            return {"graphId": prior, "state": "escalated"}
        graph_id = f"routine-{routine_name}-{len(self.started):02d}"
        self.started.append(
            {
                "graphId": graph_id,
                "routineName": routine_name,
                "taskUuid": task_uuid,
                "taskText": task_text,
                "target": target,
                "escalateTo": escalate_to,
                "timeoutSeconds": timeout_seconds,
                "sender": sender,
                "occurrenceSlotMs": occurrence_slot_ms,
            }
        )
        self.keys[key] = graph_id
        self.created_at[graph_id] = len(self.started)
        self.states[graph_id] = "running"
        return {"graphId": graph_id, "state": "running"}


    def status(self, *, graph_id: str) -> dict[str, object]:
        return {"graphId": graph_id, "state": self.states[graph_id]}

    def close(self, *, graph_id: str, actor: str) -> None:
        self.closed.append({"graphId": graph_id, "actor": actor})

    def rearm_boundary(self, *, routine_name: str) -> int:
        del routine_name
        return len(self.started)


class FakeAlarm:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def escalate(
        self,
        *,
        to: str,
        text: str,
        reason: str | None = None,
        conversation_id: str = "workflow",
        idempotency_key: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "to": to,
                "text": text,
                "reason": reason or "",
                "conversation_id": conversation_id,
                "idempotency_key": idempotency_key or "",
            }
        )


class StartFailurePac(FakePac):
    def start_idempotent(self, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("interactive target cannot execute")


class Clock:
    def __init__(self) -> None:
        self.now_ms = 0


def make_service(tmp: Path, tasks: list[SourceTask]):
    pac = FakePac()
    alarm = FakeAlarm()
    clock = Clock()
    source_calls = {"n": 0}

    def source(_filter: str) -> list[SourceTask]:
        source_calls["n"] += 1
        return tasks

    service = RoutineService(
        pac=pac,
        alarm=alarm,
        state_dir=tmp,
        clock_ms=lambda: clock.now_ms,
        source_query=source,
    )
    return service, pac, alarm, clock, source_calls


def task(uuid: str, *tags: str, description: str = "do it") -> SourceTask:
    return SourceTask(uuid=uuid, description=description, tags=tags)


#: How long a routine cycle may take to produce an observable effect before
#: we call it hung.  It bounds a HANG, ⛔ not the machine: every predicate in
#: this file waits for something to APPEAR, so a generous budget can only
#: make a real failure slower to report -- never make a passing run fail.
#: 3.0s was a bet on scheduling latency, and a throttled 2-CPU runner
#: collects on it (task 16544 ran under 79s of cgroup throttling).
CYCLE_WAIT_SECONDS = 30.0


def await_true(predicate, *, timeout: float = CYCLE_WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class RoutineServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _make_service(self, *args):
        result = make_service(*args)
        # LIFO cleanups close actor/SQLite owners before deleting their home,
        # even when an assertion fails before the explicit close below.
        self.addCleanup(result[0].close)
        return result

    def _add(self, service: RoutineService) -> None:
        service.add(yaml_text=VALID, owner="agent:o:n:duty")

    def test_cycle_dispatches_routed_tasks_and_escalates_human(self) -> None:
        tasks = [
            task("u1", "selfdrive", "route:worker:cc-1"),
            task("u2", "selfdrive", "route:self"),
            task("u3", "selfdrive", "route:human"),
            task("u4", "selfdrive"),  # no route tag → default escalate
        ]
        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), tasks)
        self._add(service)
        clock.now_ms = 1_000_000  # past first due
        service.submit_timer()
        await_true(lambda: len(pac.started) == 2 and len(alarm.calls) == 2)
        assert len(pac.started) == 2
        assert pac.started[0]["target"] == "cc-1"
        assert pac.started[1]["target"] == "agent:o:n:duty"  # route:self → owner
        assert len(alarm.calls) == 2  # u3 + u4 escalated, both to user:allen
        assert all(c["to"] == "user:allen" for c in alarm.calls)
        # The task uuid is the dispatch's durable key, not part of the text
        # (it used to appear only because the synthesized workflow NAME
        # carried a uuid hint).  The text is the rendered template with no
        # placeholder left and no nonce: completion is the owner flagging the
        # node, so there is nothing for a matcher to look for.
        assert pac.started[0]["taskUuid"] == "u1"
        assert "do it" in str(pac.started[0]["taskText"])
        assert "{{" not in str(pac.started[0]["taskText"])
        assert pac.started[0]["escalateTo"] == "user:allen"
        service.close()

    def test_template_route_self_renders_wake_reason(self) -> None:
        reason = "PAC run run-idle target agent:test:isolated:worker idle for 31m0s"
        service, pac, _, clock, _ = self._make_service(
            Path(self._tmp.name),
            [task("wake-1", "route:self", description=reason)],
        )
        text = render_template(
            "selfdrive",
            owner="agent:o:n:duty",
            escalate_to="user:allen",
            source="taskwarrior",
        )
        service.add(yaml_text=text, owner="agent:o:n:duty")
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: len(pac.started) == 1)
        dispatched = pac.started[0]
        assert dispatched["target"] == "agent:o:n:duty"
        assert f"你被 routine 唤醒:{reason}" in str(dispatched["taskText"])
        assert "{{reason}}" not in str(dispatched["taskText"])
        service.close()

    def test_done_task_dispatches_once_per_occurrence(self) -> None:
        """In-flight blocks overlap; done permits the next occurrence."""

        tasks = [task("u1", "selfdrive", "route:self")]
        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), tasks)
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: len(pac.started) == 1)
        graph_id = str(pac.started[0]["graphId"])
        clock.now_ms = 2_000_000
        service.submit_timer()  # u1 still in flight: no second dispatch
        time.sleep(0.05)
        assert len(pac.started) == 1

        # The owner flagged the node: the routine records done and closes the
        # graph as its creator.
        pac.states[graph_id] = "done"
        clock.now_ms = 3_000_000
        service.submit_timer()
        # Wait for the OUTCOME, ⛔ not for the port-side effect.  `_execute_pac`
        # closes the graph and only THEN hands the completion back, so
        # `pac.closed` is observable strictly before the registry has recorded
        # anything -- waiting on the earlier event and asserting the later one
        # is a race the test wins only on an idle machine (CI task 16544:
        # IndexError on an empty `outcomes`, under 79s of cgroup throttling).
        await_true(
            lambda: service.status(name="hq-selfdrive")["outcomes"][-1:] == ["done"]
        )
        assert pac.closed and pac.closed[0]["graphId"] == graph_id
        assert pac.closed[0]["actor"] == "agent:o:n:duty"

        await_true(lambda: len(pac.started) == 2)
        clock.now_ms = 4_000_000
        service.submit_timer()
        time.sleep(0.05)
        assert len(pac.started) == 2
        service.close()

    def test_max_in_flight_backpressure(self) -> None:
        tasks = [task(f"u{i}", "selfdrive", "route:self") for i in range(6)]
        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), tasks)
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: len(pac.started) == 3)
        assert len(pac.started) == 3  # max_in_flight = 3
        service.close()

    def test_start_failure_persists_one_alarm_and_logs_the_same_fields(self) -> None:
        alarm = FakeAlarm()
        clock = Clock()
        recorder = _EventRecorder()
        service = RoutineService(
            pac=StartFailurePac(),
            alarm=alarm,
            state_dir=Path(self._tmp.name),
            clock_ms=lambda: clock.now_ms,
            source_query=lambda _filter: [task("u1", "route:self")],
            logger=recorder,
        )
        self.addCleanup(service.close)
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(
            lambda: service.status(name="hq-selfdrive")["outcomes"]
            == ["escalated"]
        )
        await_true(lambda: len(alarm.calls) == 1, timeout=1.0)
        [call] = alarm.calls
        assert call["to"] == "user:allen"
        for fragment in (
            "hq-selfdrive",
            "u1",
            "RuntimeError",
            "interactive target cannot execute",
        ):
            assert fragment in call["text"]
        assert recorder.of("routine.start_failed") == [
            {
                "routine": "hq-selfdrive",
                "task": "u1",
                "code": "RuntimeError",
                "detail": "interactive target cannot execute",
                "to": "user:allen",
            }
        ]

    def test_start_failure_alarm_false_then_success_survives_restart(self) -> None:
        class RetryAlarm(FakeAlarm):
            delivered = False

            def escalate(self, **kwargs):  # type: ignore[no-untyped-def]
                super().escalate(**kwargs)
                return SimpleNamespace(
                    status="delivered" if self.delivered else "failed"
                )

        root = Path(self._tmp.name)
        alarm = RetryAlarm()
        clock = Clock()
        service = RoutineService(
            pac=StartFailurePac(),
            alarm=alarm,
            state_dir=root,
            clock_ms=lambda: clock.now_ms,
            source_query=lambda _filter: [task("u1", "route:self")],
        )
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: len(alarm.calls) >= 1)
        stable_key = alarm.calls[0]["idempotency_key"]
        assert stable_key
        await_true(
            lambda: any(
                row.payload.get("kind") == "RoutineAlarmEffect"
                and row.attempts >= 1
                for row in service._projection.pending_effects()
            )
        )
        projection = RoutineStore(root / "routines.sqlite3")
        try:
            assert any(
                row.payload.get("kind") == "RoutineAlarmEffect"
                for row in projection.pending_effects()
            )
        finally:
            projection.close()
        service.close()

        alarm.delivered = True
        revived = RoutineService(
            pac=StartFailurePac(),
            alarm=alarm,
            state_dir=root,
            clock_ms=lambda: clock.now_ms,
            source_query=lambda _filter: [task("u1", "route:self")],
        )
        try:
            # Recovery must preserve the durable retry deadline.  Construction
            # at the same fixed clock is not permission to redeliver early.
            time.sleep(0.1)
            assert len(alarm.calls) == 1
            clock.now_ms += 10_000
            await_true(lambda: any(call["idempotency_key"] == stable_key for call in alarm.calls[1:]))
            await_true(
                lambda: not any(
                    row.payload.get("kind") == "RoutineAlarmEffect"
                    for row in revived._projection.pending_effects()
                )
            )
            assert {call["idempotency_key"] for call in alarm.calls} == {stable_key}
        finally:
            revived.close()

    def test_circuit_breaker_pauses_and_alarms(self) -> None:
        # FAILURE-PATH TEST: remove the breaker and this test turns red —
        # the routine would keep cycling with a full-escalation ledger.
        service, pac, alarm, clock, _ = self._make_service(
            Path(self._tmp.name), [task("human", "selfdrive", "route:human")]
        )
        self._add(service)
        status = service.status(name="hq-selfdrive")
        assert status["enabled"] is True

        def wait_with_diagnostics(label: str, predicate) -> None:
            try:
                await_true(predicate)
            except AssertionError as error:
                diagnostic = service.status(name="hq-selfdrive")
                raise AssertionError(
                    f"{label} wait timed out: "
                    f"status={diagnostic!r} alarms={alarm.calls!r}"
                ) from error

        for cycle in range(5):
            clock.now_ms += 1_000_000
            assert service.submit_timer().value == "accepted"

            wait_with_diagnostics(
                f"committed outcome cycle {cycle}",
                lambda: len(service.status(name="hq-selfdrive")["outcomes"])
                == cycle + 1,
            )
        wait_with_diagnostics(
            "breaker pause",
            lambda: service.status(name="hq-selfdrive")["enabled"] is False,
        )
        status = service.status(name="hq-selfdrive")
        assert status["enabled"] is False, "breaker must pause the routine"
        wait_with_diagnostics(
            "breaker alarm",
            lambda: any("circuit breaker" in c["text"] for c in alarm.calls),
        )
        service.close()

    def test_routine_escalation_carries_per_routine_throttle_key(self) -> None:
        # B3: the alarm throttle ledger keys on (conversation_id, reason); if
        # every routine shared "workflow", routine A would eat routine B's
        # alarm.  The routine service must stamp the routine name into the
        # throttle key.
        service, workflow, alarm, clock, _ = self._make_service(
            Path(self._tmp.name), [task("human", "selfdrive", "route:human")]
        )
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: len(alarm.calls) == 1)
        assert alarm.calls[0]["conversation_id"] == "routine:hq-selfdrive"
        service.close()

    def test_permanent_source_error_pauses_immediately_zero_retry(self) -> None:
        calls = []

        def bad_source(_filter: str) -> list[SourceTask]:
            calls.append(_filter)
            raise SourcePermanentError("task binary missing")

        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), [])
        service._source_query = bad_source  # type: ignore[method-assign]
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: service.status(name="hq-selfdrive")["enabled"] is False)
        assert service.status(name="hq-selfdrive")["enabled"] is False
        # Pausing commits before the asynchronous alarm effect is delivered.
        # Observe the effect itself rather than treating enabled=False as its
        # completion receipt (run #6520 failed between these two events).
        await_true(lambda: any("permanent source error" in c["text"]
                               and "never retried" in c["text"] for c in alarm.calls))
        clock.now_ms += 1_000_000
        service.submit_timer()
        service.pause(name="hq-selfdrive")  # FIFO barrier after the extra tick.
        assert len(calls) == 1
        assert not pac.started
        service.close()

    def test_transient_source_errors_cap_at_ten_then_pause(self) -> None:
        streak = {"n": 0}

        def flaky(_filter: str) -> list[SourceTask]:
            streak["n"] += 1
            raise SourceTransientError(f"boom {streak['n']}")

        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), [])
        service._source_query = flaky  # type: ignore[method-assign]
        self._add(service)
        for cycle in range(SOURCE_ERROR_CAP):
            clock.now_ms += 1_000_000
            service.submit_timer()
            # Wait for the error to have been *recorded*, not merely for the
            # call to have started: `flaky` bumps `streak` on entry, so the
            # old predicate was satisfied while the exception was still in
            # flight and the pause below had not been decided yet.
            await_true(
                lambda: service.status(name="hq-selfdrive")["sourceErrorStreak"]
                >= cycle + 1
            )
        assert streak["n"] == SOURCE_ERROR_CAP
        assert service.status(name="hq-selfdrive")["enabled"] is False
        await_true(lambda: any(f"failed {SOURCE_ERROR_CAP} consecutive" in c["text"] for c in alarm.calls))
        # paused: no further source calls even when due
        clock.now_ms += 1_000_000
        service.submit_timer()
        # The Routine actor is a single FIFO queue and `pause` blocks until its
        # own completion event, so once it returns the timer command queued
        # ahead of it has necessarily been handled. Without this barrier the
        # assertion below ran before the tick was processed and passed even
        # when the routine had never paused at all.
        #
        # ⚠️ This barrier depends on `pause` being unconditionally enqueued
        # (service.py: it always goes through `_submit_wait`). If anyone ever
        # adds an "already paused -> return early" short-circuit, the barrier
        # evaporates *silently* and this assertion goes back to being true no
        # matter what the routine did.
        service.pause(name="hq-selfdrive")
        assert streak["n"] == SOURCE_ERROR_CAP
        service.close()

    def test_resume_clears_breaker_ledger(self) -> None:
        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), [])
        self._add(service)
        service.pause(name="hq-selfdrive")
        result = service.resume(name="hq-selfdrive")
        assert result["enabled"] is True
        status = service.status(name="hq-selfdrive")
        assert status["sourceErrorStreak"] == 0
        assert status["outcomes"] == []
        service.close()

    def test_schema_error_maps_stable_code(self) -> None:
        service, *_ = self._make_service(Path(self._tmp.name), [])
        with self.assertRaises(RoutineServiceError) as ctx:
            service.add(yaml_text="version: 1\nname: x\n", owner="o")
        assert ctx.exception.code == "ROUTINE_SCHEMA_ERROR"
        service.close()

    def test_recover_readopts_routines(self) -> None:
        tasks = [task("u1", "selfdrive", "route:self")]
        service, pac, alarm, clock, _ = self._make_service(Path(self._tmp.name), tasks)
        self._add(service)
        service.close()
        revived, pac2, alarm2, clock2, _ = self._make_service(Path(self._tmp.name), tasks)
        assert revived.recover() == 1
        clock2.now_ms = 10_000_000
        revived.submit_timer()
        await_true(lambda: len(pac2.started) == 1)
        assert len(pac2.started) == 1
        revived.close()

    def test_dispatch_is_single_attempt_with_a_fixed_deadline(self) -> None:
        """①(e2e-verifier 变异挖出)的 U3 形态:单次尝试与固定截止由机器守。

        旧形态守的是合成 workflow spec 的 ``on_timeout.action == escalate``
        与 ``max_attempts == 1``。PAC 侧没有重试字段可以守 —— 单次尝试由
        「一个任务一张图、图按耐久键去重」保证,而截止由 clock 节点的固定
        deadline 保证(裁定 D)。所以这里守的是派发参数本身。
        """

        service, pac, alarm, clock, _ = self._make_service(
            Path(self._tmp.name), [task("u1", "selfdrive", "route:self")]
        )
        self._add(service)
        clock.now_ms = 1_000_000
        service.submit_timer()
        await_true(lambda: len(pac.started) == 1)
        dispatched = pac.started[0]
        assert dispatched["timeoutSeconds"] == DEFAULT_TASK_TIMEOUT_SECONDS
        assert dispatched["escalateTo"] == "user:allen"
        assert dispatched["sender"] == "agent:o:n:duty"
        assert dispatched["taskUuid"] == "u1"
        service.close()

    def test_not_due_means_zero_dispatches_and_zero_source_calls(self) -> None:
        """②(到期才驱动是核心承诺):next_due 在未来时,若干次 tick 必须
        【零次】派发、【零次】源查询 —— 断言它不做事,不是断言它宣称没做。"""
        calls = {"n": 0}

        def counting_source(_filter: str) -> list[SourceTask]:
            calls["n"] += 1
            return [task("u1", "selfdrive", "route:self")]

        pac = FakePac()
        alarm = FakeAlarm()
        clock = Clock()
        service = RoutineService(
            pac=pac,
            alarm=alarm,
            state_dir=Path(self._tmp.name),
            clock_ms=lambda: clock.now_ms,
            source_query=counting_source,
        )
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        # next_due = now + interval(900s); stay strictly before it.
        for _ in range(10):
            clock.now_ms += 10_000  # 100s total, still < 900s
            service.submit_timer()
        assert pac.started == []
        assert calls["n"] == 0
        assert alarm.calls == []
        service.close()


# ── 2026-09-14 alarm-delivery spec: addresses must resolve, loudly ─────────


BARE = VALID.replace("user:allen", "hq-adjutant")
ADJUTANT_URI = "agent:alicesmith:node-a:hq-adjutant"


class _EventRecorder:
    """Duck-typed hyprial.log.Logger for routine event assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def log(self, level: str, event: str, **fields: object) -> None:
        self.events.append((level, event, dict(fields)))

    def of(self, event: str) -> list[dict[str, object]]:
        return [fields for _, name, fields in self.events if name == event]


def _seed_stored_routine(root: Path, yaml_text: str) -> None:
    from hyprial.biz.impl.routine.storage.store import RoutineRow, RoutineStore

    store = RoutineStore(root / "routines.sqlite3")
    store.apply(
        routine=RoutineRow(
            "hq-selfdrive", yaml_text, "agent:o:n:duty", True, 10**12, 0, "[]", 0, 1
        )
    )
    store.close()


class AddressValidationTests(unittest.TestCase):
    def test_bare_name_escalate_to_rejected_at_load(self) -> None:
        # Spec 验收 1 / mutation 1: dropping the load-time check must redden.
        for fragment in ("policy.routes[0].escalate_to", "on_task_timeout.escalate_to"):
            bad = (
                VALID.replace(
                    '- {tag: "route:human", escalate_to: "user:allen"}',
                    '- {tag: "route:human", escalate_to: "hq-adjutant"}',
                )
                if fragment.startswith("policy")
                else VALID.replace(
                    'on_task_timeout: {action: escalate, escalate_to: "user:allen"}',
                    'on_task_timeout: {action: escalate, escalate_to: "hq-adjutant"}',
                )
            )
            with self.assertRaises(RoutineSchemaError) as ctx:
                load_routine_text(bad)
            assert "hq-adjutant" in str(ctx.exception)
            assert "bare names are undeliverable" in str(ctx.exception)

    def test_user_form_accepted_route_rejected(self) -> None:
        spec = load_routine_text(VALID)  # user: form
        assert spec.escalate_to == "user:allen"
        # B4: route: would pass schema but fail at delivery — reject it loud.
        with self.assertRaises(RoutineSchemaError) as ctx:
            load_routine_text(VALID.replace("user:allen", "route:lark:main"))
        # #2092: the rejection now names the supported use and the setup command
        message = str(ctx.exception)
        assert "not a deliverable escalate_to/report_to address" in message
        assert "hyprial message send --to route:<adapter>:<route>" in message


class StoredAddressMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _service(self, migrate_address, logger=None):
        pac = FakePac()
        alarm = FakeAlarm()
        return (
            RoutineService(
                pac=pac,
                alarm=alarm,
                state_dir=self.root,
                source_query=lambda _filter: [],
                migrate_address=migrate_address,
                logger=logger,
            ),
            pac,
            alarm,
        )

    def test_unique_roster_match_rewrites_with_ledger_and_is_idempotent(self) -> None:
        _seed_stored_routine(self.root, BARE)
        resolve = {"hq-adjutant": ADJUTANT_URI}.get
        recorder = _EventRecorder()
        service, _, _ = self._service(resolve, recorder)
        listed = service.list()["routines"]
        assert len(listed) == 1
        assert listed[0].get("quarantined") is not True  # migrated, healthy
        migrations = service.address_migrations()
        assert {(m["field"], m["before"], m["after"]) for m in migrations} == {
            ("policy.routes[2].escalate_to", "hq-adjutant", ADJUTANT_URI),
            ("on_task_timeout.escalate_to", "hq-adjutant", ADJUTANT_URI),
        }
        logged = recorder.of("routine.address_migrated")
        assert len(logged) == 2
        assert all(item["before"] == "hq-adjutant" for item in logged)
        # The rewrite is durable: a second construction validates cleanly and
        # the INSERT OR IGNORE ledger does not grow.
        service.close()
        again, _, _ = self._service(resolve)
        assert len(again.address_migrations()) == 2
        again.close()

    def test_no_roster_match_quarantines_instead_of_guessing(self) -> None:
        _seed_stored_routine(self.root, BARE)
        recorder = _EventRecorder()
        service, pac, _ = self._service(lambda _name: None, recorder)
        listed = service.list()["routines"]
        assert listed[0].get("quarantined") is True
        assert "bare names are undeliverable" in str(listed[0].get("quarantineReason"))
        assert recorder.of("routine.quarantined")
        # Quarantine blocks enable/resume loud (approved Q2), not silently.
        with self.assertRaises(RoutineServiceError) as ctx:
            service.resume(name="hq-selfdrive")
        assert ctx.exception.code == "ROUTINE_QUARANTINED"
        # And it never dispatches: past-due timer stays inert.
        service.submit_timer()
        await_true(lambda: pac.started == [] and not pac.started)
        assert pac.started == []
        service.close()

    def test_half_resolvable_spec_migrates_nothing(self) -> None:
        # All-or-nothing: one unresolvable address must veto any rewrite.
        two_bare = BARE.replace(
            '- {tag: "route:self", target: "self"}',
            '- {tag: "route:self", target: "self"}\n    - {tag: "route:other", escalate_to: "other-agent"}',
        )
        _seed_stored_routine(self.root, two_bare)
        service, _, _ = self._service({"hq-adjutant": ADJUTANT_URI}.get)
        assert service.address_migrations() == []
        listed = service.list()["routines"]
        assert listed[0].get("quarantined") is True
        service.close()


class RoutineEventLogTests(unittest.TestCase):
    """Spec 5b: routine lifecycle writes events whose names carry 'routine'."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_breaker_pause_and_escalation_log_events_with_routine_name(self) -> None:
        recorder = _EventRecorder()
        pac = FakePac()
        alarm = FakeAlarm()
        clock = Clock()
        service = RoutineService(
            pac=pac,
            alarm=alarm,
            state_dir=Path(self._tmp.name),
            clock_ms=lambda: clock.now_ms,
            source_query=lambda _filter: [
                task("human", "selfdrive", "route:human")
            ],
            logger=recorder,
        )
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        for cycle in range(5):
            clock.now_ms += 1_000_000
            service.submit_timer()
            await_true(lambda: len(alarm.calls) >= cycle + 1)
        await_true(lambda: service.status(name="hq-selfdrive")["enabled"] is False)
        escalated = recorder.of("routine.escalated")
        assert escalated, "every escalation effect must log routine.escalated"
        assert all(item["routine"] == "hq-selfdrive" for item in escalated)
        paused = recorder.of("routine.paused")
        assert paused, "breaker pause must log routine.paused"
        assert paused[0]["routine"] == "hq-selfdrive"
        assert "circuit breaker" in str(paused[0]["reason"])
        # Resume is an event too (spec 5b: escalated/paused/breaker/resumed).
        service.resume(name="hq-selfdrive")
        resumed = recorder.of("routine.resumed")
        assert resumed and resumed[0]["routine"] == "hq-selfdrive"
        service.close()


if __name__ == "__main__":
    unittest.main()
