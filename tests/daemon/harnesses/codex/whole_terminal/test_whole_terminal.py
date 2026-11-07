"""E2: Codex whole-terminal turns, including turns typed in the native TUI.

Red-first acceptance for design §5 E2: a native turn without any Harness inbox
delivery is observed in-turn then idle, one native turn gets one observation
identity, carrier-injected turns keep their delivery receipt, and a session
without a producer never claims idle. Nothing here kills a turn or changes
delivery.
"""
from __future__ import annotations

import threading
import time

from hyprial.daemon.impl.harnesses.codex import CodexInteractiveAppServer
from hyprial.daemon.impl.harnesses.codex.carrier import CodexInteractiveCarrier
from hyprial.daemon.impl.harnesses.codex.observation.whole_session import (
    WholeTerminalTurns,
)
from hyprial.daemon.impl.harnesses.codex.transport import open_app_server_connection

from .support import (
    ACTOR,
    THREAD_ID,
    FakeAppServer,
    RecordingDaemon,
    carrier_app_server,
)



class _Recording:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[tuple[str, str, str | None]] = []

    def __call__(self, report_id: str, phase: str, status: str | None) -> None:
        with self.lock:
            self.rows.append((report_id, phase, status))

    def snapshot(self) -> list[tuple[str, str, str | None]]:
        with self.lock:
            return list(self.rows)

    def wait_for(self, predicate, *, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.snapshot()):
                return True
            time.sleep(0.02)
        return False


def _thread_snapshot(status: str, turns: list[dict[str, object]]) -> dict[str, object]:
    return {"id": THREAD_ID, "status": {"type": status}, "turns": turns}


def _open(turn_id: str) -> dict[str, object]:
    return {"id": turn_id, "status": "inProgress", "items": []}


def _done(turn_id: str, status: str = "completed") -> dict[str, object]:
    return {"id": turn_id, "status": status, "items": []}


def _observer(
    record: _Recording,
    *,
    subscribe_result=None,
    read_result=None,
    owned=None,
    retry_seconds: float = 0.05,
):
    observed: dict[str, object] = {"subscribes": 0}

    def subscribe():
        observed["subscribes"] = int(observed["subscribes"]) + 1
        value = subscribe_result(observed) if callable(subscribe_result) else subscribe_result
        if isinstance(value, Exception):
            raise value
        return value

    def read_thread():
        value = read_result(observed) if callable(read_result) else read_result
        if isinstance(value, Exception):
            raise value
        assert value is not None, "observer read a thread it was not given"
        return value

    return WholeTerminalTurns(
        thread_id=THREAD_ID,
        subscribe=subscribe,
        read_thread=read_thread,
        record=record,
        owned=owned,
        retry_seconds=retry_seconds,
    ), observed


def test_connection_dispatches_notification_while_a_request_is_pending(socket_root):
    server = FakeAppServer(socket_root / "app-server.sock")
    server.start()
    try:
        connection = open_app_server_connection(server.socket_path, timeout=5.0)
        try:
            received: list[dict[str, object]] = []
            connection.add_notification_listener(received.append)
            response = connection.request("thread/read", {"threadId": THREAD_ID}, 1)
            server.broadcast("turn/started", {
                "threadId": THREAD_ID,
                "turn": {"id": "turn-notification", "status": "inProgress", "items": []},
            })
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not received:
                time.sleep(0.01)
            assert response["id"] == 1
            assert [item["method"] for item in received] == ["turn/started"]
        finally:
            connection.close()
    finally:
        server.stop()


def test_subscribed_observer_reports_a_tui_turn_end_to_end(socket_root, tmp_path):
    server = FakeAppServer(socket_root / "app-server.sock")
    server.start()
    try:
        app_server = CodexInteractiveAppServer(server.socket_path, cwd=tmp_path)
        app_server._socket = open_app_server_connection(server.socket_path, timeout=5.0)
        app_server._discovered_thread_id = THREAD_ID
        record = _Recording()
        turns = WholeTerminalTurns(
            thread_id=THREAD_ID,
            subscribe=app_server.subscribe_thread,
            read_thread=app_server.read_thread_snapshot,
            record=record,
        )
        remove = app_server.add_notification_listener(turns.notify)
        try:
            turns.start()
            assert server.wait_for_request("thread/resume"), server.methods()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not turns.subscribed:
                time.sleep(0.01)
            assert turns.subscribed

            server.begin_tui_turn("turn-typed-by-user")
            assert record.wait_for(lambda rows: ("turn-typed-by-user", "started", None) in rows)
            assert record.snapshot() == [("turn-typed-by-user", "started", None)]

            server.end_turn("turn-typed-by-user", "completed")
            assert record.wait_for(
                lambda rows: rows[-1] == ("turn-typed-by-user", "ended", "completed")
            )
            assert record.snapshot() == [
                ("turn-typed-by-user", "started", None),
                ("turn-typed-by-user", "ended", "completed"),
            ]
        finally:
            turns.stop()
            remove()
            app_server.stop()
    finally:
        server.stop()


def test_duplicate_native_events_keep_one_identity():
    record = _Recording()
    turns, _ = _observer(record, subscribe_result=_thread_snapshot("idle", []))
    turns.start()
    try:
        turns.notify({"method": "turn/started", "params": {"threadId": THREAD_ID, "turn": _open("turn-short")}})
        turns.notify({"method": "turn/started", "params": {"threadId": THREAD_ID, "turn": _open("turn-short")}})
        turns.notify({
            "method": "turn/completed",
            "params": {"threadId": THREAD_ID, "turn": _done("turn-short")},
        })
        turns.notify({
            "method": "turn/completed",
            "params": {"threadId": THREAD_ID, "turn": _done("turn-short")},
        })
        assert record.wait_for(lambda rows: len(rows) >= 2), record.snapshot()
        time.sleep(0.2)
        assert record.snapshot() == [
            ("turn-short", "started", None),
            ("turn-short", "ended", "completed"),
        ]
    finally:
        turns.stop()


def test_interrupted_turn_is_reported_as_failed_not_completed():
    record = _Recording()
    turns, _ = _observer(record, subscribe_result=_thread_snapshot("idle", []))
    turns.start()
    try:
        turns.notify({"method": "turn/started", "params": {"threadId": THREAD_ID, "turn": _open("turn-aborted")}})
        turns.notify({
            "method": "turn/completed",
            "params": {"threadId": THREAD_ID, "turn": _done("turn-aborted", "interrupted")},
        })
        assert record.wait_for(lambda rows: len(rows) >= 2), record.snapshot()
        assert record.snapshot() == [
            ("turn-aborted", "started", None),
            ("turn-aborted", "ended", "failed"),
        ]
    finally:
        turns.stop()


def test_subscribe_adopts_a_turn_that_was_already_open():
    record = _Recording()
    turns, observed = _observer(
        record,
        subscribe_result=_thread_snapshot("active", [_open("turn-running")]),
        read_result=_thread_snapshot("active", [_open("turn-running")]),
    )
    turns.start()
    try:
        assert record.wait_for(lambda rows: rows == [("turn-running", "started", None)]), record.snapshot()
        turns.notify({
            "method": "turn/completed",
            "params": {"threadId": THREAD_ID, "turn": _done("turn-running")},
        })
        assert record.wait_for(lambda rows: rows[-1] == ("turn-running", "ended", "completed")), record.snapshot()
        assert observed["subscribes"] == 1
    finally:
        turns.stop()


def test_lost_completion_event_is_recovered_from_a_fresh_thread_read():
    record = _Recording()
    state = {"thread": _thread_snapshot("active", [_open("turn-lost-end")])}
    turns, _ = _observer(
        record,
        subscribe_result=lambda _observed: state["thread"],
        read_result=lambda _observed: state["thread"],
    )
    turns.start()
    try:
        assert record.wait_for(lambda rows: rows == [("turn-lost-end", "started", None)])
        state["thread"] = _thread_snapshot("idle", [_done("turn-lost-end")])
        turns.notify({
            "method": "thread/status/changed",
            "params": {"threadId": THREAD_ID, "status": {"type": "idle"}},
        })
        assert record.wait_for(
            lambda rows: rows[-1] == ("turn-lost-end", "ended", "completed")
        ), record.snapshot()
    finally:
        turns.stop()


def test_reconnect_resync_replaces_stale_state():
    record = _Recording()
    state = {"thread": _thread_snapshot("idle", [])}

    def subscribe(observed):
        if observed["subscribes"] == 1:
            raise ConnectionError("app-server socket closed")
        return state["thread"]

    turns, observed = _observer(
        record,
        subscribe_result=subscribe,
        read_result=lambda _observed: state["thread"],
    )
    turns.start()
    try:
        assert record.wait_for(lambda rows: False, timeout=0.3) is False
        assert observed["subscribes"] >= 2, observed
        state["thread"] = _thread_snapshot("active", [_open("turn-after-reconnect")])
        turns.notify({
            "method": "thread/status/changed",
            "params": {"threadId": THREAD_ID, "status": {"type": "active", "activeFlags": []}},
        })
        assert record.wait_for(
            lambda rows: rows == [("turn-after-reconnect", "started", None)]
        ), record.snapshot()
    finally:
        turns.stop()


def test_a_first_turn_before_the_rollout_exists_is_adopted_after_retry():
    """A brand-new TUI thread has no rollout, so the first subscribe fails.

    The native active status is broadcast anyway; it must drive an immediate
    retry that adopts the turn already in progress, instead of waiting out the
    retry interval or reporting nothing.
    """
    record = _Recording()
    state = {"subscribed": False, "thread": _thread_snapshot("active", [_open("turn-first")])}

    def subscribe(observed):
        if not state["subscribed"]:
            raise ConnectionError("no rollout found for thread id")
        return state["thread"]

    turns, observed = _observer(
        record,
        subscribe_result=subscribe,
        read_result=lambda _observed: state["thread"],
        retry_seconds=60.0,
    )
    turns.start()
    try:
        assert record.wait_for(lambda rows: False, timeout=0.2) is False
        assert observed["subscribes"] == 1, observed
        state["subscribed"] = True
        turns.notify({
            "method": "thread/status/changed",
            "params": {"threadId": THREAD_ID, "status": {"type": "active", "activeFlags": []}},
        })
        assert record.wait_for(
            lambda rows: rows == [("turn-first", "started", None)]
        ), record.snapshot()
        assert observed["subscribes"] == 2, observed
    finally:
        turns.stop()


def test_carrier_owned_turns_keep_their_delivery_receipt():
    record = _Recording()
    turns, _ = _observer(
        record,
        subscribe_result=_thread_snapshot("idle", []),
        owned=lambda turn_id: turn_id == "turn-injected",
    )
    turns.start()
    try:
        turns.notify({"method": "turn/started", "params": {"threadId": THREAD_ID, "turn": _open("turn-injected")}})
        turns.notify({
            "method": "turn/completed",
            "params": {"threadId": THREAD_ID, "turn": _done("turn-injected")},
        })
        assert record.wait_for(lambda rows: False, timeout=0.3) is False
        assert record.snapshot() == []
    finally:
        turns.stop()


def test_other_threads_are_never_attributed_to_this_session():
    record = _Recording()
    turns, _ = _observer(record, subscribe_result=_thread_snapshot("idle", []))
    turns.start()
    try:
        turns.notify({
            "method": "turn/started",
            "params": {"threadId": "thread-child-agent", "turn": _open("turn-child")},
        })
        assert record.wait_for(lambda rows: False, timeout=0.3) is False
        assert record.snapshot() == []
    finally:
        turns.stop()


def test_failed_subscribe_never_claims_whole_session_coverage():
    record = _Recording()
    turns, observed = _observer(record, subscribe_result=ConnectionError("app-server gone"))
    turns.start()
    try:
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            time.sleep(0.02)
        assert turns.subscribed is False
        assert observed["subscribes"] >= 1
        turns.notify({
            "method": "thread/status/changed",
            "params": {"threadId": THREAD_ID, "status": {"type": "active", "activeFlags": []}},
        })
        assert record.wait_for(lambda rows: False, timeout=0.3) is False
        assert record.snapshot() == []
    finally:
        turns.stop()


def test_a_long_native_turn_is_never_closed_by_elapsed_time():
    record = _Recording()
    turns, _ = _observer(record, subscribe_result=_thread_snapshot("idle", []))
    turns.start()
    try:
        turns.notify({"method": "turn/started", "params": {"threadId": THREAD_ID, "turn": _open("turn-long")}})
        assert record.wait_for(lambda rows: rows == [("turn-long", "started", None)])
        time.sleep(0.3)
        assert record.snapshot() == [("turn-long", "started", None)]
    finally:
        turns.stop()


def test_carrier_reports_a_tui_turn_without_any_inbox_delivery(socket_root, tmp_path):
    server = FakeAppServer(socket_root / "app-server.sock")
    server.start()
    try:
        app_server = carrier_app_server(server.socket_path, cwd=tmp_path)
        daemon = RecordingDaemon()
        carrier = CodexInteractiveCarrier(
            app_server,
            actor=ACTOR,
            session_ref=THREAD_ID,
            cwd=tmp_path,
            command=["codex", "--remote"],
            daemon_request=daemon.request,
            state_path=tmp_path / "carrier.sqlite3",
            poll_seconds=0.02,
            settlement_retry_seconds=0.02,
        )
        carrier.start()
        try:
            assert daemon.wait_for(
                lambda: any(
                    params.get("turnCoverage") == "whole-session"
                    for params in daemon.of("session.refresh")
                )
            ), daemon.of("session.refresh")

            server.begin_tui_turn("turn-typed-by-user")
            assert daemon.wait_for(
                lambda: any(
                    params.get("reportId") == "turn-typed-by-user"
                    for params in daemon.of("session.turn.started")
                )
            ), daemon.calls
            started = [
                params
                for params in daemon.of("session.turn.started")
                if params.get("reportId") == "turn-typed-by-user"
            ]
            assert started == [
                {"actor": ACTOR, "sessionRef": THREAD_ID, "reportId": "turn-typed-by-user"}
            ], started

            server.end_turn("turn-typed-by-user", "completed")
            assert daemon.wait_for(
                lambda: any(
                    params.get("reportIds") == ["turn-typed-by-user"]
                    for params in daemon.of("session.turn.ended")
                )
            ), daemon.calls
            ended = [
                params
                for params in daemon.of("session.turn.ended")
                if params.get("reportIds") == ["turn-typed-by-user"]
            ]
            assert ended == [
                {
                    "actor": ACTOR,
                    "sessionRef": THREAD_ID,
                    "reportIds": ["turn-typed-by-user"],
                    "status": "completed",
                }
            ], ended
        finally:
            carrier.stop()
            app_server.stop()
    finally:
        server.stop()


def test_interleaved_injected_delivery_keeps_both_identities(tmp_path, socket_root):
    server = FakeAppServer(socket_root / "app-server.sock")
    server.emit_native_events = True
    server.start()
    try:
        app_server = carrier_app_server(server.socket_path, cwd=tmp_path)
        daemon = RecordingDaemon()
        carrier = CodexInteractiveCarrier(
            app_server,
            actor=ACTOR,
            session_ref=THREAD_ID,
            cwd=tmp_path,
            command=["codex", "--remote"],
            daemon_request=daemon.request,
            state_path=tmp_path / "carrier.sqlite3",
            poll_seconds=0.02,
            settlement_retry_seconds=0.02,
        )
        carrier.start()
        try:
            assert daemon.wait_for(
                lambda: any(
                    params.get("turnCoverage") == "whole-session"
                    for params in daemon.of("session.refresh")
                )
            ), daemon.of("session.refresh")

            server.begin_tui_turn("turn-typed-1")
            assert daemon.wait_for(
                lambda: any(
                    params.get("reportId") == "turn-typed-1"
                    for params in daemon.of("session.turn.started")
                )
            ), daemon.calls
            server.end_turn("turn-typed-1", "completed")
            assert daemon.wait_for(
                lambda: any(
                    params.get("reportIds") == ["turn-typed-1"]
                    for params in daemon.of("session.turn.ended")
                )
            ), daemon.calls

            daemon.add_pending(
                {
                    "messageId": "delivery-1",
                    "conversationId": "conv-1",
                    "from": "agent:peer:node:requester",
                    "intent": "request",
                    "message": "please handle this",
                }
            )
            assert daemon.wait_for(
                lambda: any(
                    params.get("deliveryId") == "delivery-1"
                    for params in daemon.of("session.turn.started")
                )
            ), daemon.calls
            injected = [
                params
                for params in daemon.of("session.turn.started")
                if params.get("deliveryId") == "delivery-1"
            ]
            assert len(injected) == 1, injected
            injected_turn = str(injected[0]["reportId"])
            assert injected_turn.startswith("turn-native-"), injected_turn
            assert [
                params
                for params in daemon.of("session.turn.started")
                if params.get("reportId") == injected_turn
            ] == injected, daemon.of("session.turn.started")

            server.end_turn(injected_turn, "completed")
            assert daemon.wait_for(
                lambda: any(
                    params.get("reportIds") == [injected_turn]
                    for params in daemon.of("session.turn.ended")
                )
            ), daemon.calls

            server.begin_tui_turn("turn-typed-2")
            assert daemon.wait_for(
                lambda: any(
                    params.get("reportId") == "turn-typed-2"
                    for params in daemon.of("session.turn.started")
                )
            ), daemon.calls
            started_ids = [
                str(params["reportId"]) for params in daemon.of("session.turn.started")
            ]
            assert started_ids == ["turn-typed-1", injected_turn, "turn-typed-2"]
        finally:
            carrier.stop()
            app_server.stop()
    finally:
        server.stop()


def test_a_failing_report_sink_never_ends_the_observer():
    def record(report_id, phase, status):
        raise RuntimeError("carrier store is closed")

    turns, _ = _observer(record, subscribe_result=_thread_snapshot("idle", []))
    turns.start()
    try:
        turns.notify({"method": "turn/started", "params": {"threadId": THREAD_ID, "turn": _open("turn-sink")}})
        turns.notify({"method": "turn/completed", "params": {"threadId": THREAD_ID, "turn": _done("turn-sink")}})
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            time.sleep(0.02)
        assert turns._worker.is_alive()
    finally:
        turns.stop()
