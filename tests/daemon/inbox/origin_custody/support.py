"""Shared real-actor fixtures for I5-3 inbox origin custody tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.kernel import HarnessLaunchSpec
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections
from tests.support.runtime.cell_delivery import AdmittingCellDelivery

SENDER = "agent:op:node-1:driver"


class SilentEvents:
    def publish(self, _event: object) -> None:
        return None


class RecordingHarnesses:
    def __init__(self, actors: tuple[str, ...] = ("worker",), *, answer: bool = True) -> None:
        self._actors = actors
        self.answer = answer
        self.envelopes: list[object] = []
        self.steers: list[object] = []
        self.generations: dict[str, int] = {actor: 1 for actor in actors}
        self.worker_refs: dict[tuple[str, str], str] = {}

    def streaming_actors(self) -> tuple[str, ...]:
        return self._actors

    def streaming_generations(self) -> dict[str, int]:
        return dict(self.generations)

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str]:
        return dict(self.worker_refs)

    def dispatch(self, envelope: object) -> bool:
        self.envelopes.append(envelope)
        return self.answer

    def steer(self, envelope: object) -> bool:
        self.steers.append(envelope)
        return True

    def stop(self) -> None:
        return None

    def status(self) -> tuple[()]:
        return ()

    def reconcile(self) -> int:
        return 0

    def reconcile_session_refs(self) -> None:
        return None

    def drain_results(self) -> tuple[()]:
        return ()

    def drain_progress(self) -> tuple[()]:
        return ()


class StubPresence:
    def actor_online(self, _actor: str) -> bool:
        return True

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def make_message(
    message_id: str,
    recipient: str,
    *,
    sender: str = SENDER,
    intent: str = "request",
    body: str | None = None,
    summary: str | None = None,
    created_at_ms: int = 1,
    expires_at_ms: int | None = None,
    idempotency_key: str | None = None,
) -> InboxMessage:
    text = body if body is not None else f"body-{message_id}"
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=sender,
        recipient=recipient,
        payload=json.dumps({"message": text}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
        expires_at_ms=expires_at_ms,
        idempotency_key=idempotency_key,
        summary=summary if summary is not None else f"summary-{message_id}",
    )


def make_notice(
    message_id: str,
    recipient: str,
    *,
    created_at_ms: int = 1,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender="system:hyprial:node-1",
        recipient=recipient,
        payload=json.dumps({"message": f"notice-{message_id}", "systemNotice": True}).encode(),
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=created_at_ms,
        summary=f"notice-{message_id}",
    )


def build_actor_app(tmp_path: Path, *, actors: tuple[str, ...] = ("alpha", "beta")) -> tuple[DaemonApplication, DeliveryCustodyCoordinator, DeliveryCustodyFacade]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    try:
        db = state_dir / "inbox.sqlite3"
        coordinator = DeliveryCustodyCoordinator(
            db,
            MemoryDeliveryTransport(),
            SilentEvents(),
            node_id="node-1",
            actor_host=app._agent_session_domains.actor_hosts.host_for("inbox-authority"),
            session_current=app._origin_session_current,
        )
        facade = DeliveryCustodyFacade(coordinator, db)
        app._inbox_coordinator = coordinator
        app._inbox = facade  # type: ignore[assignment]
        app._harnesses = RecordingHarnesses(())  # type: ignore[assignment]
        app._presence = StubPresence()  # type: ignore[assignment]
        app._cell_delivery = AdmittingCellDelivery()
        for name in actors:
            if app.agents.get(name) is None:
                app.handle("agent.create", {"name": name})
        return app, coordinator, facade
    except BaseException:
        app._close()
        app._close_home_lease()
        raise


def close_app(app: DaemonApplication, facade: DeliveryCustodyFacade) -> None:
    try:
        if getattr(app, "_inbox", None) is not facade:
            facade.close()
    finally:
        app._close()
        app._close_home_lease()


def register_session(app: DaemonApplication, actor: str, session_ref: str) -> str:
    result = app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "cwd": f"/work/{actor}",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )
    return str(result["actor"])


def unregister_session(app: DaemonApplication, actor: str, session_ref: str) -> None:
    app.handle(
        "session.unregister",
        {
            "actor": actor,
            "sessionRef": session_ref,
        },
    )


def register_managed_worker(
    app: DaemonApplication,
    name: str,
    worker_ref: str,
    *,
    harnesses: RecordingHarnesses,
    harness_kind: str = "codex",
) -> str:
    if app.agents.get(name) is None:
        app.handle("agent.create", {"name": name})
    spec = HarnessLaunchSpec(
        name=name,
        harness=harness_kind,
        cwd=f"/work/{name}",
        headless=True,
    )
    app.desired_state.upsert_harness(spec)
    harnesses.worker_refs[(harness_kind, name)] = worker_ref
    app._harnesses = harnesses  # type: ignore[assignment]
    return app.agents.require(name).uri


def build_bridge(
    tmp_path: Path,
    facade: DeliveryCustodyFacade,
    harnesses: RecordingHarnesses,
    *,
    actor_host: Any,
    harness_actor_uri: Any = None,
    desired_state: DesiredStateStore | None = None,
    actor_mode: bool = True,
) -> DaemonEventBridge:
    state_dir = tmp_path / "bridge-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return DaemonEventBridge(
        state_dir=state_dir,
        node_id="node-1",
        desired_state=desired_state or DesiredStateStore(state_dir / "desired-state.json"),
        transport=MemoryDeliveryTransport(),  # type: ignore[arg-type]
        inbox=facade,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=harness_actor_uri or (lambda name: name),
        cell_delivery=AdmittingCellDelivery(),  # type: ignore[arg-type]
        actor_mode=actor_mode,
        actor_host=actor_host,
    )
