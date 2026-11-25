"""The hosted reconcile coordinator never blocks on an actor that cannot answer (I6-2)."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.reconcile import (
    CellReconcileCoordinator,
    DictLocalMemberFacts,
    MemberEffectIntent,
)
from hyprial.daemon.impl.cells.reconcile.requests import (
    BeginWorkCommand,
    FinishWorkCommand,
    ReconcileReply,
    ReconcileRequest,
    ReconcileRequestRefused,
    settle_request,
)
from hyprial.kernel import AdmissionResult, subject_cell_id
from tests.daemon.cells.actor.actor_support import wait_until
from tests.daemon.cells.runtime.cell_runtime_support import authority_for

STALL_S = 3.0
PROMPT_S = 1.0


class _Effects:
    def submit(self, intent: MemberEffectIntent) -> None:
        return None


class _StalledActor:
    """Takes or refuses commands but never runs them; a blocking ask stalls."""

    def __init__(self, admission: AdmissionResult) -> None:
        self.admission = admission
        self.told: list[object] = []
        self.asked = threading.Event()

    def tell(self, command: object) -> AdmissionResult:
        self.told.append(command)
        return self.admission

    def ask(self, _command: object, *, timeout: float = 5.0) -> object:
        self.asked.set()
        threading.Event().wait(timeout)
        raise TimeoutError("stalled actor")

    def stop(self, _timeout: float = 1.0) -> bool:
        return True


class _Host:
    def __init__(self, actor: _StalledActor) -> None:
        self.actor = actor

    def find(self, _group: str, _key: str) -> None:
        return None

    def spawn(self, _spec: object) -> _StalledActor:
        return self.actor


def _cell() -> Cell:
    agent_id = f"a.{uuid.uuid4().hex[:24]}a"
    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=authority_for("user:owner1"),
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        name="worker",
        members=(CellMember("w1", "harness"),),
        revision=1,
    )


def _coordinator(actor: _StalledActor) -> tuple[CellReconcileCoordinator, Cell]:
    coordinator = CellReconcileCoordinator(
        facts_port=DictLocalMemberFacts({}),
        effects_port=_Effects(),
        actor_host=_Host(actor),  # type: ignore[arg-type]
        request_timeout_s=STALL_S,
    )
    cell = _cell()
    coordinator.on_index([cell])
    return coordinator, cell


def test_refused_admission_answers_at_once_without_a_blocking_ask() -> None:
    actor = _StalledActor(AdmissionResult.OVERLOADED)
    coordinator, cell = _coordinator(actor)
    try:
        started = time.monotonic()
        assert coordinator.begin_work(cell.cell_id, "w1", "work-1") is False
        assert coordinator.finish_work(cell.cell_id, "w1", "work-1") is False
        assert time.monotonic() - started < PROMPT_S
        assert not actor.asked.is_set()
    finally:
        coordinator.close()


def test_close_releases_a_caller_waiting_for_an_accepted_request() -> None:
    actor = _StalledActor(AdmissionResult.ACCEPTED)
    coordinator, cell = _coordinator(actor)
    told_before = len(actor.told)
    results: list[bool] = []
    caller = threading.Thread(
        target=lambda: results.append(coordinator.begin_work(cell.cell_id, "w1", "work-1")),
        daemon=True,
    )
    caller.start()
    try:
        wait_until(lambda: len(actor.told) > told_before or actor.asked.is_set())
        started = time.monotonic()
        coordinator.close()
        caller.join(PROMPT_S)
        assert not caller.is_alive()
        assert time.monotonic() - started < PROMPT_S
        assert results == [False]
        assert not actor.asked.is_set()
    finally:
        coordinator.close()
        caller.join(STALL_S + 1)


def test_a_handler_interrupt_reaches_the_caller_as_a_refusal_not_the_raw_error() -> None:
    request = ReconcileRequest(FinishWorkCommand("w1", "work-1"), ReconcileReply())

    def interrupted(_command: object) -> object:
        raise KeyboardInterrupt("handler interrupted")

    with pytest.raises(KeyboardInterrupt):
        settle_request(request, interrupted)
    with pytest.raises(ReconcileRequestRefused, match="KeyboardInterrupt"):
        request.reply.wait(PROMPT_S)


def test_begin_work_whose_caller_gave_up_never_claims_a_slot() -> None:
    reply = ReconcileReply()
    with pytest.raises(TimeoutError):
        reply.wait(0.0)
    dispatched: list[object] = []

    def dispatch(command: object) -> object:
        dispatched.append(command)
        return True

    assert settle_request(ReconcileRequest(BeginWorkCommand("w1", "work-1"), reply), dispatch) is False
    assert dispatched == []
    late_finish = ReconcileRequest(FinishWorkCommand("w1", "work-1"), ReconcileReply())
    late_finish.reply.fail(TimeoutError("caller gave up"))
    assert settle_request(late_finish, dispatch) is True
    assert dispatched == [late_finish.command]
