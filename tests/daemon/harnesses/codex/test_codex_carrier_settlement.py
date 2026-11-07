"""Settlement classification and per-row isolation for the Codex carrier.

The daemon shapes stubbed here are the real ones, read off dev 6dc3fa53:

- ``message.ack`` on a row that is gone (already consumed, pruned, or never
  existed) never raises; it returns ``{"ok": False, "acknowledged": False,
  "code": "MESSAGE_ACK_UNAVAILABLE"}`` (daemon/application.py message.ack
  handler + inbox/service.py ack()).
- The carrier never replies: ``message.settle_hint`` decides, ``ack`` acks and
  ``keep`` is remembered (wp5c.md section 3.3).
- Transport failures reach the carrier as ``TransientDaemonError``
  subclasses (contracts/ipc_errors.py), never as bare codes.
- ``message.status`` with a messageId answers ``state`` as one of
  ``fetched`` / ``expired`` / ``pending`` / ``unknown``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses import codex
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore

THREAD_ID = "thread-11111111-2222-3333-4444-555555555555"
ACTOR = "agent:op:node-1:codex-tui"


class RecordingLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.entries.append(("info", event, fields))

    def warn(self, event: str, **fields: object) -> None:
        self.entries.append(("warn", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.entries.append(("error", event, fields))

    def events(self, name: str) -> list[tuple[str, dict[str, object]]]:
        return [
            (level, fields)
            for level, event, fields in self.entries
            if event == name
        ]


class TurnServer:
    """One-turn-per-prompt fake Codex app server."""

    pid = 123

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def start_turn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"turn-{len(self.prompts)}"

    def read_turn(self, turn_id: str) -> dict[str, object]:
        return {
            "id": turn_id,
            "status": "completed",
            "items": [
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": f"final-{turn_id}",
                }
            ],
        }

    def interrupt_turn(self, turn_id: str) -> None:
        return None


def _batch_response(rows: list[dict[str, object]]) -> dict[str, object]:
    """The daemon's inbox-batch-v1 answer for this fake daemon's live rows."""

    return {
        "ok": True,
        "schemaVersion": 1,
        "format": "inbox-batch-v1",
        "cursor": "cursor-settlement",
        "daemonEpoch": "e1",
        "actor": ACTOR,
        "itemCount": len(rows),
        "totalBodyBytes": 0,
        "inlineLimitBytes": 16384,
        "items": rows,
        "delivery": {
            "mode": "inline",
            "text": (
                "[Fake daemon inbox batch]\n"
                + "\n".join(str(row["messageId"]) for row in rows)
            )
            if rows
            else "",
            "spillPath": None,
            "spillBytes": None,
            "spillSha256": None,
        },
    }


def _hint(method: str, params: dict[str, object]) -> dict[str, object] | None:
    """The turn-report and settle-hint answers every fake daemon shares."""

    if method == "message.settle_hint":
        return {"ok": True, "hint": "ack", "pending": True}
    if method in {"session.turn.started", "session.turn.ended"}:
        return {"ok": True}
    return None


def _message(message_id: str, *, intent: str = "request") -> dict[str, object]:
    return {
        "messageId": message_id,
        "conversationId": f"c-{message_id}",
        "from": "requester",
        "intent": intent,
        "message": f"prompt-{message_id}",
    }


def _carrier(
    server: object,
    daemon_request: Any,
    state_path: Path,
    logger: RecordingLogger,
) -> codex.CodexInteractiveCarrier:
    return codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-settlement"),
        command=["codex", "--remote"],
        daemon_request=daemon_request,
        logger=logger,
        state_path=state_path,
        poll_seconds=0.01,
        settlement_retry_seconds=0.01,
    )


@pytest.fixture
def run_carrier() -> Any:
    """Start carriers and guarantee they stop even when an assert fails.

    A failed assertion that skips ``carrier.stop()`` leaves the non-daemon
    pump thread alive and hangs the pytest process at exit, which turns an
    honest red run into a timeout.
    """

    carriers: list[codex.CodexInteractiveCarrier] = []

    def start(carrier: codex.CodexInteractiveCarrier) -> codex.CodexInteractiveCarrier:
        carriers.append(carrier)
        carrier.start()
        return carrier

    yield start
    for carrier in carriers:
        carrier.stop()


# These tests assert terminal classification, not a 3s throughput contract.
# One final crosses roughly five durable transitions; the store already permits
# a 5s SQLite lock wait per transition. Give that pipeline 5*5s + scheduling
# margin, without changing any production retry/IPC deadline.
SETTLEMENT_WATCHDOG_SECONDS = 30.0


def _wait(predicate: Any, timeout: float = SETTLEMENT_WATCHDOG_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())  # a scheduling pause must not hide a completed fact


def _store_rows(state_path: Path, carrier: Any = None) -> list[tuple[str, str, int]]:
    def read_rows(database):
        with closing(database.cursor()) as cursor:
            cursor.execute(
                """
                SELECT message_id, state, settlement_attempts
                FROM codex_interactive_delivery ORDER BY message_id
                """
            )
            return [tuple(row) for row in cursor]

    if carrier is not None:
        store = carrier._store
        assert store is not None
        assert store.path.resolve() == state_path.resolve()
        # Observe live writes through their owner lock, rather than introducing
        # an unrelated SQLite reader racing a rollback-journal commit.
        with store._lock:
            return read_rows(store._db)
    # sqlite3.Connection's context manager commits/rolls back; it does NOT close.
    # Preserve independent durable verification after the carrier has stopped.
    with closing(sqlite3.connect(state_path)) as database:
        return read_rows(database)


def test_functional_wait_observes_completion_after_a_scheduling_pause(monkeypatch):
    import sys
    from types import SimpleNamespace

    ticks = iter([0.0, 0.0, 2.0])
    ready = []
    monkeypatch.setattr(sys.modules[__name__], "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda _delay: ready.append(True),
    ))
    assert _wait(lambda: ready, timeout=1.0)


def test_functional_wait_still_rejects_an_absent_postcondition():
    assert not _wait(lambda: False, timeout=0.0)


def test_live_store_observer_does_not_open_a_competing_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "carrier.sqlite3"
    store = CodexCarrierStore(path)
    store.record_fetched(ACTOR, THREAD_ID, HarnessDelivery(
        delivery_id="observer-control", conversation_id="conversation",
        sender="sender", recipient=ACTOR, message="body",
    ), "request")
    connect = sqlite3.connect
    connections = []

    def immediate_connect(*args, **kwargs):
        db = connect(*args, timeout=0, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", immediate_connect)
    try:
        with store._lock:
            store._db.execute("BEGIN EXCLUSIVE")
            assert _store_rows(path, SimpleNamespace(_store=store)) == [
                ("observer-control", "FETCHED", 0),
            ]
    finally:
        store._db.rollback()
        store.close()
        for db in connections:
            db.close()
    assert not connections


def test_store_observer_closes_connection_even_with_a_pinned_query_error(tmp_path, monkeypatch):
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table") as caught:
            _store_rows(tmp_path / "empty.sqlite3")
        for db in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
        assert caught.value.__traceback__ is not None
    finally:
        for db in connections:
            db.close()


def test_ack_target_already_settled_elsewhere_is_terminal(tmp_path: Path, run_carrier: Any) -> None:
    """A reply-intent final whose row the model already acked settles once.

    The daemon answers acknowledged=False + MESSAGE_ACK_UNAVAILABLE (the row
    is consumed or gone); the carrier must go terminal with a superseded
    event instead of retrying forever.
    """

    message = _message("m-acked", intent="reply")
    logger = RecordingLogger()
    acks: list[dict[str, object]] = []
    status_queries: list[dict[str, object]] = []
    staged = threading.Event()

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "session.turn.started":
            staged.set()
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            # The model acked the row inside its turn: once fetched, the
            # observation listing no longer shows it.
            return {"messages": [] if staged.is_set() else [message]}
        if method == "message.pending.batch":
            rows = [] if staged.is_set() else [message]
            return _batch_response(rows)
        if method == "message.ack":
            acks.append(params)
            return {
                "ok": False,
                "messageId": params["messageId"],
                "acknowledged": False,
                "code": ipc_errors.MESSAGE_ACK_UNAVAILABLE,
            }
        if method == "message.status":
            status_queries.append(params)
            return {"ok": True, "messageId": params["messageId"], "state": "fetched"}
        raise AssertionError(method)

    state_path = tmp_path / "carrier.sqlite3"
    carrier = _carrier(TurnServer(), daemon_request, state_path, logger)
    run_carrier(carrier)
    assert _wait(lambda: logger.events("worker.carrier.settlement.superseded")), logger.entries
    assert _wait(lambda: not _store_rows(state_path, carrier)), logger.entries
    time.sleep(0.1)
    carrier.stop()

    assert _store_rows(state_path) == []
    assert len(acks) == 1, acks
    assert status_queries == [{"actor": ACTOR, "sessionRef": THREAD_ID, "messageId": "m-acked"}]
    superseded = logger.events("worker.carrier.settlement.superseded")
    assert len(superseded) == 1
    level, fields = superseded[0]
    assert level == "warn"
    assert fields["messageId"] == "m-acked"
    assert fields["stage"] == "message.ack"
    assert fields["daemonCode"] == ipc_errors.MESSAGE_ACK_UNAVAILABLE
    assert fields["deliveryStatus"] == "fetched"
    assert not logger.events("worker.carrier.settlement.pending")


def test_ack_code_without_delivery_evidence_keeps_retrying(
    tmp_path: Path, run_carrier: Any
) -> None:
    """MESSAGE_ACK_UNAVAILABLE alone is ambiguous and never terminal."""

    message = _message("m-ack-ambiguous", intent="reply")
    logger = RecordingLogger()
    ack_calls = 0

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal ack_calls
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [message]}
        if method == "message.pending.batch":
            return _batch_response([message])
        if method == "message.ack":
            ack_calls += 1
            return {
                "ok": False,
                "messageId": params["messageId"],
                "acknowledged": False,
                "code": ipc_errors.MESSAGE_ACK_UNAVAILABLE,
            }
        if method == "message.status":
            return {"ok": True, "messageId": params["messageId"], "state": "unknown"}
        raise AssertionError(method)

    state_path = tmp_path / "carrier.sqlite3"
    carrier = _carrier(TurnServer(), daemon_request, state_path, logger)
    run_carrier(carrier)
    assert _wait(lambda: ack_calls >= 2)
    carrier.stop()

    assert not logger.events("worker.carrier.settlement.superseded")
    pending = logger.events("worker.carrier.settlement.pending")
    assert pending
    rows = _store_rows(state_path)
    assert rows and rows[0][0:2] == ("m-ack-ambiguous", "FINAL_OBSERVED")


def test_settle_hint_failure_keeps_retrying_without_replying(
    tmp_path: Path, run_carrier: Any
) -> None:
    """A hint that cannot be had is a retry, never an ack and never a reply."""

    message = _message("m-nohint")
    logger = RecordingLogger()
    hints = 0
    other: list[str] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal hints
        if method in {"session.refresh", "session.heartbeat", "session.turn.started", "session.turn.ended"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [message]}
        if method == "message.pending.batch":
            return _batch_response([message])
        if method == "message.settle_hint":
            hints += 1
            raise ipc_errors.IpcTimeoutError("authority timeout")
        other.append(method)
        raise AssertionError(method)

    state_path = tmp_path / "carrier.sqlite3"
    carrier = _carrier(TurnServer(), daemon_request, state_path, logger)
    run_carrier(carrier)
    assert _wait(lambda: hints >= 2 and logger.events("worker.carrier.settlement.pending"))
    carrier.stop()

    assert other == []
    assert logger.events("worker.carrier.settlement.pending")
    assert not logger.events("worker.carrier.settlement.superseded")
    rows = _store_rows(state_path)
    assert rows and rows[0][0:2] == ("m-nohint", "FINAL_OBSERVED")


def test_transient_daemon_errors_never_settle_terminally(tmp_path: Path, run_carrier: Any) -> None:
    """Negative rule: unreachable / timing-out daemons must keep retrying."""

    message = _message("m-transient", intent="reply")
    logger = RecordingLogger()
    settle_calls = 0

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal settle_calls
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [message]}
        if method == "message.pending.batch":
            return _batch_response([message])
        if method == "message.ack":
            settle_calls += 1
            if settle_calls % 2:
                raise ipc_errors.DaemonUnavailableError(
                    "cannot connect to the Harness daemon socket"
                )
            raise ipc_errors.IpcTimeoutError(
                "timed out after 10s waiting for the Harness daemon"
            )
        raise AssertionError(method)

    state_path = tmp_path / "carrier.sqlite3"
    carrier = _carrier(TurnServer(), daemon_request, state_path, logger)
    run_carrier(carrier)
    assert _wait(lambda: settle_calls >= 3)
    carrier.stop()

    assert not logger.events("worker.carrier.settlement.superseded")
    pending = logger.events("worker.carrier.settlement.pending")
    assert pending
    rows = _store_rows(state_path)
    assert rows and rows[0][0:2] == ("m-transient", "FINAL_OBSERVED")
    assert rows[0][2] >= 2
    # The retry must walk the existing backoff: attempts climb one by one
    # and the next-attempt delay never shrinks and never drops to zero
    # (no immediate retry), per the CHECKPOINT ruling.
    attempts = [int(fields["attempt"]) for _, fields in pending]
    assert attempts == list(range(1, len(attempts) + 1))
    delays = [float(fields["retryInSeconds"]) for _, fields in pending]
    assert all(delay >= 0.01 for delay in delays)
    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


def test_one_poisoned_settlement_row_does_not_block_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_carrier: Any
) -> None:
    """Per-row isolation in _settle_finals.

    m-poison's settlement journalling raises (the store row disappeared,
    the real shape record_remote_settled refuses); m-clean on the same
    carrier must still settle.
    """

    messages = [_message("m-poison"), _message("m-clean")]
    logger = RecordingLogger()
    replied: list[str] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            remaining = [
                item
                for item in messages
                if str(item["messageId"]) not in replied
            ]
            return {"messages": remaining}
        if method == "message.pending.batch":
            return _batch_response(
                [
                    item
                    for item in messages
                    if str(item["messageId"]) not in replied
                ]
            )
        if method == "message.ack":
            replied.append(str(params["messageId"]))
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    state_path = tmp_path / "carrier.sqlite3"
    carrier = _carrier(TurnServer(), daemon_request, state_path, logger)
    store = carrier._store
    assert store is not None
    original = store.record_remote_settled

    def poisoned(actor: str, message_id: str) -> None:
        if message_id == "m-poison":
            raise RuntimeError("carrier delivery disappeared after remote settlement")
        original(actor, message_id)

    monkeypatch.setattr(store, "record_remote_settled", poisoned)
    run_carrier(carrier)
    assert _wait(lambda: "m-clean" in replied)
    assert _wait(
        lambda: any(
            fields.get("stage") == "settlement"
            and fields.get("messageId") == "m-poison"
            for _, fields in logger.events("worker.carrier.error")
        )
    )
    time.sleep(0.1)
    carrier.stop()

    assert "m-clean" in replied
    rows = _store_rows(state_path)
    assert all(row[0] != "m-clean" for row in rows)


def test_already_correlated_row_does_not_freeze_the_poll(tmp_path: Path, run_carrier: Any) -> None:
    """A mid-turn delivery reappearing in the fetched listing is skipped.

    The incident shape: one delivery is durably TURN_STARTED, a new message
    arrives, and the staging loop used to re-stage the correlated row --
    the store's already-correlated guard then aborted the whole poll every
    1-5s.  The new message must reach its turn and settle instead.
    """

    state_path = tmp_path / "carrier.sqlite3"
    stuck = HarnessDelivery(
        delivery_id="m-stuck",
        conversation_id="c-m-stuck",
        sender="requester",
        recipient=ACTOR,
        message="prompt-m-stuck",
    )
    seed = CodexCarrierStore(state_path)
    seed.record_fetched(ACTOR, THREAD_ID, stuck, "request")
    seed.record_turn_started(ACTOR, "m-stuck", "turn-stuck")
    seed.close()

    class StuckAwareServer(TurnServer):
        def start_turn(self, prompt: str) -> str:
            assert prompt != "prompt-m-stuck", "correlated row must not restart"
            return super().start_turn(prompt)

        def read_turn(self, turn_id: str) -> dict[str, object]:
            if turn_id == "turn-stuck":
                return {"id": turn_id, "status": "inProgress", "items": []}
            return super().read_turn(turn_id)

    fresh = _message("m-new")
    logger = RecordingLogger()
    replied: list[str] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            listing = [_message("m-stuck")]
            if "m-new" not in replied:
                listing.append(fresh)
            return {"messages": listing}
        if method == "message.pending.batch":
            listing = [_message("m-stuck")]
            if "m-new" not in replied:
                listing.append(fresh)
            return _batch_response(listing)
        if method == "message.ack":
            replied.append(str(params["messageId"]))
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = _carrier(StuckAwareServer(), daemon_request, state_path, logger)
    run_carrier(carrier)
    assert _wait(lambda: "m-new" in replied)
    carrier.stop()

    poll_errors = [
        fields
        for _, fields in logger.events("worker.carrier.error")
        if "already has durable turn/final correlation" in str(fields.get("error"))
    ]
    assert poll_errors == []
    # The batch includes every pending daemon row and settles each on its own
    # hint, so an already-correlated row may be acked; what must never happen
    # is restarting it as a native turn (StuckAwareServer above enforces that).
    assert "m-new" in replied


def test_poisoned_attempts_1024_rows_converge_and_new_work_runs(tmp_path: Path, run_carrier: Any) -> None:
    """The recovery acceptance: existing poison rows self-heal on upgrade.

    Start from the observed field state -- FINAL_OBSERVED rows with
    settlement_attempts=1024 whose targets were acked long ago -- and show
    the fixed carrier settles them terminally without manual database
    surgery while a newly FETCHED request still reaches its turn.
    """

    state_path = tmp_path / "carrier.sqlite3"
    seed = CodexCarrierStore(state_path)
    for index in range(3):
        message_id = f"m-poison-{index}"
        delivery = HarnessDelivery(
            delivery_id=message_id,
            conversation_id=f"c-{message_id}",
            sender="requester",
            recipient=ACTOR,
            message=f"prompt-{message_id}",
        )
        seed.record_fetched(ACTOR, THREAD_ID, delivery, "reply")
        seed.record_turn_started(ACTOR, message_id, f"turn-{message_id}")
        seed.record_final(ACTOR, message_id, f"final-{message_id}")
        seed.record_settlement_attempts(ACTOR, message_id, 1024)
    seed.close()

    fresh = _message("m-fresh")
    logger = RecordingLogger()
    replied: list[str] = []

    class SeededServer(TurnServer):
        def read_turn(self, turn_id: str) -> dict[str, object]:
            if turn_id.startswith("turn-m-poison"):
                return {"id": turn_id, "status": "inProgress", "items": []}
            return super().read_turn(turn_id)

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [] if "m-fresh" in replied else [fresh]}
        if method == "message.pending.batch":
            return _batch_response([] if "m-fresh" in replied else [fresh])
        if method == "message.ack":
            if str(params["messageId"]).startswith("m-poison"):
                return {
                    "ok": False,
                    "messageId": params["messageId"],
                    "acknowledged": False,
                    "code": ipc_errors.MESSAGE_ACK_UNAVAILABLE,
                }
            replied.append(str(params["messageId"]))
            return {"ok": True, "acknowledged": True}
        if method == "message.status":
            return {"ok": True, "messageId": params["messageId"], "state": "fetched"}
        raise AssertionError(method)

    carrier = _carrier(SeededServer(), daemon_request, state_path, logger)
    run_carrier(carrier)
    assert _wait(lambda: "m-fresh" in replied)
    assert _wait(
        lambda: {
            row[0] for row in _store_rows(state_path, carrier)
            if row[1] == "STATUS_UNKNOWN"
        }
        == {"m-poison-0", "m-poison-1", "m-poison-2"}
    ), logger.entries
    time.sleep(0.1)
    carrier.stop()

    rows = _store_rows(state_path)
    assert [(row[0], row[1]) for row in rows] == [
        ("m-poison-0", "STATUS_UNKNOWN"),
        ("m-poison-1", "STATUS_UNKNOWN"),
        ("m-poison-2", "STATUS_UNKNOWN"),
    ]
    assert not logger.events("worker.carrier.settlement.superseded")
    terminal = logger.events("worker.carrier.settlement.status_unknown")
    assert {fields["messageId"] for _, fields in terminal} == {
        "m-poison-0",
        "m-poison-1",
        "m-poison-2",
    }
    assert replied == ["m-fresh"]


def test_status_query_budget_terminalises_without_further_queries(
    tmp_path: Path, run_carrier: Any
) -> None:
    """A status endpoint that never produces records must not be polled forever."""

    message = _message("m-status-storm", intent="reply")
    logger = RecordingLogger()
    status_queries: list[dict[str, object]] = []
    served = {"value": False}

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "session.turn.started":
            served["value"] = True
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {"messages": [] if served["value"] else [message]}
        if method == "message.pending.batch":
            rows = [] if served["value"] else [message]
            return _batch_response(rows)
        if method == "message.ack":
            return {
                "ok": False,
                "messageId": params["messageId"],
                "acknowledged": False,
                "code": ipc_errors.MESSAGE_ACK_UNAVAILABLE,
            }
        if method == "message.status":
            status_queries.append(params)
            return {"ok": True, "messageId": params["messageId"], "state": "unknown"}
        raise AssertionError(method)

    state_path = tmp_path / "carrier.sqlite3"
    server = TurnServer()
    carrier = _carrier(server, daemon_request, state_path, logger)
    carrier.status_query_limit = 3
    run_carrier(carrier)
    assert _wait(lambda: logger.events("worker.carrier.settlement.status_unknown"))
    assert _wait(
        lambda: any(
            row[0] == "m-status-storm" and row[1] == "STATUS_UNKNOWN"
            for row in _store_rows(state_path, carrier)
        )
    )
    time.sleep(0.1)
    carrier.stop()

    assert len(status_queries) == 3
    terminal = logger.events("worker.carrier.settlement.status_unknown")
    assert len(terminal) == 1
    _level, fields = terminal[0]
    assert fields["messageId"] == "m-status-storm"
    assert fields["stage"] == "message.status"
    assert fields["attempts"] == 3
    assert fields["limit"] == 3
    rows = _store_rows(state_path)
    assert rows == [("m-status-storm", "STATUS_UNKNOWN", 3)]

    served["value"] = False
    time.sleep(0.2)
    assert len(server.prompts) == 1


def test_status_attempt_is_persisted_before_the_query(
    tmp_path: Path, run_carrier: Any
) -> None:
    """A crash after the query must still leave a durable attempt."""

    state_path = tmp_path / "carrier.sqlite3"
    seed = CodexCarrierStore(state_path)
    message_id = "m-write-ahead"
    delivery = HarnessDelivery(
        delivery_id=message_id,
        conversation_id=f"c-{message_id}",
        sender="requester",
        recipient=ACTOR,
        message=f"prompt-{message_id}",
    )
    seed.record_fetched(ACTOR, THREAD_ID, delivery, "reply")
    seed.record_turn_started(ACTOR, message_id, f"turn-{message_id}")
    seed.record_final(ACTOR, message_id, f"final-{message_id}")
    seed.record_settlement_attempts(ACTOR, message_id, 7)
    seed.close()

    logger = RecordingLogger()
    seen_attempts: list[int] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method in {"message.pending.list", "message.pending.batch"}:
            return {"messages": []} if method.endswith("list") else _batch_response([])
        if method == "message.ack":
            return {
                "ok": False,
                "messageId": params["messageId"],
                "acknowledged": False,
                "code": ipc_errors.MESSAGE_ACK_UNAVAILABLE,
            }
        if method == "message.status":
            with closing(sqlite3.connect(state_path)) as db:
                seen_attempts.append(
                    int(
                        db.execute(
                            "SELECT settlement_attempts "
                            "FROM codex_interactive_delivery WHERE message_id = ?",
                            (message_id,),
                        ).fetchone()[0]
                    )
                )
            return {"ok": True, "messageId": params["messageId"], "state": "unknown"}
        raise AssertionError(method)

    carrier = _carrier(TurnServer(), daemon_request, state_path, logger)
    carrier.status_query_limit = 8
    run_carrier(carrier)
    assert _wait(lambda: logger.events("worker.carrier.settlement.status_unknown"))
    carrier.stop()

    assert seen_attempts
    assert all(attempts >= 8 for attempts in seen_attempts)


def test_status_unknown_fence_emits_once() -> None:
    """Deleting the settled fence mutation must make this test fail."""

    from types import SimpleNamespace
    import threading
    from hyprial.daemon.impl.harnesses.codex.settlement import (
        _CarrierSettlementMixin,
    )

    class Fake(_CarrierSettlementMixin):
        def __init__(self) -> None:
            self._carrier_settled_pending = set()
            self._carrier_clients: dict[str, object] = {}
            self._carrier_io_lock = threading.RLock()
            self.events: list[tuple[str, dict[str, object]]] = []
            self.facts: list[object] = []

        def _log(self, _level: str, event: str, **fields: object) -> None:
            self.events.append((event, fields))

        def _submit_carrier_fact(self, fact: object) -> None:
            self.facts.append(fact)

    fake = Fake()
    state = SimpleNamespace(
        generation=0,
        delivery=SimpleNamespace(delivery_id="m-fence"),
        settlement_attempts=3,
    )
    fake._settle_status_unknown(state, detail="test")  # type: ignore[arg-type]
    fake._settle_status_unknown(state, detail="test")  # type: ignore[arg-type]

    assert [event for event, _fields in fake.events] == [
        "worker.carrier.settlement.status_unknown"
    ]
    assert len(fake.facts) == 1


def test_status_unknown_tombstone_loads_for_a_new_session(tmp_path: Path) -> None:
    """A tombstone from an old native session must remain visible to the new one."""

    state_path = tmp_path / "carrier.sqlite3"
    store = CodexCarrierStore(state_path)
    delivery = HarnessDelivery(
        delivery_id="m-old",
        conversation_id="c-old",
        sender="requester",
        recipient=ACTOR,
        message="prompt-old",
    )
    store.record_fetched(ACTOR, "old-session", delivery, "reply")
    store.record_status_unknown(ACTOR, "m-old", 8)
    loaded = store.load(ACTOR, "new-session")
    store.close()

    assert [(item.delivery.delivery_id, item.state) for item in loaded] == [
        ("m-old", "STATUS_UNKNOWN")
    ]


def test_settlement_attempt_writes_are_monotonic(tmp_path: Path) -> None:
    """A late deferred fact must never refund attempts already spent."""

    state_path = tmp_path / "carrier.sqlite3"
    store = CodexCarrierStore(state_path)
    message_id = "m-monotonic"
    delivery = HarnessDelivery(
        delivery_id=message_id,
        conversation_id="c-monotonic",
        sender="requester",
        recipient=ACTOR,
        message="prompt-monotonic",
    )
    store.record_fetched(ACTOR, THREAD_ID, delivery, "reply")
    store.record_turn_started(ACTOR, message_id, "turn-monotonic")
    store.record_final(ACTOR, message_id, "final-monotonic")
    try:
        store.record_settlement_attempts(ACTOR, message_id, 6)
        store.record_settlement_attempts(ACTOR, message_id, 2)
        assert _store_rows(state_path) == [(message_id, "FINAL_OBSERVED", 6)]

        store.record_status_unknown(ACTOR, message_id, 3)
        assert _store_rows(state_path) == [(message_id, "STATUS_UNKNOWN", 6)]
    finally:
        store.close()
