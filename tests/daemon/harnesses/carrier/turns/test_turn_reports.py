"""T1-T10, K8: interactive carriers report turn boundaries over IPC.

wp5.md §4, §7.6.  A real ``DaemonApplication`` (the WP7 reply-reminder
assembly), a real inbox, a fake daemon clock and a recording daemon-level
lane.  The carrier's own ``seq``/``source``/timestamps are never read.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.wiring.reply_reminder_hooks import (
    wire_reply_reminder_hooks,
)
from hyprial.daemon.impl.hooks import DaemonHookService
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.kernel import DaemonRequestError, HookBus, canonical_agent_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _daemon_dependencies

NODE = "wp5a-turns"
OWNER = "wp5a-owner"
A_NAME = "carrier-a"
A_URI = canonical_agent_uri(OWNER, NODE, A_NAME)
X_URI = canonical_agent_uri(OWNER, NODE, "peer-x")


class _LoopbackTransport:
    def __init__(self) -> None:
        self.service: InboxService | None = None

    def bind(self, service: InboxService) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


class _Recorder:
    """A daemon-level consumer lane that records every observed event."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def observe(self, event: Any) -> None:
        with self.lock:
            self.events.append(
                {
                    "event": event.event,
                    "seq": event.seq,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": dict(event.payload),
                }
            )

    def of(self, event: str) -> list[dict[str, Any]]:
        with self.lock:
            return [item for item in self.events if item["event"] == event]

    def wait_for(self, predicate: Any, *, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())


def _inbox(tmp_path: Path) -> InboxService:
    from hyprial.daemon.impl.dispatch.admission import LinkAdmission
    from tests.support.links.gate import PermissiveLinkGate

    transport = _LoopbackTransport()
    # The batch read runs link admission (design §3.3); these fixtures use
    # canonical agent URIs on both ends, so bind the permissive gate the
    # durable batch fixtures use instead of the fail-closed default.
    admission = LinkAdmission()
    admission.bind(PermissiveLinkGate())
    service = InboxService(
        tmp_path / "inbox.sqlite3", transport, link_admission=admission
    )
    transport.bind(service)
    return service


class _TurnApp:
    """One application with a fake-clock hook service and a recording lane."""

    def __init__(
        self,
        tmp_path: Path,
        clock: dict[str, int],
        *,
        inbox_path: Path | None = None,
        reminder_factory: Any = None,
    ) -> None:
        self.clock = clock
        self.inbox = _inbox(inbox_path or tmp_path / "inbox.sqlite3")
        self.recorder = _Recorder()
        dependencies = dict(_daemon_dependencies())
        if reminder_factory is None:
            reminder_factory = dependencies.pop("reply_reminder_consumer_factory", None)
        else:
            dependencies.pop("reply_reminder_consumer_factory", None)
        self.app = DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            node_id=NODE,
            owner=OWNER,
            reply_reminder_consumer_factory=reminder_factory,
            **dependencies,
        )
        self.app._inbox = self.inbox
        self.app._presence = object()
        self.app._harnesses = object()
        self.app._adapters = None
        if self.app.agents.get(A_NAME) is None:
            self.app.handle("agent.create", {"name": A_NAME})

        self.bus = HookBus()
        self.hooks = DaemonHookService(
            bus=self.bus,
            config_path_for_agent=lambda _actor: tmp_path / "absent" / "hooks.json",
            consumers={},
            handler_notifier=lambda *_args, **_kwargs: None,
            clock_ms=lambda: clock["now"],
        )
        self.app._daemon_hooks = self.hooks
        self.hooks.register_consumer(
            "recorder",
            self.recorder,
            ("turn.started", "turn.completed", "turn.failed"),
        )
        if reminder_factory is not None:
            wire_reply_reminder_hooks(
                inbox=self.inbox,
                node_id=NODE,
                clock_ms=lambda: clock["now"],
                consumer_factory=reminder_factory,
                daemon_hooks=self.hooks,
            )

    def register(self, *, session_ref: str = "session-1", runtime: str = "pi_interactive") -> None:
        self.app.handle(
            "session.register",
            {
                "actor": A_NAME,
                "sessionRef": session_ref,
                "cwd": "/work",
                "command": ["test"],
                "source": "channel-test",
                "runtime": runtime,
                "channelConfirmed": True,
            },
        )

    def started(
        self, report_id: str, session_ref: str = "session-1", **extra: object
    ) -> dict[str, object]:
        return self.app.handle(
            "session.turn.started",
            {"actor": A_NAME, "sessionRef": session_ref, "reportId": report_id, **extra},
        )

    def ended(
        self,
        report_ids: list[str],
        session_ref: str = "session-1",
        **extra: object,
    ) -> dict[str, object]:
        return self.app.handle(
            "session.turn.ended",
            {"actor": A_NAME, "sessionRef": session_ref, "reportIds": report_ids, **extra},
        )

    def close(self) -> None:
        assert self.bus.close(5.0)
        self.app._close()
        self.inbox.close()

    def seed_request(self, message_id: str, *, summary: str | None = None) -> None:
        self.inbox.receive(
            InboxMessage(
                message_id=message_id,
                conversation_id="conv-turns",
                sender=X_URI,
                recipient=A_URI,
                payload=json.dumps({"message": "please answer"}).encode(),
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=1,
                summary=summary,
            )
        )


def test_started_and_ended_publish_daemon_stamped_events(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        # Carrier-sent seq/source/startedAtMs are noise: never read.
        first = app.started("t1", deliveryId="d1", seq=0, source="pi-extension", startedAtMs=1)
        assert first == {"ok": True, "startedAtMs": 5_000}
        clock["now"] = 6_000
        ended = app.ended(["t1"], status="completed", deliveryId="d1")
        assert ended["ok"] is True
        assert ended["endedAtMs"] == 6_000

        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 1
        )
        started = app.recorder.of("turn.started")
        completed = app.recorder.of("turn.completed")
        assert len(started) == 1 and len(completed) == 1
        for item in (started[0], completed[0]):
            assert item["actor"] == A_URI
            assert item["source"] == "daemon.turns"
        assert started[0]["payload"]["startedAtMs"] == 5_000  # the daemon's
        assert started[0]["payload"]["startedAtMs"] != 1  # not the carrier's
        assert completed[0]["payload"]["endedAtMs"] == 6_000
        assert completed[0]["payload"]["deliveryId"] == "d1"
        assert completed[0]["seq"] > started[0]["seq"]
    finally:
        app.close()


def test_turn_reports_are_fenced(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        # No session registered at all: a transient re-register signal.
        with pytest.raises(DaemonRequestError) as unregistered:
            app.started("t1")
        assert unregistered.value.code == "STALE_SESSION"

        app.register(session_ref="session-1")
        with pytest.raises(DaemonRequestError) as superseded:
            app.started("t1", session_ref="session-gone")
        assert superseded.value.code == "SESSION_SUPERSEDED"

        app.register(session_ref="session-2")
        with pytest.raises(DaemonRequestError) as stale:
            app.started("t1", session_ref="session-1")
        assert stale.value.code == "SESSION_SUPERSEDED"
        with pytest.raises(DaemonRequestError):
            app.ended(["t1"], session_ref="session-1")

        time.sleep(0.1)
        assert app.recorder.events == []  # nothing leaked through the fence
    finally:
        app.close()


def test_turn_reports_are_idempotent_per_report_id(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        first = app.started("t1")
        clock["now"] = 5_500
        second = app.started("t1")
        assert first == second  # the first acceptance time, not the retry's

        clock["now"] = 6_000
        end_first = app.ended(["t1"])
        clock["now"] = 6_500
        end_second = app.ended(["t1"])
        assert end_first["endedAtMs"] == end_second["endedAtMs"] == 6_000

        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 1
        )
        time.sleep(0.1)
        assert len(app.recorder.of("turn.started")) == 1
        assert len(app.recorder.of("turn.completed")) == 1
    finally:
        app.close()


def test_claude_stop_pulse_publishes_completed(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register(runtime="claude_interactive")
        app.started("s1")
        clock["now"] = 6_000
        ended = app.ended(["s1", "s2"])  # a Stop pulse: no status field

        assert ended["ok"] is True
        assert ended["endedAtMs"] == 6_000
        assert "turnCount" in ended and "lastTurnEndedAtMs" in ended
        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 2
        )
        time.sleep(0.1)
        assert app.recorder.of("turn.failed") == []
    finally:
        app.close()


def test_failed_status_publishes_turn_failed(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        app.started("f1")
        clock["now"] = 6_000
        app.ended(["f1"], status="failed")
        assert app.recorder.wait_for(lambda: len(app.recorder.of("turn.failed")) == 1)
        time.sleep(0.1)
        assert app.recorder.of("turn.completed") == []

        with pytest.raises(DaemonRequestError) as invalid:
            app.ended(["f2"], status="exploded")
        assert invalid.value.code == "INVALID_ARGUMENT"
        time.sleep(0.1)
        assert len(app.recorder.of("turn.failed")) == 1
    finally:
        app.close()


def _wall_clock() -> dict[str, int]:
    """A fake clock anchored at wall time.

    The pending.list fetched stamp uses the process clock, so the reminder
    window tests anchor the fake clock at wall time: a fetch taken between
    the calls always lands inside [startedAtMs, endedAtMs].
    """

    return {"now": time.time_ns() // 1_000_000}


def test_interactive_turn_drives_reply_reminder(tmp_path: Path) -> None:
    clock = _wall_clock()
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        app.seed_request("r1")

        app.started("w1")
        listed = app.app.handle(
            "message.pending.list",
            {"actor": A_NAME, "sessionRef": "session-1", "fetched": True},
        )
        assert [row["messageId"] for row in listed["messages"] if row["intent"] == "request"] == ["r1"]
        clock["now"] = clock["now"] + 60_000
        app.ended(["w1"])

        assert app.recorder.wait_for(
            lambda: len(app.inbox.system_notices(A_URI)) == 1
        )
        notice = app.inbox.system_notices(A_URI)[0]
        assert "r1" in notice.summary

        # The explicit reply closes the item: the next turn does not remind.
        app.app.handle(
            "message.reply",
            {
                "actor": A_NAME,
                "sessionRef": "session-1",
                "messageId": "r1",
                "summary": "here you go",
                "message": "here you go",
            },
        )
        clock["now"] = clock["now"] + 60_000
        app.started("w2")
        clock["now"] = clock["now"] + 60_000
        app.ended(["w2"])
        time.sleep(0.3)
        assert len(app.inbox.system_notices(A_URI)) == 1
    finally:
        app.close()


def test_started_after_fetch_misses_the_window(tmp_path: Path) -> None:
    clock = _wall_clock()
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        app.seed_request("r1")

        # The carrier fetched before reporting started: outside the window.
        app.app.handle(
            "message.pending.list",
            {"actor": A_NAME, "sessionRef": "session-1", "fetched": True},
        )
        clock["now"] = clock["now"] + 60_000
        app.started("w1")
        clock["now"] = clock["now"] + 60_000
        app.ended(["w1"])

        app.recorder.wait_for(lambda: len(app.recorder.of("turn.completed")) == 1)
        time.sleep(0.3)
        assert app.inbox.system_notices(A_URI) == ()
    finally:
        app.close()


def test_ended_retry_after_lost_response_publishes_once(tmp_path: Path) -> None:
    clock = _wall_clock()
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        app.seed_request("r1")
        app.started("w1")
        app.app.handle(
            "message.pending.list",
            {"actor": A_NAME, "sessionRef": "session-1", "fetched": True},
        )
        clock["now"] = clock["now"] + 60_000
        first = app.ended(["w1"])
        clock["now"] = clock["now"] + 60_000  # the carrier retries, same id
        retry = app.ended(["w1"])

        assert first["endedAtMs"] == retry["endedAtMs"]
        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 1
        )
        time.sleep(0.2)
        assert len(app.inbox.system_notices(A_URI)) == 1  # exactly one reminder
    finally:
        app.close()


def test_started_dedup_is_per_session_and_report(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register()
        app.started("x")
        clock["now"] = 5_500
        app.started("y")  # a new reportId never overwrites x's record
        clock["now"] = 6_000
        app.ended(["x"])

        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 1
        )
        started_x = app.recorder.of("turn.started")[0]
        completed_x = app.recorder.of("turn.completed")[0]
        assert started_x["payload"]["startedAtMs"] == 5_000
        assert completed_x["payload"]["startedAtMs"] == 5_000

        # After the session is replaced, old-sessionRef reports are rejected
        # and cannot touch the new session's ledger.
        assert (A_URI, "session-1") in app.app._carrier_turn_ledgers
        app.register(session_ref="session-2")
        # P5: a same-actor generation change drops the superseded session's
        # ledger instead of letting it live until daemon exit.
        assert (A_URI, "session-1") not in app.app._carrier_turn_ledgers
        with pytest.raises(DaemonRequestError):
            app.started("z", session_ref="session-1")
        with pytest.raises(DaemonRequestError):
            app.ended(["z"], session_ref="session-1")
        clock["now"] = 7_000
        app.started("z", session_ref="session-2")
        assert app.started("z", session_ref="session-2")["startedAtMs"] == 7_000
    finally:
        app.close()


def test_a_new_runtime_needs_no_contract_change(tmp_path: Path) -> None:
    """K8: an unknown runtime works through the same neutral contract."""

    from hyprial.daemon.impl.harnesses.carrier.contract import (
        IPC_SESSION_TURN_ENDED,
        IPC_SESSION_TURN_STARTED,
    )

    clock = {"now": 5_000}
    app = _TurnApp(tmp_path, clock)
    try:
        app.register(runtime="zz_test_interactive")
        app.seed_request("r1", summary="S")

        # 1. one daemon batch through the existing pending surface.
        app.started("k1")
        clock["now"] = 5_500
        listed = app.app.handle(
            "message.pending.batch",
            {"actor": A_NAME, "sessionRef": "session-1"},
        )
        row = next(item for item in listed["items"] if item["messageId"] == "r1")
        # The item carries the daemon text; the unknown runtime gets the same
        # contract, with the full body inside the untrusted-data delimiters.
        assert row["summary"] == "S"
        assert row["bodyBytes"] == len("please answer".encode())
        block = listed["delivery"]["text"]
        assert block.startswith("[Harness Network inbox batch")
        assert "please answer" in block

        # 2. turn start/end over the same two IPC names.
        assert IPC_SESSION_TURN_STARTED == "session.turn.started"
        assert IPC_SESSION_TURN_ENDED == "session.turn.ended"
        clock["now"] = 6_000
        ended = app.ended(["k1"], deliveryId="d1")
        assert ended["ok"] is True
        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 1
        )

        # 3. settle_hint and ack work for the unknown runtime's session.
        hint = app.app.handle(
            "message.settle_hint",
            {"actor": A_NAME, "sessionRef": "session-1", "messageId": "r1"},
        )
        assert hint["hint"] == "keep"  # no gate bound, request pending
        acked = app.app.handle(
            "message.ack",
            {"actor": A_NAME, "sessionRef": "session-1", "messageId": "r1"},
        )
        assert acked["ok"] is True
        hint_again = app.app.handle(
            "message.settle_hint",
            {"actor": A_NAME, "sessionRef": "session-1", "messageId": "r1"},
        )
        assert hint_again["hint"] == "ack"  # settled state, not the carrier

        # 4. an unregistered or invalidated session is still fenced.
        app.register(session_ref="session-2", runtime="zz_test_interactive")
        with pytest.raises(DaemonRequestError):
            app.started("k2", session_ref="session-1")
    finally:
        app.close()
