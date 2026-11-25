from __future__ import annotations

from tests.support.runtime.cell_delivery import AdmittingCellDelivery

import json
import threading
import time
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.cells.actor.snapshot import CellActorState
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent


class _Harnesses:
    def streaming_actors(self) -> tuple[str, ...]:
        return ()

    def dispatch(self, _envelope: object) -> bool:
        return True


def test_attempt_generation_and_holds_are_owned_by_dispatch_mailbox(
    tmp_path: Path, actor_host,
) -> None:
    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=object(),  # type: ignore[arg-type]
        harnesses=_Harnesses(),  # type: ignore[arg-type]
        actor_mode=True,
        actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    bridge.start()
    message = InboxMessage(
        message_id="delivery-1",
        conversation_id="conversation-1",
        sender="agent:owner:node:sender",
        recipient="agent:owner:node:worker",
        payload=json.dumps({"message": "work"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    try:
        bridge._note_delivery_seen("worker", message)
        assert bridge._dispatch_state is not None
        projection = bridge._dispatch_state.projection()
        assert dict(projection.table_sizes)["inflight"] == 1
        bridge._note_delivery_progress("delivery-1", 2)
        finished = bridge._finish_attempt("delivery-1")
        assert finished is not None
        assert finished.last_progress_ms == 2
        assert bridge._attempt_generation["delivery-1"] == 1
        assert dict(bridge._dispatch_state.projection().table_sizes)["inflight"] == 0
    finally:
        bridge.stop()


def test_blocked_worker_offer_does_not_stall_independent_worker(tmp_path: Path, actor_host) -> None:
    blocked = threading.Event()
    release = threading.Event()
    second_done = threading.Event()

    class Harnesses:
        def streaming_actors(self) -> tuple[str, ...]:
            return ("worker-a", "worker-b")

        def dispatch(self, envelope: object) -> bool:
            if envelope.stamp.member == "worker-a":
                blocked.set()
                assert release.wait(2)
            else:
                second_done.set()
            return True

    messages = {
        actor: InboxMessage(
            message_id=f"delivery-{actor}",
            conversation_id=f"conversation-{actor}",
            sender="agent:owner:node:sender",
            recipient=actor,
            payload=b'{"message":"work"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
        for actor in ("worker-a", "worker-b")
    }

    class Inbox:
        def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
            return (messages[recipient],)

        def refresh_hold(self, _message_id: str) -> bool:
            return True

    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=Harnesses(),  # type: ignore[arg-type]
        actor_mode=True,
        actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    bridge.start()
    try:
        assert bridge._dispatch_harness_deliveries() == 0
        assert blocked.wait(1)
        assert second_done.wait(1)
        assert bridge._dispatch_offers is not None
        assert bridge._dispatch_offers.projection().pending >= 1
    finally:
        release.set()
        bridge.stop()


def test_blocked_harness_reconcile_does_not_stall_dispatch_tick(
    tmp_path: Path, actor_host,
) -> None:
    reconcile_entered = threading.Event()
    release_reconcile = threading.Event()
    prune_entered = threading.Event()
    release_prune = threading.Event()
    dispatched = threading.Event()
    message = InboxMessage(
        message_id="independent-delivery",
        conversation_id="independent-conversation",
        sender="agent:owner:node:sender",
        recipient="worker",
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )

    class Harnesses:
        def streaming_actors(self) -> tuple[str, ...]:
            return ("worker",)

        def reconcile(self) -> int:
            reconcile_entered.set()
            assert release_reconcile.wait(2)
            return 0

        def reconcile_session_refs(self) -> None:
            pass

        def dispatch(self, _envelope: object) -> bool:
            dispatched.set()
            return True

        def drain_results(self) -> tuple[()]:
            return ()

        def drain_progress(self) -> tuple[()]:
            return ()

    class Inbox:
        def pending_messages(self, _recipient: str) -> tuple[InboxMessage, ...]:
            return (message,)

        def refresh_hold(self, _message_id: str) -> bool:
            return True

        def retry_due(self) -> tuple[()]:
            return ()

        def prune_inbox(self) -> tuple[()]:
            prune_entered.set()
            assert release_prune.wait(2)
            return ()

    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=Harnesses(),  # type: ignore[arg-type]
        actor_mode=True,
        actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    bridge.start()
    try:
        started = time.monotonic()
        bridge.on_timer()
        assert time.monotonic() - started < 0.3
        assert reconcile_entered.wait(1)
        assert prune_entered.wait(1)
        assert dispatched.wait(1)
    finally:
        release_reconcile.set()
        release_prune.set()
        bridge.stop()


def test_blocked_progress_publication_does_not_hold_dispatch_worker(
    tmp_path: Path, actor_host,
) -> None:
    publication_entered = threading.Event()
    release_publication = threading.Event()
    dispatched = threading.Event()
    message = InboxMessage(
        message_id="progress-delivery",
        conversation_id="progress-conversation",
        sender="agent:owner:node:sender",
        recipient="worker",
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    progress = ProgressEvent(
        delivery_id=message.message_id,
        conversation_id=message.conversation_id,
        actor="worker", harness="pi", phase="working", seq=1,
        emitted_at_ms=1, summary="started",
    )

    class Harnesses:
        def streaming_actors(self):  # type: ignore[no-untyped-def]
            return ("worker",)

        def dispatch(self, _envelope):  # type: ignore[no-untyped-def]
            dispatched.set()
            return True

        def drain_progress(self):  # type: ignore[no-untyped-def]
            return (progress,)

    class Inbox:
        def pending_messages(self, _recipient):  # type: ignore[no-untyped-def]
            return (message,)

        def refresh_hold(self, _message_id):  # type: ignore[no-untyped-def]
            return True

        def submit_progress_event(self, _event, *, recipient):  # type: ignore[no-untyped-def]
            assert recipient == message.sender
            publication_entered.set()
            assert release_publication.wait(2)
            return True

    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state", node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=Harnesses(),  # type: ignore[arg-type]
        actor_mode=True,
        actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    bridge.start()
    try:
        started = time.monotonic()
        assert bridge._publish_harness_progress() == 0
        assert time.monotonic() - started < 0.3
        assert publication_entered.wait(1)
        bridge._dispatch_harness_deliveries()
        assert dispatched.wait(1)
    finally:
        release_publication.set()
        bridge.stop()


def test_blocked_stalled_notice_probe_does_not_hold_dispatch_tick(
    tmp_path: Path, actor_host,
) -> None:
    probe_entered = threading.Event()
    release_probe = threading.Event()
    now_ms = [1]

    class Harnesses:
        def streaming_actors(self):  # type: ignore[no-untyped-def]
            return ("worker",)

        def dispatch(self, _envelope):  # type: ignore[no-untyped-def]
            return False

        def reconcile(self):  # type: ignore[no-untyped-def]
            return 0

        def reconcile_session_refs(self):  # type: ignore[no-untyped-def]
            return None

        def drain_progress(self):  # type: ignore[no-untyped-def]
            return ()

        def drain_results(self):  # type: ignore[no-untyped-def]
            return ()

    class Inbox:
        def pending_messages(self, _recipient):  # type: ignore[no-untyped-def]
            return ()

        def is_acknowledged(self, _delivery_id):  # type: ignore[no-untyped-def]
            probe_entered.set()
            assert release_probe.wait(2)
            return False

        def retry_due(self):  # type: ignore[no-untyped-def]
            return ()

        def prune_inbox(self):  # type: ignore[no-untyped-def]
            return ()

    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=Harnesses(),  # type: ignore[arg-type]
        actor_mode=True,
        clock_ms=lambda: now_ms[0],
        actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    bridge.start()
    message = InboxMessage(
        message_id="stalled-delivery",
        conversation_id="stalled-conversation",
        sender="agent:owner:node:sender",
        recipient="agent:owner:node:worker",
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    bridge._note_delivery_seen("worker", message)
    now_ms[0] = 1_000_000
    try:
        started = time.monotonic()
        bridge.on_timer()
        assert time.monotonic() - started < 0.3
        assert probe_entered.wait(1)
    finally:
        release_probe.set()
        bridge.stop()


def test_production_dispatch_skips_a_timed_out_worker_and_keeps_peers_moving(
    tmp_path: Path, actor_host,
) -> None:
    """actor_mode=False is the production composition path.

    A worker whose dispatch never returns must not hold the shared reconcile
    tick or its peers.  Once the per-agent budget elapses the runtime skips
    that worker for the round, logs one bounded timeout event, and leaves the
    message pending for a later round instead of marking it delivered.
    """

    blocked = threading.Event()
    release = threading.Event()
    fast_done = threading.Event()
    calls = {"worker-a": 0, "worker-b": 0}
    logged: list[tuple[str, str, dict[str, object]]] = []

    class Harnesses:
        def streaming_actors(self) -> tuple[str, ...]:
            return ("worker-a", "worker-b")

        def dispatch(self, envelope: object) -> bool:
            calls[envelope.stamp.member] += 1
            if envelope.stamp.member == "worker-a":
                blocked.set()
                release.wait(2)
            else:
                fast_done.set()
            return True

    messages = {
        actor: InboxMessage(
            message_id=f"delivery-{actor}",
            conversation_id=f"conversation-{actor}",
            sender="agent:owner:node:sender",
            recipient=actor,
            payload=b'{"message":"work"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
        for actor in ("worker-a", "worker-b")
    }

    class Inbox:
        def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
            return (messages[recipient],)

        def refresh_hold(self, _message_id: str) -> bool:
            return True

    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=Harnesses(),  # type: ignore[arg-type]
        actor_host=actor_host,
        logger=lambda level, component, event, **fields: logged.append(
            (level, event, fields)
        ),
        cell_delivery=AdmittingCellDelivery(),
    )
    bridge._dispatch_agent_timeout_ms = 50
    bridge.start()
    try:
        started = time.monotonic()
        assert bridge._dispatch_harness_deliveries() == 1
        assert time.monotonic() - started < 0.5
        assert blocked.wait(1)
        assert fast_done.wait(1)

        time.sleep(0.08)
        bridge._dispatch_harness_deliveries()
        timeouts = [
            fields
            for _level, event, fields in logged
            if event == "harness.dispatch.agent_timeout"
        ]
        assert len(timeouts) == 1
        assert timeouts[0]["agent"] == "worker-a"
        assert timeouts[0]["code"] == "DISPATCH_AGENT_TIMEOUT"
        assert timeouts[0]["durationMs"] >= 50

        bridge._dispatch_harness_deliveries()
        assert calls["worker-a"] == 1
        assert len(
            [
                fields
                for _level, event, fields in logged
                if event == "harness.dispatch.agent_timeout"
            ]
        ) == 1
    finally:
        release.set()
        bridge.stop()


class _CellHarnesses:
    """One-arg envelope dispatch/steer fakes over the wired port shape."""

    def __init__(self) -> None:
        self.envelopes: list[object] = []
        self.steers: list[object] = []
        self.steer_answer = True
        self.answer = True

    def streaming_actors(self) -> tuple[str, ...]:
        return ("worker",)

    def dispatch(self, envelope: object) -> bool:
        self.envelopes.append(envelope)
        return self.answer

    def steer(self, envelope: object) -> bool:
        self.steers.append(envelope)
        return self.steer_answer


class _CellInbox:
    def __init__(self, message: InboxMessage) -> None:
        self.messages = [message]
        self.fetches: list[str] = []

    def pending_messages(self, _recipient: str) -> tuple[InboxMessage, ...]:
        return tuple(self.messages)

    def fetch_pending(self, recipient: str, only: frozenset[str] | None = None) -> bool:
        del recipient
        self.fetches.extend(sorted(only or ()))
        return True

    def refresh_hold(self, _message_id: str) -> bool:
        return True


class _CellPort:
    """A headless cell delivery port over one mutable snapshot source."""

    def __init__(self, snapshot) -> None:  # type: ignore[no-untyped-def]
        self.snapshot = snapshot
        self.offered: list[tuple[str, tuple[str, ...]]] = []

    def headless_envelope(self, worker: str, delivery) -> object:  # type: ignore[no-untyped-def]
        from hyprial.daemon.impl.cells.actor.contracts import (
            CellDispatchEnvelope,
            CellDispatchStamp,
        )

        snapshot = self.snapshot() if callable(self.snapshot) else self.snapshot
        from hyprial.daemon.impl.cells.actor.snapshot import headless_admission

        decision = headless_admission(snapshot)
        if not decision.admitted:
            return None
        assert snapshot is not None
        members = tuple(delivery.message_ids) if delivery.message_ids else ()
        origin = members if members else (delivery.delivery_id,)
        self.offered.append((worker, origin))
        return CellDispatchEnvelope(
            stamp=CellDispatchStamp(
                snapshot.cell_id,
                decision.member,
                snapshot.epoch,
                snapshot.effective_revision,
                origin,
            ),
            input=delivery,
        )

    def steer_envelope(self, stamp, delivery_id: str) -> object:  # type: ignore[no-untyped-def]
        if stamp is None:
            return None
        from hyprial.daemon.impl.composition.cell_delivery.headless import (
            build_steer_envelope,
        )

        return build_steer_envelope(stamp, delivery_id)


def _cell_snapshot(
    *, state: object = None, entry: str | None = "agent:owner:node:worker"
) -> object:
    from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot

    return CellActorSnapshot(
        cell_id="c.worker",
        epoch="ce-1",
        revision=7,
        effective_revision=7,
        entry_member=entry,
        state=state if state is not None else CellActorState.ACTIVE,
    )


def _worker_message() -> InboxMessage:
    return InboxMessage(
        message_id="cell-delivery-1",
        conversation_id="cell-conversation",
        sender="agent:owner:node:sender",
        recipient="agent:owner:node:worker",
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _cell_bridge(
    tmp_path: Path,
    harnesses: _CellHarnesses,
    inbox: _CellInbox,
    port: _CellPort,
    *,
    actor_host,
) -> DaemonEventBridge:
    return DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: f"agent:owner:node:{name}",
        cell_delivery=port,  # type: ignore[arg-type]
        actor_mode=True,
        actor_host=actor_host,
    )


def test_a_deferring_cell_leaves_the_row_pending_and_unfetched(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    inbox = _CellInbox(_worker_message())
    port = _CellPort(_cell_snapshot(state=CellActorState.EMPTY, entry=None))
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        assert bridge._dispatch_harness_deliveries() == 0
        assert harnesses.envelopes == []
        assert inbox.fetches == []
        assert inbox.pending_messages("agent:owner:node:worker") == (
            _worker_message(),
        )
    finally:
        bridge.stop()


def test_an_admitting_cell_offers_one_envelope_and_remembers_the_stamp(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    message = _worker_message()
    inbox = _CellInbox(message)
    port = _CellPort(_cell_snapshot())
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while not harnesses.envelopes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(harnesses.envelopes) == 1
        envelope = harnesses.envelopes[0]
        assert envelope.stamp.cell_id == "c.worker"
        assert envelope.stamp.member == "agent:owner:node:worker"
        assert envelope.stamp.revision == 7
        assert envelope.stamp.origin_message_ids == ("cell-delivery-1",)
        assert port.offered == [("worker", ("cell-delivery-1",))]
        attempt = bridge._inflight["cell-delivery-1"]
        assert attempt is not None
        assert attempt.stamp == envelope.stamp
    finally:
        bridge.stop()


def test_the_admission_read_not_the_offer_time_decides_the_member(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    inbox = _CellInbox(_worker_message())
    holder: dict[str, object] = {"snapshot": _cell_snapshot()}
    port = _CellPort(lambda: holder["snapshot"])
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while not harnesses.envelopes and time.monotonic() < deadline:
            time.sleep(0.01)
        first = harnesses.envelopes[0]
        holder["snapshot"] = _cell_snapshot(entry="agent:owner:node:other")
        assert first.stamp.member == "agent:owner:node:worker"
        second_message = InboxMessage(
            message_id="cell-delivery-2",
            conversation_id="cell-conversation",
            sender="agent:owner:node:sender",
            recipient="agent:owner:node:worker",
            payload=b'{"message":"work"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=2,
        )
        inbox.messages.append(second_message)
        inbox.messages.pop(0)
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while len(harnesses.envelopes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(harnesses.envelopes) == 2
        second = harnesses.envelopes[1]
        assert second.stamp.member == "agent:owner:node:other"
    finally:
        bridge.stop()


def test_steer_reuses_the_attempt_member_and_never_the_current_entry(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    message = _worker_message()
    inbox = _CellInbox(message)
    holder: dict[str, object] = {"snapshot": _cell_snapshot()}
    port = _CellPort(lambda: holder["snapshot"])
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while not harnesses.envelopes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert harnesses.envelopes
        holder["snapshot"] = _cell_snapshot(entry="agent:owner:node:other")
        attempt = bridge._inflight["cell-delivery-1"]
        assert attempt is not None
        assert bridge._steer_stalled_delivery(attempt) is True
        assert len(harnesses.steers) == 1
        steered = harnesses.steers[0]
        assert steered.stamp.member == "agent:owner:node:worker"
        assert steered.stamp.revision == 7
        assert steered.stamp.origin_message_ids == ("cell-delivery-1",)
    finally:
        bridge.stop()


def test_a_stampless_attempt_never_steers(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    inbox = _CellInbox(_worker_message())
    port = _CellPort(None)
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        bridge._note_delivery_seen("worker", _worker_message())
        attempt = bridge._inflight["cell-delivery-1"]
        assert attempt is not None
        assert bridge._steer_stalled_delivery(attempt) is False
        assert harnesses.steers == []
    finally:
        bridge.stop()


def test_concurrent_dispatch_scans_offer_one_envelope_only(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    inbox = _CellInbox(_worker_message())
    port = _CellPort(_cell_snapshot())
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        barrier = threading.Barrier(2)

        def scan() -> None:
            barrier.wait(2)
            bridge._dispatch_actor_deliveries("worker", frozenset())

        threads = [threading.Thread(target=scan) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        deadline = time.monotonic() + 5
        while len(harnesses.envelopes) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(harnesses.envelopes) == 1
    finally:
        bridge.stop()


def test_a_cell_epoch_change_never_touches_an_offered_works_stamp(
    tmp_path: Path, actor_host,
) -> None:
    harnesses = _CellHarnesses()
    message = _worker_message()
    inbox = _CellInbox(message)
    holder: dict[str, object] = {"snapshot": _cell_snapshot()}
    port = _CellPort(lambda: holder["snapshot"])
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while not harnesses.envelopes and time.monotonic() < deadline:
            time.sleep(0.01)
        offered = harnesses.envelopes[0]
        from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot

        holder["snapshot"] = CellActorSnapshot(
            cell_id="c.worker",
            epoch="ce-2",
            revision=8,
            effective_revision=8,
            entry_member="agent:owner:node:worker",
            state=CellActorState.ACTIVE,
        )
        attempt = bridge._inflight["cell-delivery-1"]
        assert attempt is not None
        assert attempt.stamp == offered.stamp
        assert offered.stamp.epoch == "ce-1"
        assert "cell-delivery-1" not in bridge._attempt_generation
        steered = port.steer_envelope(attempt.stamp, "cell-delivery-1")
        assert steered.stamp.epoch == "ce-1"
        assert steered.stamp.revision == 7
    finally:
        bridge.stop()


def test_origin_claim_allows_one_headless_or_interactive_holder(
    tmp_path: Path, actor_host,
) -> None:
    """M-F16 closed by I5-3: inbox OriginCustody grants at most one holder."""
    from hyprial.daemon.impl.inbox import (
        DeliveryCustodyCoordinator,
        DeliveryCustodyFacade,
        MemoryDeliveryTransport,
    )
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    worker_uri = "agent:owner:node:worker"
    active_sessions = {(worker_uri, "session-1")}

    class _Events:
        def publish(self, _event: object) -> None:
            return None

    def _make_pair(subdir: str):
        root = tmp_path / subdir
        db = root / "inbox.sqlite3"
        coord = DeliveryCustodyCoordinator(
            db, MemoryDeliveryTransport(), _Events(),
            node_id="node", actor_host=actor_host,
            session_current=lambda actor, ref: (actor, ref) in active_sessions,
        )
        facade = DeliveryCustodyFacade(coord, db)
        harnesses = _CellHarnesses()
        bridge = DaemonEventBridge(
            state_dir=root / "state", node_id="node",
            desired_state=DesiredStateStore(root / "desired.json"),
            transport=object(), inbox=facade, harnesses=harnesses,  # type: ignore[arg-type]
            harness_actor_uri=lambda name: f"agent:owner:node:{name}",
            cell_delivery=AdmittingCellDelivery(),  # type: ignore[arg-type]
            actor_mode=True, actor_host=actor_host,
        )
        return facade, harnesses, bridge

    inbox_first, harnesses_first, bridge_first = _make_pair("headless-first")
    try:
        assert inbox_first.receive(_worker_message()).accepted
        bridge_first.start()
        bridge_first._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while not harnesses_first.envelopes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert harnesses_first.envelopes, "headless admission never took the row"
        assert inbox_first.fetch_pending(
            worker_uri, only=frozenset({"cell-delivery-1"}),
            origin_claim=OriginClaimant.session(worker_uri, "session-1"),
        ) == ()
        assert inbox_first.has_fetched("cell-delivery-1") is False
    finally:
        bridge_first.stop()
        inbox_first.close()

    inbox_second, harnesses_second, bridge_second = _make_pair("interactive-first")
    try:
        assert inbox_second.receive(_worker_message()).accepted
        claimed = inbox_second.fetch_pending(
            worker_uri, only=frozenset({"cell-delivery-1"}),
            origin_claim=OriginClaimant.session(worker_uri, "session-1"),
        )
        assert [m.message_id for m in claimed] == ["cell-delivery-1"]
        bridge_second.start()
        assert bridge_second._dispatch_harness_deliveries() == 0
        assert harnesses_second.envelopes == []
    finally:
        bridge_second.stop()
        inbox_second.close()


def test_a_deferred_row_never_starts_a_no_progress_clock(
    tmp_path: Path, actor_host,
) -> None:
    """Cell model I5-2 r2 item 3: no attempt without an admitted offer.

    A row the cell keeps deferring must not enter the in-flight map, so the
    availability lane reports nothing however long the deferral lasts, and
    the row stays pending for the next round.
    """
    harnesses = _CellHarnesses()
    inbox = _CellInbox(_worker_message())
    port = _CellPort(_cell_snapshot(state=CellActorState.EMPTY, entry=None))
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        for _ in range(3):
            assert bridge._dispatch_harness_deliveries() == 0
        assert harnesses.envelopes == []
        assert bridge._inflight.items() == ()
        assert bridge._report_stalled_deliveries() == 0
        assert inbox.fetches == []
        assert inbox.pending_messages("agent:owner:node:worker") == (
            _worker_message(),
        )
    finally:
        bridge.stop()


def _submission_refusal_bridge(
    tmp_path: Path, harnesses: _CellHarnesses, inbox: _CellInbox, port: _CellPort,
    *, actor_host,
) -> DaemonEventBridge:
    return DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: f"agent:owner:node:{name}",
        cell_delivery=port,  # type: ignore[arg-type]
        actor_host=actor_host,
    )


def test_an_admitted_delivery_keeps_its_clock_when_submission_fails(
    tmp_path: Path, actor_host,
) -> None:
    """Round 3 item 1: the clock starts at admission, before submission.

    A row whose cell admits it but whose submission is refused keeps the
    in-flight attempt, so a silent worker is reported by the no-progress
    lane exactly as before the cell gate; the row itself stays pending.
    """
    import os

    harnesses = _CellHarnesses()
    harnesses.answer = False
    harnesses.steer_answer = False
    inbox = _CellInbox(_worker_message())
    port = _CellPort(_cell_snapshot())
    now_ms = [1]
    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: f"agent:owner:node:{name}",
        cell_delivery=port,  # type: ignore[arg-type]
        clock_ms=lambda: now_ms[0],
        actor_host=actor_host,
    )
    os.environ["HYPRIAL_T1_BUDGET_SECONDS"] = "1"
    try:
        assert bridge._dispatch_harness_deliveries() == 0
        attempts = list(bridge._inflight.items())
        assert len(attempts) == 1
        delivery_id, attempt = attempts[0]
        assert delivery_id == "cell-delivery-1"
        assert attempt.stamp is not None
        assert attempt.stamp.member == "agent:owner:node:worker"
        now_ms[0] += 100_000
        bridge._report_stalled_deliveries()
        reported = bridge._inflight.get("cell-delivery-1")
        assert reported is not None and reported.reported
        assert inbox.pending_messages("agent:owner:node:worker") == (
            _worker_message(),
        )
    finally:
        del os.environ["HYPRIAL_T1_BUDGET_SECONDS"]
        bridge.stop()


def test_a_reoffered_row_adopts_the_newest_stamp_and_keeps_its_clock(
    tmp_path: Path, actor_host,
) -> None:
    """Round 3 item 1: the stamp follows the most recent submitted envelope.

    After an execution-side refusal the cell entry may move; the next
    admitted submission replaces the attempt's stamp (so a later steer
    targets the member that actually received the work) without touching
    the clock's first-seen time.
    """
    harnesses = _CellHarnesses()
    message = _worker_message()
    inbox = _CellInbox(message)
    holder = {"snapshot": _cell_snapshot(entry="agent:owner:node:worker")}
    port = _CellPort(lambda: holder["snapshot"])
    bridge = _cell_bridge(tmp_path, harnesses, inbox, port, actor_host=actor_host)
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while not harnesses.envelopes and time.monotonic() < deadline:
            time.sleep(0.01)
        first_seen = bridge._inflight.get("cell-delivery-1")
        assert first_seen is not None
        assert first_seen.stamp is not None
        assert first_seen.stamp.member == "agent:owner:node:worker"
        holder["snapshot"] = _cell_snapshot(
            entry="agent:owner:node:replacement"
        )
        bridge._dispatch_harness_deliveries()
        deadline = time.monotonic() + 5
        while len(harnesses.envelopes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        updated = bridge._inflight.get("cell-delivery-1")
        assert updated is not None
        assert updated.stamp is not None
        assert updated.stamp.member == "agent:owner:node:replacement"
        assert updated.last_progress_ms == first_seen.last_progress_ms
    finally:
        bridge.stop()
