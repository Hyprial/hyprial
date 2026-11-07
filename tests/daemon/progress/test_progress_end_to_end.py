"""Route-C end-to-end seams (T10).

These tests close the two failure modes unit tests cannot see: the daemon's
composition root must actually pass ``progress_node`` to its node endpoint,
and a worker observation must travel pump -> supervisor -> runtime -> inbox
without entering the prompt-dispatch path.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.application.wiring import transport as daemon_application
from hyprial.daemon.impl.application.netendpoints import endpoints as transport_owner
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from hyprial.daemon.impl.harnesses.streaming.protocol import ProgressObservation
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxService, MemoryDeliveryTransport
from tests.support.links.gate import permissive_admission_slot
from hyprial.daemon.impl.inbox.tracking.progress import decode_progress_event

OWNER = "op"
NODE = "node-1"
WORKER = f"agent:{OWNER}:{NODE}:worker"
SENDER = f"agent:{OWNER}:{NODE}:requester"


@dataclass(frozen=True)
class _Outcome:
    result: str
    is_error: bool = False


class _ScriptedClient:
    running = True
    pid = None

    async def __aenter__(self) -> "_ScriptedClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):  # type: ignore[no-untyped-def]
        yield ProgressObservation(phase="turn-start", summary="turn started")
        yield ProgressObservation(
            phase="tool-call",
            summary="calling read",
            tool_call_id="call-1",
            tool_name="read",
        )
        yield ProgressObservation(
            phase="turn-end", summary="turn settled", terminal=True
        )
        yield _Outcome("authoritative answer")

    async def interrupt(self) -> None:
        return None


class _Launcher:
    def __init__(self) -> None:
        self.process: StreamingTurnProcess | None = None

    def start(self, spec: HarnessLaunchSpec) -> StreamingTurnProcess:
        self.process = StreamingTurnProcess(
            harness=spec.harness,
            label="scripted progress worker",
            client_factory=_ScriptedClient,
            thread_name="test-route-c-e2e",
        )
        return self.process


class _Transport:
    def close(self) -> None:
        return None

def _wait_for(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the scripted worker")


def test_worker_progress_reaches_sender_without_becoming_a_prompt(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    # Explicit permissive premise: SENDER→WORKER is agent-to-agent through a
    # slot with no real gate yet (item 3).
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        link_admission=permissive_admission_slot(),
    )
    launcher = _Launcher()
    harnesses = ManagedHarnessRuntime(launcher)  # type: ignore[arg-type]
    runtime = DaemonEventBridge(
        state_dir=state_dir,
        node_id=NODE,
        desired_state=DesiredStateStore(state_dir / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,
        harnesses=harnesses,
        harness_actor_uri=lambda name: f"agent:{OWNER}:{NODE}:{name}",
    )
    original = InboxMessage(
        message_id="delivery-progress-e2e",
        conversation_id="conversation-progress-e2e",
        sender=SENDER,
        recipient=WORKER,
        payload=json.dumps({"message": "do the work"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    accepted = inbox.receive(original)
    assert accepted.accepted
    spec = HarnessLaunchSpec(harness="pi", name="worker", headless=True)
    runtime.desired_state.upsert_harness(spec)
    harnesses.restore((spec,))

    runtime.start()
    try:
        # First tick dispatches the durable request to the scripted pump.
        runtime.on_timer()
        process = launcher.process
        assert process is not None
        _wait_for(lambda: len(process._turn_runtime.read_results()) == 1)

        # Second tick publishes drained progress, then completes the terminal
        # result through the ordinary reply+ack path.
        summary = runtime.on_timer()

        assert summary.harness_progress == 2
        assert summary.harness_results == 1
        stored = inbox.list_progress_events(SENDER, delivery_id=original.message_id)
        assert len(stored) == 2
        decoded = [decode_progress_event(item.payload) for item in stored]
        # Per-tick coalescing retains the latest tool event and latest other
        # event; the merged turn-start is charged as a drop.
        assert [item.phase for item in decoded if item is not None] == [
            "tool-call",
            "turn-end",
        ]
        assert sum(
            item.dropped_since_seq for item in decoded if item is not None
        ) == 1
        assert all(item.recipient == SENDER for item in stored)
        assert all(item.sender == WORKER for item in stored)
        # The structural route-C defense: no progress row is visible to the
        # notice-to-prompt path or to the sender's normal inbox FIFO.
        assert inbox.system_notices(SENDER) == ()
        assert all(
            message.intent != "progress"
            for message in inbox.pending_messages(SENDER)
        )
        # Terminal authority: the completed turn settles per hint and never
        # replies.  With no gate bound the hint is keep, so the request row
        # stays pending in the worker's inbox; no reply is held anywhere.
        assert inbox.outbox_items() == ()
        assert len(inbox.pending_messages(WORKER)) == 1
    finally:
        runtime.stop()
        harnesses.stop()
        inbox.close()


class _Registration:
    def close(self) -> None:
        return None


class _CompositionTransport:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.liveliness: list[str] = []

    def declare_liveliness(self, key: str) -> _Registration:
        self.liveliness.append(key)
        return _Registration()

    def observe_liveliness(
        self,
        _key_expr: str,
        _callback: object,
        *,
        history: bool = True,
    ) -> _Registration:
        return _Registration()

    def subscribe(self, _key_expr: str, _callback: object) -> _Registration:
        return _Registration()

    def close(self) -> None:
        return None


class _CompositionDirectory:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def actor_online(self, _actor: str) -> bool:
        return False

    def presence_projection(self):  # type: ignore[no-untyped-def]
        from hyprial.daemon.impl.transport.presence_actor import PresenceProjection

        return PresenceProjection(1)

    def bind_actor_online(self, _observer: object):  # type: ignore[no-untyped-def]
        return lambda: None

    def observe_actor_online(self, _observer: object):  # type: ignore[no-untyped-def]
        return lambda: None

    def online_actors(self) -> tuple[()]:
        return ()

    def online_mailboxes(self) -> tuple[()]:
        return ()

    def close(self) -> None:
        return None


class _CompositionDelivery:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.actors: list[str] = []

    def bind_receiver(self, _receiver: object) -> None:
        return None

    def register_actor(self, actor: str) -> _Registration:
        self.actors.append(actor)
        return _Registration()


class _CompositionBridge:
    def __init__(self, inner: _CompositionDelivery, *_args: object) -> None:
        self.inner = inner


class _CompositionInbox:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    @property
    def alarm_emitter(self) -> object:
        """The PAC workflow service reads the inbox alarm emitter at wiring
        time; the composition test only needs the attribute to exist."""
        return _CompositionAlarm()

    def receive(self, *_args: object, **_kwargs: object) -> None:
        return None

    def delivery_status(self, *_args: object, **_kwargs: object) -> None:
        return None

    def close(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def pending_all(self) -> tuple[()]:
        return ()

    def consumption_state(self, _message_id: str) -> str:
        return "unknown"

    def outbox_recipient_page(
        self, *, after: str | None = None, limit: int = 64
    ) -> tuple[()]:
        del after, limit
        return ()


class _CompositionInboxCoordinator:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.inbox = _CompositionInbox()

    def submit(self, _command: object) -> object:
        return object()


class _CompositionAlarm:
    """A stub alarm for the composition test's inbox fake."""

    def emit(self, *_args: object, **_kwargs: object) -> None:
        return None


class _CompositionAdapters:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def stop(self) -> None:
        return None


class _CompositionHarnesses:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def status(self) -> tuple[()]:
        return ()

    def stop(self) -> None:
        return None

    drain_complete = True


class _CompositionEndpoint:
    instances: list["_CompositionEndpoint"] = []

    def __init__(self, *_args: object, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False
        type(self).instances.append(self)

    def close(self) -> None:
        self.closed = True


class _CompositionRuntime:
    def __init__(self, **_kwargs: object) -> None:
        return None

    def start(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            attempted=0,
            restored=0,
            failed=0,
            previous_run_unclean=False,
            missing_interactive=(),
        )

    def stop(self) -> None:
        return None


class _CloseOnly:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def close(self) -> None:
        return None


class _OrgEndpoint(_CloseOnly):
    def publish_accepted(self) -> None:
        return None


def test_application_composition_wires_progress_only_on_the_node_endpoint(
    monkeypatch, tmp_path: Path  # type: ignore[no-untyped-def]
) -> None:
    """The #205 follow-up: hop 8b is asserted at the composition root."""

    _CompositionEndpoint.instances = []
    monkeypatch.setattr(transport_owner, "ZenohTransport", _CompositionTransport)
    monkeypatch.setattr(
        transport_owner, "TransportSessionAuthority", lambda session: session
    )
    monkeypatch.setattr(daemon_application, "LivelinessDirectory", _CompositionDirectory)
    monkeypatch.setattr(daemon_application, "ZenohDeliveryTransport", _CompositionDelivery)
    monkeypatch.setattr(daemon_application, "LocalFirstDeliveryTransport", _CompositionDelivery)
    monkeypatch.setattr(daemon_application, "LarkReplyBridgeTransport", _CompositionBridge)
    monkeypatch.setattr(
        daemon_application,
        "DeliveryCustodyCoordinator",
        _CompositionInboxCoordinator,
    )
    monkeypatch.setattr(
        daemon_application,
        "DeliveryCustodyFacade",
        lambda coordinator, _database: coordinator.inbox,
    )
    monkeypatch.setattr(daemon_application, "AdapterRuntime", _CompositionAdapters)
    monkeypatch.setattr(daemon_application, "HarnessRuntimeActor", _CompositionHarnesses)
    monkeypatch.setattr(
        daemon_application,
        "HarnessPortClient",
        lambda actor, _events: actor,
    )
    monkeypatch.setattr(daemon_application, "ZenohInboxEndpoint", _CompositionEndpoint)
    monkeypatch.setattr(daemon_application, "DeliveryStatusEndpoint", _CloseOnly)
    monkeypatch.setattr(daemon_application, "ReceiverUserDelivery", _CloseOnly)
    monkeypatch.setattr(daemon_application, "ZenohUserDeliveryEndpoint", _CloseOnly)
    monkeypatch.setattr(daemon_application, "ZenohUserDeliveryTransport", _CloseOnly)
    monkeypatch.setattr(daemon_application, "OrgContextMesh", _OrgEndpoint)
    monkeypatch.setattr(daemon_application, "DaemonEventBridge", _CompositionRuntime)
    monkeypatch.setattr(
        DaemonApplication,
        "_start_lifecycle_manager",
        lambda self, **_kwargs: None,
    )

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        hyprial_home=tmp_path,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    try:
        app._start_runtime()

        assert app._recipient_wakes is not None
        assert app._stop_recipient_wake_observer is not None

        node_endpoint = app._endpoint
        assert isinstance(node_endpoint, _CompositionEndpoint)
        assert node_endpoint.kwargs["notice_node"] == NODE
        assert node_endpoint.kwargs["progress_node"] == NODE
        # Every other endpoint constructed by this composition root stays deaf
        # to progress; only the node-level subscriber receives route-C events.
        assert _CompositionEndpoint.instances[0] is node_endpoint
    finally:
        app._close()
        app.agents.close()
