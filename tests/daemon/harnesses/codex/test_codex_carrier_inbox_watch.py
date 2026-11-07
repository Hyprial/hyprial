"""Shared inbox-watch integration for the synchronous Codex carrier."""
from __future__ import annotations

import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.harnesses.codex import CodexInteractiveCarrier


ACTOR = "agent:probe:isolated:codex-watch"
SESSION_REF = "codex-watch-session"
BATCH_TEXT = "[Harness Network inbox batch cursor=codex]\nBYTE-EXACT-秘密"


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _IdleServer:
    pid = 424242

    def start_turn(self, _prompt: str) -> str:
        raise AssertionError("an idle carrier must not start a turn")

    def interrupt_turn(self, _turn_id: str) -> None:
        return None


class _BlockingTurnServer:
    pid = 424243

    def __init__(self, clock: _Clock | None = None) -> None:
        self.clock = clock
        self.started = threading.Event()
        self.release = threading.Event()
        self.started_at: float | None = None
        self.prompts: list[str] = []

    def start_turn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.started_at = self.clock() if self.clock is not None else time.monotonic()
        self.started.set()
        return "turn-1"

    def read_turn(self, turn_id: str) -> dict[str, object]:
        # A status read, like the real app-server's: it never blocks. The
        # carrier reconciles in-flight turns from its poll lane, so a fake
        # that blocked here stalled every poll for seconds.
        if not self.release.is_set():
            return {"id": turn_id, "status": "inProgress", "items": []}
        return {
            "id": turn_id,
            "status": "completed",
            "items": [
                {"type": "agentMessage", "phase": "final_answer", "text": "done"}
            ],
        }

    def interrupt_turn(self, _turn_id: str) -> None:
        self.release.set()


class _FakeDaemon:
    def __init__(self, clock: _Clock | None = None) -> None:
        self.clock = clock
        self.epoch = "epoch-1"
        self.messages: list[dict[str, object]] = []
        self.batch_text: str | None = None
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.wait_entered = threading.Event()
        self.ack_received = threading.Event()
        self.message_at: float | None = None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if method in {"session.register", "session.refresh", "session.heartbeat"}:
            return {"ok": True, "daemonEpoch": self.epoch}
        if method == "message.pending.list":
            return {"ok": True, "daemonEpoch": self.epoch, "messages": list(self.messages)}
        if method == "message.pending.batch":
            # Like the real daemon: one rendered text for exactly this snapshot.
            delivery_text = self.batch_text or (
                "[Fake daemon inbox batch]\n"
                + "\n".join(str(item["messageId"]) for item in self.messages)
            )
            return {
                "ok": True,
                "schemaVersion": 1,
                "format": "inbox-batch-v1",
                "cursor": "cursor-codex",
                "daemonEpoch": self.epoch,
                "actor": ACTOR,
                "itemCount": len(self.messages),
                "totalBodyBytes": 7,
                "inlineLimitBytes": 16384,
                "items": list(self.messages),
                "delivery": {
                    "mode": "inline",
                    "text": delivery_text,
                    "spillPath": None,
                    "spillBytes": None,
                    "spillSha256": None,
                },
            }
        if method == "message.pending.wait":
            self.wait_entered.set()
            hold_seconds = int(params["holdMs"]) / 1000
            if self.clock is not None:
                deadline = self.clock.now + hold_seconds
                if self.message_at is not None and self.message_at <= deadline:
                    self.clock.now = self.message_at
                    return {
                        "ok": True,
                        "daemonEpoch": self.epoch,
                        "held": True,
                        "changed": True,
                    }
                self.clock.now = deadline
            else:
                time.sleep(hold_seconds)
            return {
                "ok": True,
                "daemonEpoch": self.epoch,
                "held": True,
                "changed": False,
            }
        if method in {"session.turn.started", "session.turn.ended"}:
            return {"ok": True}
        if method == "message.settle_hint":
            return {"ok": True, "hint": "ack", "pending": True}
        if method == "message.ack":
            self.messages.clear()
            self.ack_received.set()
            return {"ok": True, "acknowledged": True}
        if method == "message.status":
            return {"ok": True, "state": "pending", "records": []}
        raise AssertionError(method)


def _message(message_id: str = "m-1") -> dict[str, object]:
    return {
        "messageId": message_id,
        "conversationId": "conversation-1",
        "from": "requester",
        "intent": "request",
        "message": "do the work",
    }


def _carrier(
    tmp_path: Path,
    daemon: _FakeDaemon,
    *,
    server: Any | None = None,
    clock: Any = time.monotonic,
    poll_seconds: float = 0.05,
) -> CodexInteractiveCarrier:
    return CodexInteractiveCarrier(
        server or _IdleServer(),
        actor=ACTOR,
        session_ref=SESSION_REF,
        cwd=tmp_path,
        command=["codex", "--remote", "fake-only"],
        daemon_request=daemon.request,
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=poll_seconds,
        settlement_retry_seconds=poll_seconds,
        clock=clock,
    )


def _stop_unstarted(carrier: CodexInteractiveCarrier) -> None:
    carrier._stop.set()
    carrier.start()
    carrier.stop()


_MAX_POLL_STEPS = 10_000


def _drive(carrier: CodexInteractiveCarrier, clock: _Clock, until: float) -> None:
    # Bounded so a mutation that stops the fake clock advancing fails the
    # test instead of hanging the shard (pytest-timeout is not installed).
    for _ in range(_MAX_POLL_STEPS):
        if clock.now >= until:
            return
        delay = carrier._poll_once()
        if isinstance(delay, float) and delay > 0:
            clock.now += delay
        else:
            # The pre-migration carrier returns no scheduling hint.
            clock.now += carrier.poll_seconds
    raise AssertionError(f"poll lane did not reach t={until} in {_MAX_POLL_STEPS} steps")


def test_idle_doorbell_reduces_lists_to_at_most_fifteen_per_minute(tmp_path: Path) -> None:
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    carrier = _carrier(tmp_path, daemon, clock=clock)
    try:
        started = clock.now
        _drive(carrier, clock, started + 60.0)
        methods = Counter(method for method, _params in daemon.calls)
        assert methods["message.pending.list"] <= 15
        assert methods["message.pending.wait"] >= 15
        assert all(
            params["holdMs"] <= 2000
            for method, params in daemon.calls
            if method == "message.pending.wait"
        )
    finally:
        _stop_unstarted(carrier)


def _pump_until(
    carrier: CodexInteractiveCarrier, event: threading.Event, *, wall_seconds: float = 30.0
) -> bool:
    """Keep the carrier's poll lane turning, without advancing fake time,
    until ``event`` is set.

    The turn starts only when the carrier drains its actor's
    EnqueueTurnRequested effect, and only the poll lane drains it. The actor
    runs on its own thread, so under load its effect can land after the last
    poll that _drive made. Waiting on the event alone would then wait for a
    drain that never comes. Fake time stays put, so latency assertions on the
    fake clock remain exact.
    """

    deadline = time.monotonic() + wall_seconds
    while not event.is_set() and time.monotonic() < deadline:
        carrier._poll_once()
        event.wait(0.01)
    return event.is_set()


def test_codex_native_turn_uses_daemon_batch_text_byte_exactly(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    daemon.messages = [_message("m-1"), _message("m-2")]
    daemon.batch_text = BATCH_TEXT
    server = _BlockingTurnServer(clock)
    carrier = _carrier(tmp_path, daemon, server=server, clock=clock)
    try:
        _drive(carrier, clock, clock.now + 1.0)
        assert _pump_until(carrier, server.started)
        assert server.prompts == [BATCH_TEXT]
        batches = [params for method, params in daemon.calls if method == "message.pending.batch"]
        assert len([params for params in batches if "cursor" not in params]) == 1
        assert [params["cursor"] for params in batches if "cursor" in params] == ["cursor-codex"] * 2
    finally:
        server.release.set()
        _stop_unstarted(carrier)


def test_idle_doorbell_starts_a_new_turn_within_one_second(tmp_path: Path) -> None:
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    server = _BlockingTurnServer(clock)
    carrier = _carrier(tmp_path, daemon, server=server, clock=clock)
    try:
        _drive(carrier, clock, clock.now + 0.1)
        daemon.messages = [_message()]
        daemon.message_at = clock.now + 0.2
        _drive(carrier, clock, daemon.message_at + 0.5)
        assert _pump_until(carrier, server.started)
        assert server.started_at is not None
        assert server.started_at - daemon.message_at < 1.0
    finally:
        server.release.set()
        _stop_unstarted(carrier)


def test_doorbell_turn_starts_when_the_enqueue_effect_lands_after_the_last_poll(
    tmp_path: Path,
) -> None:
    # The CI interleaving behind the 2/2 red on #1162: the actor's enqueue
    # effect arrives after _drive's last poll. Hold the effect back for the
    # whole drive, then release it; the poll lane must still start the turn.
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    server = _BlockingTurnServer(clock)
    carrier = _carrier(tmp_path, daemon, server=server, clock=clock)
    real_drain = carrier._carrier_runtime.drain_effects
    held: list[object] = []
    hold = {"on": True}

    def late_drain() -> list[object]:
        effects = list(real_drain())
        if hold["on"]:
            held.extend(effects)
            return []
        released = held + effects
        held.clear()
        return released

    carrier._carrier_runtime.drain_effects = late_drain  # type: ignore[method-assign]
    try:
        _drive(carrier, clock, clock.now + 0.1)
        daemon.messages = [_message()]
        daemon.message_at = clock.now + 0.2
        _drive(carrier, clock, daemon.message_at + 0.5)
        assert not server.started.is_set()
        # Put the poll lane where CI found it: the list that caused the turn
        # is done and the next one is not due. Holds off and a long interval;
        # one more step lists (if a list is due) while the effect is still
        # held, after which, with fake time frozen, every step is a Sleep.
        carrier._inbox_hold_allowed = lambda: False  # type: ignore[method-assign]
        carrier.poll_seconds = 3600.0
        carrier._poll_once()
        lists_before = sum(m == "message.pending.list" for m, _ in daemon.calls)
        hold["on"] = False
        # Only the carrier's per-step effect drain can start the turn now.
        assert _pump_until(carrier, server.started, wall_seconds=5.0)
        assert sum(m == "message.pending.list" for m, _ in daemon.calls) == lists_before
    finally:
        server.release.set()
        _stop_unstarted(carrier)


def test_stop_returns_within_three_seconds_during_held_wait(tmp_path: Path) -> None:
    daemon = _FakeDaemon()
    carrier = _carrier(tmp_path, daemon, poll_seconds=0.01)
    carrier.start()
    assert daemon.wait_entered.wait(2.0)
    started = time.monotonic()
    carrier.stop()
    assert time.monotonic() - started < 3.0
    assert not carrier._thread.is_alive()


def test_inflight_delivery_never_holds_before_settlement(tmp_path: Path) -> None:
    daemon = _FakeDaemon()
    daemon.messages = [_message()]
    server = _BlockingTurnServer()
    carrier = _carrier(tmp_path, daemon, server=server, poll_seconds=0.02)
    carrier.start()
    try:
        assert server.started.wait(2.0)
        # Let several ordinary poll intervals elapse while the turn remains
        # in flight.  The forbidden mutation enters a 2s hold in this window.
        time.sleep(0.1)
        wait_count = sum(
            method == "message.pending.wait" for method, _params in daemon.calls
        )
        released_at = time.monotonic()
        server.release.set()
        assert daemon.ack_received.wait(1.0)
        assert time.monotonic() - released_at < 1.0
        assert sum(
            method == "message.pending.wait" for method, _params in daemon.calls
        ) == wait_count == 0
    finally:
        server.release.set()
        carrier.stop()


def test_heartbeat_restart_lists_before_starting_another_hold(tmp_path: Path) -> None:
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    heartbeat_calls = 0

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal heartbeat_calls
        if method == "session.heartbeat":
            heartbeat_calls += 1
            daemon.epoch = "epoch-2"
        return daemon.request(method, params)

    carrier = _carrier(tmp_path, daemon, clock=clock)
    carrier.daemon_request = request
    try:
        _drive(carrier, clock, clock.now + 3.0)
        methods = [method for method, _params in daemon.calls]
        refresh_index = methods.index("session.refresh", 1)
        assert heartbeat_calls >= 1
        assert methods[refresh_index + 1] == "message.pending.list"
    finally:
        _stop_unstarted(carrier)


def test_a_message_whose_staging_failed_is_retried_on_the_next_list(tmp_path: Path) -> None:
    # The core reports a message "new" only once; the carrier must still
    # stage any listed message it does not hold, or a row whose staging
    # failed once would never be retried (the pre-migration behaviour).
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    daemon.messages.append(_message("m-1"))
    carrier = _carrier(tmp_path, daemon, clock=clock)
    attempts: list[str] = []
    real_stage = carrier._stage_carrier_fetched

    def flaky_stage(delivery, intent):  # type: ignore[no-untyped-def]
        attempts.append(delivery.delivery_id)
        if len(attempts) == 1:
            raise RuntimeError("injected staging failure")
        return real_stage(delivery, intent)

    carrier._stage_carrier_fetched = flaky_stage  # type: ignore[method-assign]
    try:
        _drive(carrier, clock, clock.now + 5.0)
        assert attempts.count("m-1") >= 2, attempts
    finally:
        _stop_unstarted(carrier)


def test_a_carrier_that_turned_busy_after_a_list_does_not_hold(tmp_path: Path) -> None:
    # Review 852 on #1154: use_wait was consulted only when a list settled,
    # so work the carrier picked up afterwards (a delivery refilled from its
    # store in the same round) still sat behind a 2-4 s hold.
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    carrier = _carrier(tmp_path, daemon, clock=clock)
    busy = {"now": False}
    real_allowed = carrier._inbox_hold_allowed

    def hold_allowed() -> bool:
        return False if busy["now"] else real_allowed()

    carrier._inbox_hold_allowed = hold_allowed  # type: ignore[method-assign]
    carrier._list_may_hold = lambda _response: True  # type: ignore[method-assign]
    try:
        for _ in range(_MAX_POLL_STEPS):
            if any(method == "message.pending.list" for method, _ in daemon.calls):
                break
            delay = carrier._poll_once()
            clock.now += delay or carrier.poll_seconds
        else:
            raise AssertionError("the carrier never listed its inbox")
        busy["now"] = True  # work arrived after the list settled
        _drive(carrier, clock, clock.now + 3.0)
        assert not any(method == "message.pending.wait" for method, _ in daemon.calls)
    finally:
        _stop_unstarted(carrier)


def test_a_failing_hold_check_never_wedges_the_poll_lane(tmp_path: Path) -> None:
    # Review 852: the hold check sits between poll_next and poll_done; an
    # exception there left the list outstanding and the lane wedged.
    clock = _Clock()
    daemon = _FakeDaemon(clock)
    carrier = _carrier(tmp_path, daemon, clock=clock)

    def broken(_response: object) -> bool:
        raise RuntimeError("injected hold-check failure")

    carrier._list_may_hold = broken  # type: ignore[method-assign]
    logged: list[dict[str, object]] = []
    real_log = carrier._log

    def capture(level: str, event: str, **fields: object) -> None:
        logged.append({"level": level, "event": event, **fields})
        real_log(level, event, **fields)

    carrier._log = capture  # type: ignore[method-assign]
    try:
        _drive(carrier, clock, clock.now + 3.0)
        lists = sum(method == "message.pending.list" for method, _ in daemon.calls)
        assert lists >= 3, lists
        # Review 854: the swallowed failure must still be visible.
        hold_errors = [entry for entry in logged if entry.get("stage") == "inbox-hold-check"]
        assert hold_errors, logged
        assert "injected hold-check failure" in str(hold_errors[0]["error"])
    finally:
        _stop_unstarted(carrier)
