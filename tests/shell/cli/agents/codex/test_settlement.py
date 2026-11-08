"""cli_codex_interactive: settlement scenarios."""

from __future__ import annotations
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
    expected = ("FINAL_OBSERVED", "turn-rebuild", "rebuild final", 1)
    row = None
    try:
        first.start()
        assert first_attempt.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while row != expected and time.monotonic() < deadline:
            with sqlite3.connect(state_path) as database:
                row = database.execute(
                    """
                    SELECT state, turn_id, final_output, settlement_attempts
                    FROM codex_interactive_delivery WHERE message_id = 'm-rebuild'
                    """
                ).fetchone()
            if row != expected:
                time.sleep(0.01)
    finally:
        first.stop()
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
    with sqlite3.connect(state_path) as database:
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
    """Stop joins a pump parked at its I/O queue, not mid RPC-retry cycle.

    Runs 44050/44128 shard 4 failed twice with "Codex interactive app-server
    did not stop within 2.0s" plus 3 leaked loop fds: the carrier's reconcile
    read settles and acks while the pump is still inside its 0.5s RPC-retry
    cycle, so ``stop()``'s 2.0s join had to absorb the rest of that cycle;
    on loaded shards (3-CPU cgroup, load ~8-11) it expired. The wait below
    targets the real condition — the pump published the turn result
    (``_pump.idle``) — before stopping, leaving the join only scheduler hops.
    The 30s budget follows the heartbeat-bind round: no product constant
    sizes "one retry cycle plus loaded-host scheduling"; a wedged pump still
    fails, bounded. Evidence: docs/notes/codex-settlement-timing-2026-11-03.md.
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


def test_m4_codex_interactive_settlement_regression(tmp_path: Path) -> None:
    test_codex_interactive_carrier_retries_unsettled_final_without_second_turn(tmp_path)


def test_m4_codex_interactive_rebuild_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_codex_interactive_carrier_recovers_final_after_process_rebuild(
        tmp_path, monkeypatch
    )
