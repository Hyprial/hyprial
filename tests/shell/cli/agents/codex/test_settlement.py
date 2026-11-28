"""cli_codex_interactive: settlement scenarios."""

from __future__ import annotations
from contextlib import closing
import sqlite3
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses import codex
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore

from .support import (
    batch_route,
    ACTOR,
    RecordingLogger,
    THREAD_ID,
    _delay_actor_turn_projection,
    _legacy_runtime_context as _legacy_runtime_context,
    _hint,
    _prompt,
)

_PUMP_IDLE_BUDGET_SECONDS = 30.0
# stop() joins the pump thread for the StreamingTurnProcess stop budget (product
# default 2.0s). Even with the pump idle, shard 4 of #2527 (run 50607) and of
# #2543 (head 060be5dae) still expired it: 3-CPU cgroup, load 8-11, cgroup
# throttling and heavy gen-0 GC on the xdist worker. The join measures only
# scheduler time here, so this test gives it the same bound as the idle wait;
# a pump that never exits still fails, bounded. The product default is unchanged.
_PUMP_STOP_BUDGET_SECONDS = 30.0


def test_codex_interactive_carrier_retries_unsettled_final_without_second_turn(
    tmp_path: Path,
) -> None:
    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "turn-1"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "durable final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    logger = RecordingLogger()
    message = {
        "messageId": "m-retry",
        "conversationId": "c-retry",
        "from": "adapter:lark:test",
        "intent": "request",
        "message": "answer me",
    }
    replies: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [] if len(replies) >= 2 else [message]}
        if method == "message.ack":
            replies.append(params)
            if len(replies) == 1:
                return {"ok": False, "acknowledged": False}
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
        logger=logger,  # type: ignore[arg-type]
    )
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(replies) < 2:
        time.sleep(0.01)
    carrier.stop()

    assert len(replies) == 2
    assert replies[0] == replies[1]
    assert server.prompts == [_prompt(message, sender="adapter:lark:test")]
    transitions = [
        fields["toState"]
        for _level, event, fields in logger.entries
        if event == "worker.carrier.transition"
    ]
    assert transitions == ["FETCHED", "TURN_STARTED", "FINAL_OBSERVED", "SETTLED"]


def test_codex_interactive_carrier_recovers_final_after_process_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "turn-rebuild"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "rebuild final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    state_path = tmp_path / "carrier.sqlite3"
    message = {
        "messageId": "m-rebuild",
        "conversationId": "c-rebuild",
        "from": "adapter:lark:test",
        "intent": "request",
        "message": "incident question",
    }
    replies: list[dict[str, object]] = []
    first_attempt = threading.Event()
    settled = threading.Event()

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [] if settled.is_set() else [message]}
        if method == "message.ack":
            replies.append(params)
            if len(replies) == 1:
                first_attempt.set()
                return {"ok": False, "acknowledged": False}
            settled.set()
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    first = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=tmp_path,
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        poll_seconds=0.01,
        state_path=state_path,
        settlement_retry_seconds=5.0,
    )
    _delay_actor_turn_projection(first, monkeypatch)
    recorded = threading.Event()
    store = first._store
    assert store is not None
    original_record = store.record_settlement_attempts

    def record_and_stop(actor: str, message_id: str, attempts: int) -> None:
        original_record(actor, message_id, attempts)
        if message_id == "m-rebuild" and attempts == 1:
            first._stop.set()
            recorded.set()

    monkeypatch.setattr(store, "record_settlement_attempts", record_and_stop)
    expected = ("FINAL_OBSERVED", "turn-rebuild", "rebuild final", 1)
    try:
        first.start()
        assert first_attempt.wait(timeout=2.0)
        assert recorded.wait(timeout=2.0)
    finally:
        first.stop()
    with closing(sqlite3.connect(state_path)) as database:
        row = database.execute(
            """
            SELECT state, turn_id, final_output, settlement_attempts
            FROM codex_interactive_delivery WHERE message_id = 'm-rebuild'
            """
        ).fetchone()
    assert row == expected

    second = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=tmp_path,
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        poll_seconds=0.01,
        state_path=state_path,
    )
    try:
        second.start()
        assert settled.wait(timeout=2.0)
    finally:
        second.stop()

    assert server.prompts == [
        _prompt(message, sender="adapter:lark:test")
    ]
    assert len(replies) == 2
    assert replies[0] == replies[1] == {
        "actor": ACTOR,
        "sessionRef": THREAD_ID,
        "messageId": "m-rebuild",
    }
    assert state_path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(state_path)) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM codex_interactive_delivery"
        ).fetchone() == (0,)


def test_codex_interactive_carrier_reconciles_persisted_turn_after_rebuild(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "carrier.sqlite3"
    delivery = HarnessDelivery(
        delivery_id="m-persisted-turn",
        conversation_id="c-persisted-turn",
        sender="adapter:lark:test",
        recipient=ACTOR,
        message="not duplicated",
    )
    store = CodexCarrierStore(state_path)
    store.record_fetched(ACTOR, THREAD_ID, delivery, "request")
    store.record_turn_started(ACTOR, delivery.delivery_id, "turn-persisted")
    store.close()

    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.turn_starts = 0

        def start_turn(self, prompt: str) -> str:
            self.turn_starts += 1
            raise AssertionError("persisted turn must not be started again")

        def read_turn(self, turn_id: str) -> dict[str, object]:
            assert turn_id == "turn-persisted"
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "reconciled persisted final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    settled = threading.Event()
    replies: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            # The durable correlation outlives pending-list visibility/TTL.
            return {"messages": []}
        if method == "message.ack":
            replies.append(params)
            settled.set()
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=tmp_path,
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        poll_seconds=0.01,
        state_path=state_path,
    )
    carrier.start()
    assert settled.wait(timeout=2.0)
    carrier.stop()

    assert server.turn_starts == 0
    assert replies == [
        {
            "actor": ACTOR,
            "sessionRef": THREAD_ID,
            "messageId": "m-persisted-turn",
        }
    ]


def test_codex_interactive_carrier_logs_rpc_error_and_retries_same_final(
    tmp_path: Path,
) -> None:
    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "turn-rpc"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "same final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    logger = RecordingLogger()
    message = {
        "messageId": "m-rpc",
        "conversationId": "c-rpc",
        "from": "adapter:lark:test",
        "intent": "request",
        "message": "rpc question",
    }
    attempts = 0
    replies: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [] if replies else [message]}
        if method == "message.ack":
            attempts += 1
            if attempts == 1:
                raise ConnectionError("reply RPC disconnected")
            replies.append(params)
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
        logger=logger,  # type: ignore[arg-type]
    )
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not replies:
        time.sleep(0.01)
    carrier.stop()

    assert attempts == 2
    assert server.prompts == [_prompt(message, sender="adapter:lark:test")]
    assert replies == [
        {
            "actor": ACTOR,
            "sessionRef": THREAD_ID,
            "messageId": "m-rpc",
        }
    ]
    assert any(
        level == "warn"
        and event == "worker.carrier.settlement.pending"
        and fields["error"] == "reply RPC disconnected"
        for level, event, fields in logger.entries
    )


def test_codex_interactive_carrier_reconciles_captured_completed_turn(
    tmp_path: Path,
) -> None:
    """Stop joins the pump once the carrier's reconciled final released it.

    Runs 44050/44128 shard 4 failed twice with "Codex interactive app-server
    did not stop within 2.0s" plus 3 leaked loop fds, and GitHub run
    45874350431 failed the same way after the budget was raised to 30.0s.
    The cause was not join timing.  When the carrier actor published
    FINAL_OBSERVED before ``_settle_finals`` looked, settlement acked and
    dropped the pump's turn client before ``_drain_carrier_effects`` derived
    ``SupplyFinalRequested``, so the reconciled final never reached
    ``receive_response``.  The pump kept retrying its failing read, never
    went idle, and no join budget could end it; raising the budget from 2s
    to 30s did not address that.  Settlement now hands the final to the
    client as it drops it, pinned deterministically by
    ``test_codex_interactive_carrier_settlement_releases_pump_observer``.
    The wait below targets the pump's published turn result (``_pump.idle``)
    before stopping; its bounds are unchanged.  Earlier timing notes:
    docs/notes/codex-settlement-timing-2026-11-03.md.
    """

    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "turn-captured"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            if threading.current_thread().name.startswith("asyncio_"):
                raise codex.CodexAppServerRpcError("pump completion missed")
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "question"}],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "checking",
                    },
                    {"type": "commandExecution", "status": "completed"},
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "captured substantive final",
                    },
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    message = {
        "messageId": "m-captured",
        "conversationId": "c-captured",
        "from": "adapter:lark:test",
        "intent": "request",
        "message": "question",
    }
    replies: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [] if replies else [message]}
        if method == "message.ack":
            replies.append(params)
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    carrier._pump._stop_timeout_seconds = _PUMP_STOP_BUDGET_SECONDS
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not replies:
        time.sleep(0.01)
    idle_deadline = time.monotonic() + _PUMP_IDLE_BUDGET_SECONDS
    while time.monotonic() < idle_deadline and not carrier._pump.idle:
        time.sleep(0.01)
    carrier.stop()

    assert carrier._pump.idle
    assert server.prompts == [_prompt(message, sender="adapter:lark:test")]
    assert replies == [
        {
            "actor": ACTOR,
            "sessionRef": THREAD_ID,
            "messageId": "m-captured",
        }
    ]


def _wait_until(predicate, budget: float) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _stage(carrier: codex.CodexInteractiveCarrier, delivery_id: str) -> str | None:
    state = carrier._carrier_runtime.snapshot(delivery_id)
    return None if state is None else state.stage


def _drive_to_final_observed(
    carrier: codex.CodexInteractiveCarrier, delivery: HarnessDelivery
) -> None:
    """Run one delivery to FINAL_OBSERVED from the test thread, no poll thread.

    Each step is one the carrier poll thread performs, in its order:
    stage the fetched fact, drain the enqueue effect into the pump, wait for
    the pump's turn start, reconcile ``thread/read``, wait for the actor.
    """

    delivery_id = delivery.delivery_id
    assert carrier._stage_carrier_fetched(delivery, "request")
    assert _wait_until(lambda: _stage(carrier, delivery_id) == "FETCHED", 2.0)
    carrier._drain_carrier_effects()
    assert _wait_until(lambda: _stage(carrier, delivery_id) == "TURN_STARTED", 2.0)
    carrier._reconcile_inflight_turns()
    assert _wait_until(lambda: _stage(carrier, delivery_id) == "FINAL_OBSERVED", 2.0)


def _stop_unpolled(carrier: codex.CodexInteractiveCarrier) -> None:
    carrier._stop.set()
    carrier._thread.start()
    carrier.stop()


def test_codex_interactive_carrier_settlement_releases_pump_observer(
    tmp_path: Path,
) -> None:
    """Settling a reconciled final hands that final to the pump's observer.

    Root cause of the reconciles-captured-completed-turn stop failures:
    ``_apply_inbox_list`` submits ``CarrierFinalObserved``, then
    ``_settle_finals`` acks and drops the turn client, and only afterwards
    ``_drain_carrier_effects`` derives ``SupplyFinalRequested``.  When the
    actor publishes FINAL_OBSERVED inside that window the supply finds no
    client, ``receive_response`` keeps retrying its failing read, the pump
    never goes idle and ``stop()`` cannot join it.  This drives that order.
    """

    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "turn-release"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            if threading.current_thread().name.startswith("asyncio_"):
                raise codex.CodexAppServerRpcError("pump completion missed")
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "reconciled final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    replies: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"ok": True, "messages": []}
        if method == "message.ack":
            replies.append(params)
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=tmp_path,
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    delivery = HarnessDelivery(
        delivery_id="m-release",
        conversation_id="c-release",
        sender="adapter:lark:test",
        recipient=ACTOR,
        message="question",
    )
    try:
        _drive_to_final_observed(carrier, delivery)
        assert not carrier._pump.idle
        carrier._settle_finals()
        carrier._drain_carrier_effects()
        assert _wait_until(lambda: carrier._pump.idle, _PUMP_IDLE_BUDGET_SECONDS)
    finally:
        _stop_unpolled(carrier)

    assert len(server.prompts) == 1
    assert replies == [
        {"actor": ACTOR, "sessionRef": THREAD_ID, "messageId": "m-release"}
    ]


def test_codex_interactive_carrier_deferred_settlement_waits_for_retry_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deferred settlement is not retried before its retry time.

    Root cause of the recovers-final-after-process-rebuild row loss: the
    retry time travels to the actor as ``CarrierSettlementDeferred`` and
    ``_settle_finals`` reads it back from the projection.  The next poll
    step runs at once (delay 0.0); while the actor has not applied the fact
    the projection still says "due", so the carrier acks a second time
    milliseconds later, settles and deletes the row the rebuild should find.
    The actor is held inside that fact here to open the window.
    """

    class FakeServer:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            return "turn-deferred"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "deferred final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    acks: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"ok": True, "messages": []}
        if method == "message.ack":
            acks.append(params)
            if len(acks) == 1:
                return {"ok": False, "acknowledged": False}
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        FakeServer(),
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=tmp_path,
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
        settlement_retry_seconds=5.0,
    )
    store = carrier._store
    assert store is not None
    original_record = store.record_settlement_attempts
    entered = threading.Event()
    release = threading.Event()

    def held_record(actor: str, message_id: str, attempts: int) -> None:
        entered.set()
        release.wait(_PUMP_IDLE_BUDGET_SECONDS)
        original_record(actor, message_id, attempts)

    delivery = HarnessDelivery(
        delivery_id="m-deferred",
        conversation_id="c-deferred",
        sender="adapter:lark:test",
        recipient=ACTOR,
        message="question",
    )
    try:
        _drive_to_final_observed(carrier, delivery)
        monkeypatch.setattr(store, "record_settlement_attempts", held_record)
        carrier._settle_finals()
        assert entered.wait(2.0)
        carrier._settle_finals()
        acks_before_retry_time = len(acks)
    finally:
        release.set()
        _stop_unpolled(carrier)

    assert acks_before_retry_time == 1


def test_m4_codex_interactive_settlement_regression(tmp_path: Path) -> None:
    test_codex_interactive_carrier_retries_unsettled_final_without_second_turn(tmp_path)


def test_m4_codex_interactive_rebuild_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_codex_interactive_carrier_recovers_final_after_process_rebuild(
        tmp_path, monkeypatch
    )
