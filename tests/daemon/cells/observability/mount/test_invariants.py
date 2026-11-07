"""Cell reads preserve business custody and rebuild without durable cell state."""

import ast
from pathlib import Path

from hyprial.daemon.impl.composition.cell_observation import CellObservationView
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport, InboxMessage, DeliveryLifecycle
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
from tests.daemon.cells.observability.mount.test_mount import composed as composed, settle


def test_mount_reads_preserve_pending_message_sender_grants_and_home(composed, tmp_path):
    app, launcher, harness, transport, delivery, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    domains = app._agent_session_domains
    app.agents.grant_capability("worker", grant_id="channel-1", capability="channel",
                                scope='["test-channel"]', granted_by="user:owner", revision=1)
    grants = domains.agent.read_capability_grants("worker")
    agent = domains.agent.read_agent("worker")
    binding = domains.agent.read_binding(spec.actor)
    routes = app._routes.owners(spec.actor)
    inbox = InboxService(tmp_path / "cell-inbox.sqlite3", MemoryDeliveryTransport())
    try:
        inbox.receive(InboxMessage(
            message_id="cell-pending", conversation_id="cell-test",
            sender="agent:sender:node:source", recipient=spec.actor, payload=b'{}',
            intent="request", lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            idempotency_key=None, created_at_ms=0,
        ))
        pending = inbox.pending_all()
        assert pending[0].message_id == "cell-pending"
        assert pending[0].sender == "agent:sender:node:source"
        before_files = {p.relative_to(app.hyprial_home) for p in app.hyprial_home.rglob("*")}
        first = domains.cells.snapshot(spec.actor)
        for _ in range(5):
            assert domains.cells.snapshot(spec.actor) == first
            assert domains.cells.observe(spec.actor).current == first.lifecycle
        domains.cells.close()
        fresh = CellObservationView(domains.agent, domains.session, "node")
        try:
            fresh.attach_harnesses(harness.projection)
            recovered = fresh.snapshot(spec.actor)
            assert recovered.diagnostic_incarnation == first.diagnostic_incarnation
            assert recovered.lifecycle.epoch != first.lifecycle.epoch
            assert recovered.lifecycle.state == "running"
        finally:
            fresh.close()
        assert inbox.pending_all() == pending
        assert not inbox.has_fetched("cell-pending")
        assert domains.agent.read_capability_grants("worker") == grants
        assert domains.agent.read_agent("worker") == agent
        assert domains.agent.read_binding(spec.actor) == binding
        assert app._routes.owners(spec.actor) == routes
        assert {p.relative_to(app.hyprial_home) for p in app.hyprial_home.rglob("*")} == before_files
        assert launcher.starts == 1 and launcher.processes[0].running
    finally:
        inbox.close()


def test_cell_modules_contain_no_execution_or_business_commands():
    root = Path(__file__).resolve().parents[5] / "src/hyprial/daemon/impl"
    forbidden = {"ActorSpec", "Thread", "Queue", "Timer", "tell", "send", "receive",
                 "reply", "ack", "spawn", "stop", "kill", "approve", "submit"}
    for package in (root / "cells/observability", root / "composition/cell_observation"):
        for path in package.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                        node.func.id if isinstance(node.func, ast.Name) else None)
                    assert name not in forbidden, (path, name)
