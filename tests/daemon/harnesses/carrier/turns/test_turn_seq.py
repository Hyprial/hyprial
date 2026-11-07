"""T6, T11, T12: the daemon-stamped seq survives restarts and races.

wp5.md §4.3, §4.5.  Sequence allocation and bus enqueue share one publish
critical section; a full daemon restart clears the high water mark, while a
carrier restart never resets the daemon's counter.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.biz import ReplyReminderConsumer
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

from tests.daemon.harnesses.carrier.turns.test_turn_reports import (
    A_NAME,
    A_URI,
    X_URI,
    _TurnApp,
    _wall_clock,
)

EVENTS = ("turn.started", "turn.completed", "turn.failed")


def _request(inbox: Any, message_id: str = "r1") -> None:
    inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id="conv-seq",
            sender=X_URI,
            recipient=A_URI,
            payload=json.dumps({"message": "please answer"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )


def _observed_seq(events: list[dict[str, Any]]) -> list[int]:
    return [item["seq"] for item in events]


def test_seq_survives_full_restart(tmp_path: Path) -> None:
    """(a) one counter for both emitters; (b) a restart clears the high water."""

    # (a) headless and interactive turns interleave on one strict counter.
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path / "a", clock, inbox_path=tmp_path / "inbox.sqlite3")
    try:
        app.register()
        projection = HarnessDelivery("d1", "conv", X_URI, A_URI, "req")
        result = HarnessResult(
            "d1", A_URI, HarnessResultStatus.COMPLETED, output="ok"
        )
        for index in range(4):
            if index % 2 == 0:
                app.started(f"ipc-{index}")
                clock["now"] = clock["now"] + 10
                app.ended([f"ipc-{index}"])
            else:
                clock["now"] = clock["now"] + 10
                app.hooks.observe_turn(
                    projection, result, started_at_ms=clock["now"] - 5,
                    ended_at_ms=clock["now"],
                )
        assert app.recorder.wait_for(
            lambda: len(app.recorder.events) == 6
        )
        seqs = _observed_seq(app.recorder.events)
        assert seqs == sorted(seqs) and len(set(seqs)) == 6
        assert {item["source"] for item in app.recorder.events} == {"daemon.turns"}
    finally:
        app.close()

    # (b) full restart: new emitter AND new consumer over the same inbox file.
    stale: list[tuple[str, dict[str, object]]] = []
    clock = _wall_clock()
    first = _TurnApp(
        tmp_path / "b1", clock, inbox_path=tmp_path / "restart.sqlite3"
    )
    try:
        for _ in range(55):
            first.hooks.sequence()  # the first life runs the counter past 50
    finally:
        assert first.bus.close(5.0)
        first.app._close()
        first.inbox.close()

    def factory(port: Any) -> ReplyReminderConsumer:
        def capture(level: str, component: str, event: str, **fields: object) -> None:
            stale.append((event, dict(fields)))

        return ReplyReminderConsumer(port=port, logger=capture)

    second = _TurnApp(
        tmp_path / "b2",
        clock,
        inbox_path=tmp_path / "restart.sqlite3",
        reminder_factory=factory,
    )
    try:
        second.register()
        _request(second.inbox)
        second.started("w1")
        second.app.handle(
            "message.pending.list",
            {"actor": A_NAME, "sessionRef": "session-1", "fetched": True},
        )
        clock["now"] = clock["now"] + 60_000
        second.ended(["w1"])

        assert second.recorder.wait_for(
            lambda: len(second.recorder.of("turn.completed")) == 1
        )
        # The new consumer accepts the restarted counter (seq restarts at 0):
        # the reminder is written and no stale replay is logged.
        assert second.recorder.of("turn.completed")[0]["seq"] < 5
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(second.inbox.system_notices(A_URI)) < 1:
            time.sleep(0.01)
        notices = second.inbox.system_notices(A_URI)
        assert len(notices) == 1
        assert "r1" in notices[0].summary
        assert [name for name, _fields in stale if name == "reply_reminder.stale_replay"] == []
    finally:
        second.close()


def test_concurrent_publishes_keep_seq_order_per_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two racing publishers cannot hand the lane a lower seq after a higher."""

    clock = _wall_clock()
    logged: list[str] = []

    def factory(port: Any) -> ReplyReminderConsumer:
        def capture(level: str, component: str, event: str, **fields: object) -> None:
            logged.append(event)

        return ReplyReminderConsumer(port=port, logger=capture)

    app = _TurnApp(tmp_path, clock, reminder_factory=factory)
    try:
        app.register()
        _request(app.inbox, "r1")
        app.started("w1")
        app.app.handle(
            "message.pending.list",
            {"actor": A_NAME, "sessionRef": "session-1", "fetched": True},
        )

        original_sequence = app.hooks.sequence
        state = {"first": True}

        def slow_first_sequence() -> int:
            # The first caller stalls AFTER sequence() has allocated and
            # BEFORE bus.publish (the real interleaving window).  With the
            # publish critical section this stall holds the lock, so the
            # second publisher cannot overtake; without it the lane sees
            # [1, 0].
            value = original_sequence()
            if state["first"]:
                state["first"] = False
                time.sleep(0.25)
            return value

        monkeypatch.setattr(app.hooks, "sequence", slow_first_sequence)

        started = threading.Barrier(2)

        def publish(delivery_id: str) -> None:
            started.wait(2.0)
            app.hooks.observe_carrier_turn(
                actor=A_URI,
                phase="completed",
                at_ms=clock["now"] + 60_000,
                started_at_ms=clock["now"],
                delivery_id=delivery_id,
            )

        threads = [
            threading.Thread(target=publish, args=(name,), daemon=True)
            for name in ("d1", "d2")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)

        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 2
        )
        completed = app.recorder.of("turn.completed")
        seqs = _observed_seq(completed)
        assert seqs == sorted(seqs)  # the lane never sees [1, 0]
        assert {item["payload"]["deliveryId"] for item in completed} == {"d1", "d2"}

        # Both deliveries entered the WP7 consumer's judgment (no stale
        # rejection): one reminder for r1, no stale_replay log line.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not app.inbox.system_notices(A_URI):
            time.sleep(0.01)
        assert len(app.inbox.system_notices(A_URI)) == 1
        assert "reply_reminder.stale_replay" not in logged
    finally:
        app.close()


def test_mixed_emitters_under_threads_stay_monotonic(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        projection = HarnessDelivery("d-mix", "conv", X_URI, A_URI, "req")
        result = HarnessResult(
            "d-mix", A_URI, HarnessResultStatus.COMPLETED, output="ok"
        )
        errors: list[BaseException] = []

        def headless() -> None:
            for _ in range(100):
                try:
                    app.hooks.observe_turn(
                        projection,
                        result,
                        started_at_ms=clock["now"],
                        ended_at_ms=clock["now"] + 1,
                    )
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

        def interactive() -> None:
            for index in range(100):
                try:
                    app.hooks.observe_carrier_turn(
                        actor=A_URI,
                        phase="completed",
                        at_ms=clock["now"] + 2,
                        started_at_ms=clock["now"],
                        delivery_id=f"ipc-{index}",
                    )
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

        def interceptor() -> None:
            for _ in range(100):
                try:
                    app.hooks.sequence()  # the delivery.prepared intercept
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

        threads = [
            threading.Thread(target=headless),
            threading.Thread(target=interactive),
            threading.Thread(target=interceptor),
            threading.Thread(target=interactive),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30.0)
        assert errors == []

        assert app.recorder.wait_for(
            lambda: len(app.recorder.events) >= 300, timeout=30.0
        )
        seqs = _observed_seq(app.recorder.events)
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        assert {item["source"] for item in app.recorder.events} == {"daemon.turns"}
    finally:
        app.close()
