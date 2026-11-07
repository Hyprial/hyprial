"""H1-H10: the headless carrier settles per hint and never auto-replies.

wp5.md §3.1-§3.3, §5.  The bridge assembly follows
``tests/daemon/harnesses/actors/test_harness_reply_settlement.py``: a real
``DeliveryCustodyFacade`` (the authority) and a scripted harness double, so
every stamp, hint and ack below is the authority's own.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.links.ports import Allow
from hyprial.daemon.impl.runtime import DaemonEventBridge
from tests.support.links.gate import permissive_admission_slot

NODE = "wp5a-headless"
AGENT = f"agent:op:{NODE}:sprite"
SENDER = f"agent:op:{NODE}:asker"
BODY = "BODY-SENTINEL-1 the real prompt text"


class _Transport:
    def close(self) -> None:
        return None


class _ScriptedHarnesses:
    """Results handed out once; one streaming actor that records dispatches."""

    def __init__(self) -> None:
        self.results: list[HarnessResult] = []
        self.dispatched: list[Any] = []

    def drain_results(self) -> tuple[HarnessResult, ...]:
        drained, self.results = tuple(self.results), []
        return drained

    def reconcile(self) -> int:
        return 0

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def streaming_actors(self) -> tuple[str, ...]:
        return ("sprite",)

    def dispatch(self, actor: str, delivery: Any) -> bool:
        self.dispatched.append(delivery)
        return True

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_failed_events(self) -> tuple[()]:
        return ()

    def status(self) -> tuple[()]:
        return ()


class _HintGate:
    """A gate whose settle verdict is scripted; admits everything."""

    def __init__(self, hint: str = "keep") -> None:
        self.hint = hint
        self.hint_calls: list[str] = []

    def admit_send(self, **_kwargs: object) -> Allow:
        return Allow()

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        return True

    def admit_dispatch(self, **_kwargs: object) -> bool:
        return True

    def settle_hint(self, *, actor: str, message_id: str) -> str:
        self.hint_calls.append(message_id)
        return self.hint


class _SubmitSpy:
    """Records every submit that passes through to the real inbox."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.submitted: list[InboxMessage] = []

    def submit(self, message: InboxMessage, **kwargs: object) -> object:
        self.submitted.append(message)
        return self._inner.submit(message, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _TimesOutFirstFetch:
    """The real inbox, except the next fetch_pending times out."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.fail = True
        self.fetches = 0

    def fetch_pending(self, recipient: str, **kwargs: object) -> object:
        self.fetches += 1
        if self.fail:
            self.fail = False
            raise InboxAuthorityTimeout("fetch_pending did not confirm in time")
        return self._inner.fetch_pending(recipient, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _desired(path: Path, harness: str = "pi") -> DesiredStateStore:
    desired = path / "desired-state.json"
    Path(f"{desired}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": harness,
                        "name": "sprite",
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    return DesiredStateStore(desired)


def _inbox(database: Path) -> DeliveryCustodyFacade:
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        type("_Events", (), {"publish": staticmethod(lambda _event: None)})(),
        node_id=NODE,
        # Keep this suite about settlement; dispatch admission is explicitly
        # permissive (wp4 T13 premise).
        service_options={"link_admission": permissive_admission_slot()},
    )
    return DeliveryCustodyFacade(coordinator, database)


def _bridge(
    tmp_path: Path,
    inbox: object,
    harnesses: _ScriptedHarnesses,
    clock: dict[str, int],
    *,
    gate: object | None = None,
    harness: str = "pi",
    logs: Any = None,
) -> DaemonEventBridge:
    slot = None
    if gate is not None:
        slot = LinkAdmission()
        slot.bind(gate)
    value = DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=_desired(tmp_path, harness),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: f"agent:op:{NODE}:{name}",
        clock_ms=lambda: clock["now"],
        logger=logs,
        link_admission=slot,
    )
    return value


def _message(
    message_id: str,
    *,
    intent: str = "request",
    summary: str | None = "S",
    recipient: str = AGENT,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=SENDER,
        recipient=recipient,
        payload=json.dumps({"message": BODY}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        summary=summary,
    )


def _fetched_at(database: Path, message_id: str) -> int | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT fetched_at_ms FROM inbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _answer(message_id: str, text: str = "ANSWER") -> HarnessResult:
    return HarnessResult(message_id, AGENT, HarnessResultStatus.COMPLETED, output=text)


def test_completed_request_submits_no_reply(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    real_inbox = _inbox(database)
    spy = _SubmitSpy(real_inbox)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, spy, harnesses, clock)
    assert real_inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(_answer("r1", "ANSWER"))
        assert bridge.on_timer().harness_results == 1

        assert real_inbox.pending_messages(SENDER) == ()
        assert [item for item in spy.submitted if item.intent == "reply"] == []
        assert real_inbox.outbox_items() == ()
    finally:
        bridge.stop()
        real_inbox.close()


def test_keep_leaves_request_pending_stamped_and_not_redispatched(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)  # no gate: hint is keep
    assert inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(_answer("r1"))
        assert bridge.on_timer().harness_results == 1

        pending = {item.message_id for item in inbox.pending_messages(AGENT)}
        assert pending == {"r1"}
        assert _fetched_at(database, "r1") is not None

        clock["now"] = 2_000
        bridge.on_timer()
        clock["now"] = 3_000
        bridge.on_timer()
        assert len(harnesses.dispatched) == 1  # never re-injected
    finally:
        bridge.stop()
        inbox.close()


def test_ack_hint_acks_without_reply(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    spy = _SubmitSpy(inbox)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    gate = _HintGate(hint="ack")
    bridge = _bridge(tmp_path, spy, harnesses, clock, gate=gate)
    assert inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(_answer("r1"))
        assert bridge.on_timer().harness_results == 1

        assert inbox.pending_messages(AGENT) == ()
        assert gate.hint_calls == ["r1"]
        assert [item for item in spy.submitted if item.intent == "reply"] == []
    finally:
        bridge.stop()
        spy._inner.close()


def test_reply_and_event_rows_are_acked_without_a_gate(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    assert inbox.receive(_message("rep", intent="reply")).accepted
    assert inbox.receive(_message("evt", intent="event")).accepted

    bridge.start()
    try:
        # One daemon batch coalesces both rows; each result still settles its
        # own message id.
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(_answer("rep"))
        harnesses.results.append(_answer("evt"))
        assert bridge.on_timer().harness_results == 2

        # Ruling 56: a reply or event owes nothing back, gate or no gate.
        assert inbox.pending_messages(AGENT) == ()
    finally:
        bridge.stop()
        inbox.close()


def test_dispatched_prompt_is_the_daemon_batch_text(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    row = _message("r1")
    assert inbox.receive(row).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        delivered = harnesses.dispatched[0]
        # One daemon-rendered batch: the body is inline (inside the untrusted
        # data delimiters), the cursor/members are claimed, and the scalar
        # header is absent.
        assert delivered.message.startswith("[Harness Network inbox batch")
        assert "--- BEGIN UNTRUSTED MESSAGE DATA ---" in delivered.message
        assert BODY in delivered.message
        assert delivered.batch_cursor
        assert delivered.message_ids == ("r1",)
        assert "[Harness Network message from" not in delivered.message
    finally:
        bridge.stop()
        inbox.close()


def test_failed_turn_is_not_settled_and_retries(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    assert inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(
            HarnessResult(
                "r1",
                AGENT,
                HarnessResultStatus.FAILED,
                error="connection reset",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            )
        )
        assert bridge.on_timer().harness_results == 1

        # The batch read is a consuming read and stamped fetched when it was
        # captured; the failed turn still settles nothing, and the failure
        # backoff is what makes the row dispatchable again.
        assert _fetched_at(database, "r1") is not None
        assert {m.message_id for m in inbox.pending_messages(AGENT)} == {"r1"}
        clock["now"] = 2_000  # first backoff (1_000 ms) has elapsed
        assert bridge.on_timer().harness_deliveries == 1
        assert len(harnesses.dispatched) == 2
    finally:
        bridge.stop()
        inbox.close()


def test_settle_deferral_retries_without_rerunning_the_turn(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    real_inbox = _inbox(database)
    flaky = _TimesOutFirstFetch(real_inbox)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, flaky, harnesses, clock)
    assert real_inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        assert _fetched_at(database, "r1") is None
        harnesses.results.append(_answer("r1"))
        bridge.on_timer()  # fetch retry succeeds, then the result settles
        assert not bridge._pending_settle_results
        assert _fetched_at(database, "r1") is not None

        bridge._dispatch_harness_deliveries()
        assert len(harnesses.dispatched) == 1
        assert flaky.fetches >= 2
        assert len(harnesses.dispatched) == 1
    finally:
        bridge.stop()
        real_inbox.close()


def test_python_worker_still_receives_the_body(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock, harness="user-proxy")
    row = _message("r1")
    assert inbox.receive(row).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        delivered = harnesses.dispatched[0]
        # Ruling 60: a python_worker has no model context; it gets the body.
        assert delivered.message == BODY
    finally:
        bridge.stop()
        inbox.close()


@pytest.mark.parametrize("via_authority", [False, True])
def test_body_read_then_transient_failure_still_retries(
    tmp_path: Path, via_authority: bool
) -> None:
    database = tmp_path / "inbox.sqlite3"
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}

    def build() -> tuple[DaemonEventBridge, Any]:
        if via_authority:
            inbox: Any = _inbox(database)
        else:
            transport = MemoryDeliveryTransport()
            inbox = InboxService(
                database,
                transport,
                node_id=NODE,
                link_admission=permissive_admission_slot(),
            )
        return (
            _bridge(tmp_path, inbox, harnesses, clock),
            inbox,
        )

    bridge, inbox = build()
    assert inbox.receive(_message("r1")).accepted
    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        # The model reads the body (the read path stamps fetched first).
        inbox.fetch_pending(AGENT, only=frozenset({"r1"}))
        harnesses.results.append(
            HarnessResult(
                "r1",
                AGENT,
                HarnessResultStatus.FAILED,
                error="connection reset",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            )
        )
        assert bridge.on_timer().harness_results == 1
        assert _fetched_at(database, "r1") is not None
    finally:
        bridge.stop()
        inbox.close()

    # Rebuild both bridge and inbox over the same SQLite file mid-backoff.
    clock["now"] = 1_500
    bridge, inbox = build()
    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 0
        clock["now"] = 2_000  # backoff elapsed: the stamped row retries
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(_answer("r1"))
        assert bridge.on_timer().harness_results == 1
        # The successful turn clears the non-terminal retry settlement and
        # then keeps the row (no gate): it is never dispatched again.
        for tick in range(3):
            clock["now"] = 3_000 + tick
            assert bridge.on_timer().harness_deliveries == 0
        assert len(harnesses.dispatched) == 2

        with sqlite3.connect(database) as connection:
            settlements = connection.execute(
                "SELECT COUNT(*) FROM harness_failure_settlements "
                "WHERE message_id = ? AND terminal = 0",
                ("r1",),
            ).fetchone()[0]
            attempts = connection.execute(
                "SELECT COUNT(*) FROM harness_failure_attempts WHERE message_id = ?",
                ("r1",),
            ).fetchone()[0]
        assert settlements == 0
        assert attempts >= 1  # append-only evidence survives
    finally:
        bridge.stop()
        inbox.close()


def test_permanent_failure_is_not_redispatched_after_body_read(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    assert inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        inbox.fetch_pending(AGENT, only=frozenset({"r1"}))
        harnesses.results.append(
            HarnessResult(
                "r1",
                AGENT,
                HarnessResultStatus.FAILED,
                error="permission denied",
                failure_code="PROVIDER_PERMISSION_DENIED",
            )
        )
        assert bridge.on_timer().harness_results == 1

        clock["now"] = 10_000
        bridge.on_timer()
        clock["now"] = 60_000
        bridge.on_timer()
        assert len(harnesses.dispatched) == 1  # terminal: never again
    finally:
        bridge.stop()
        inbox.close()


def test_late_batch_success_keeps_original_member_claims(
    tmp_path: Path,
) -> None:
    """A timed-out batch offer still owns every member when it later accepts.

    The timeout returns the tick to its caller, not the batch to the inbox.
    _drain_dispatch_offer_outcomes must restore the same claim that an
    immediate acceptance would have recorded, so a later row cannot form a
    second batch while the first turn still owns its members.
    """

    import threading

    release = threading.Event()

    class Harnesses(_ScriptedHarnesses):
        def dispatch(self, actor: str, delivery: Any) -> bool:
            self.dispatched.append(delivery)
            if len(self.dispatched) == 1:
                release.wait(2)
            return True

    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = Harnesses()
    bridge = _bridge(tmp_path, inbox, harnesses, {"now": 1_000})
    bridge._dispatch_agent_timeout_ms = 30
    for message_id in ("r1", "r2"):
        assert inbox.receive(_message(message_id)).accepted
    bridge.start()
    try:
        assert bridge._dispatch_harness_deliveries() == 0
        assert harnesses.dispatched[0].message_ids == ("r1", "r2")
        assert bridge._dispatch_harness_deliveries() == 0
        assert len(harnesses.dispatched) == 1
        release.set()
        assert bridge._dispatch_offers.drain(1)
        assert bridge._drain_dispatch_offer_outcomes() == 1
        assert bridge._batch_claims.get("sprite") == ("r1", "r2")
        assert inbox.receive(_message("r3")).accepted
        bridge._dispatch_harness_deliveries()
        assert [delivery.message_ids for delivery in harnesses.dispatched] == [
            ("r1", "r2")
        ]
    finally:
        release.set()
        bridge.stop()
        inbox.close()


def test_settled_batch_result_does_not_revive_a_late_offer_claim(
    tmp_path: Path,
) -> None:
    """A result that settled before the late offer must not be revived.

    The offer can accept after the harness already produced a result.  The
    result released (or never recorded) the batch claim; the late completion
    must not restore that claim and block a younger row forever.
    """

    import threading

    release = threading.Event()
    blocked = {"value": False}

    class Harnesses(_ScriptedHarnesses):
        def dispatch(self, actor: str, delivery: Any) -> bool:
            self.dispatched.append(delivery)
            if not blocked["value"]:
                blocked["value"] = True
                release.wait(2)
            return True

    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = Harnesses()
    bridge = _bridge(tmp_path, inbox, harnesses, {"now": 1_000})
    bridge._dispatch_agent_timeout_ms = 30
    for message_id in ("r1", "r2"):
        assert inbox.receive(_message(message_id)).accepted
    bridge.start()
    try:
        assert bridge._dispatch_harness_deliveries() == 0
        assert harnesses.dispatched[0].message_ids == ("r1", "r2")
        harnesses.results.append(
            HarnessResult(
                "r1",
                AGENT,
                HarnessResultStatus.COMPLETED,
                output="done",
                message_ids=("r1", "r2"),
            )
        )
        bridge.on_timer()
        release.set()
        assert bridge._dispatch_offers.drain(1)
        bridge._drain_dispatch_offer_outcomes()
        assert not bridge._batch_claims
        assert not bridge._pending_batch_claims
        assert inbox.receive(_message("r3")).accepted
        bridge._dispatch_harness_deliveries()
        assert any("r3" in delivery.message_ids for delivery in harnesses.dispatched[1:])
    finally:
        release.set()
        bridge.stop()
        inbox.close()


def test_batch_is_not_fetched_until_the_worker_accepts(tmp_path: Path) -> None:
    import threading

    release = threading.Event()
    blocked = {"value": False}

    class Harnesses(_ScriptedHarnesses):
        def dispatch(self, actor: str, delivery: Any) -> bool:
            self.dispatched.append(delivery)
            if not blocked["value"]:
                blocked["value"] = True
                release.wait(2)
            return True

    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = Harnesses()
    bridge = _bridge(tmp_path, inbox, harnesses, {"now": 1_000})
    bridge._dispatch_agent_timeout_ms = 30
    for message_id in ("r1", "r2"):
        assert inbox.receive(_message(message_id)).accepted
    bridge.start()
    try:
        assert bridge._dispatch_harness_deliveries() == 0
        assert _fetched_at(database, "r1") is None
        release.set()
        assert bridge._dispatch_offers.drain(1)
        bridge._drain_dispatch_offer_outcomes()
        assert _fetched_at(database, "r1") is not None
        assert _fetched_at(database, "r2") is not None
    finally:
        release.set()
        bridge.stop()
        inbox.close()


def test_rejected_batch_offer_leaves_rows_unfetched_and_retryable(
    tmp_path: Path,
) -> None:
    attempts = {"value": 0}

    class Harnesses(_ScriptedHarnesses):
        def dispatch(self, actor: str, delivery: Any) -> bool:
            attempts["value"] += 1
            self.dispatched.append(delivery)
            return attempts["value"] > 1

    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = Harnesses()
    bridge = _bridge(tmp_path, inbox, harnesses, {"now": 1_000})
    for message_id in ("r1", "r2"):
        assert inbox.receive(_message(message_id)).accepted
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        assert not bridge._batch_claims
        assert _fetched_at(database, "r1") is None
        bridge._dispatch_harness_deliveries()
        assert bridge._batch_claims.get("sprite") == ("r1", "r2")
        assert _fetched_at(database, "r1") is not None
    finally:
        bridge.stop()
        inbox.close()


def test_batch_workflow_flag_is_the_or_of_its_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating the batch workflow aggregate to False must be observable."""

    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ScriptedHarnesses()
    bridge = _bridge(tmp_path, inbox, harnesses, {"now": 1_000})
    for message_id in ("r1", "r2"):
        assert inbox.receive(_message(message_id)).accepted
    monkeypatch.setattr(
        bridge,
        "_is_workflow_message",
        lambda message: message.message_id == "r2",
    )
    bridge.start()
    try:
        assert bridge._dispatch_harness_deliveries() == 1
        assert harnesses.dispatched[0].message_ids == ("r1", "r2")
        assert harnesses.dispatched[0].workflow is True
    finally:
        bridge.stop()
        inbox.close()
