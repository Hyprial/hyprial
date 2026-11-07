from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent


class _Harnesses:
    def streaming_actors(self) -> tuple[str, ...]:
        return ()

    def dispatch(self, _actor: str, _delivery: object) -> bool:
        return True


def test_attempt_generation_and_holds_are_owned_by_dispatch_mailbox(
    tmp_path: Path,
) -> None:
    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=object(),  # type: ignore[arg-type]
        harnesses=_Harnesses(),  # type: ignore[arg-type]
        actor_mode=True,
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


def test_blocked_worker_offer_does_not_stall_independent_worker(tmp_path: Path) -> None:
    blocked = threading.Event()
    release = threading.Event()
    second_done = threading.Event()

    class Harnesses:
        def streaming_actors(self) -> tuple[str, ...]:
            return ("worker-a", "worker-b")

        def dispatch(self, actor: str, _delivery: object) -> bool:
            if actor == "worker-a":
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
    tmp_path: Path,
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

        def dispatch(self, _actor: str, _delivery: object) -> bool:
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
    tmp_path: Path,
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

        def dispatch(self, _actor, _delivery):  # type: ignore[no-untyped-def]
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
    tmp_path: Path,
) -> None:
    probe_entered = threading.Event()
    release_probe = threading.Event()
    now_ms = [1]

    class Harnesses:
        def streaming_actors(self):  # type: ignore[no-untyped-def]
            return ("worker",)

        def dispatch(self, _actor, _delivery):  # type: ignore[no-untyped-def]
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
    tmp_path: Path,
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

        def dispatch(self, actor: str, _delivery: object) -> bool:
            calls[actor] += 1
            if actor == "worker-a":
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
        logger=lambda level, component, event, **fields: logged.append(
            (level, event, fields)
        ),
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
