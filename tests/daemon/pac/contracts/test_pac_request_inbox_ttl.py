"""PAC workflow requests retain custody for their node deadline."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import yaml

from hyprial.daemon.impl.pac.actors.daemon import DaemonPacNotificationSender
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.dispatch.identity import dispatch_message_id
from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.daemon.impl.inbox.contracts.api import InboxPruneItem
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


AGENT = "agent:test:machine:worker"
USER = "user:test"


class RecordingSender:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}

    def send(self, **kwargs) -> str:
        key = kwargs["idempotency_key"]
        self.messages.setdefault(key, kwargs)
        return key


def _message(message_id: str, *, created_at_ms: int) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="pac-test",
        sender=USER,
        recipient=AGENT,
        payload=b"queued work",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


def test_delivery_frame_carries_explicit_expiry() -> None:
    message = replace(
        _message("fixed", created_at_ms=1_000), expires_at_ms=9_000
    )

    assert decode_delivery_frame(encode_delivery_frame(message)).expires_at_ms == 9_000


def test_pac_request_queued_behind_long_turn_outlives_generic_inbox_ttl(
    tmp_path,
) -> None:
    """A node request waits for its node deadline, not the ordinary hold TTL."""

    clock = [1_000]
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id="machine",
    )
    inbox.receive(_message("long-turn", created_at_ms=clock[0]), now_ms=clock[0])

    class Delivery:
        def deliver(self, **kwargs):
            message_id = dispatch_message_id(kwargs["effect_id"])
            values = {
                "message_id": message_id,
                "conversation_id": kwargs["conversation_id"],
                "sender": kwargs["sender"],
                "recipient": kwargs["target"],
                "payload": kwargs["text"].encode(),
                "intent": "request",
                "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
                "created_at_ms": clock[0],
            }
            if kwargs.get("expires_at_ms") is not None:
                values["expires_at_ms"] = kwargs["expires_at_ms"]
            result = inbox.receive(InboxMessage(**values), now_ms=clock[0])
            assert result.accepted
            return SimpleNamespace(message_id=message_id)

    application = SimpleNamespace(
        owner="test",
        node_id="machine",
        _pac_notification_io=Delivery(),
        _remote_workflow=None,
    )
    workflow = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=DaemonPacNotificationSender(application),
        clock_ms=lambda: clock[0],
        start_thread=False,
    )
    try:
        graph_id = workflow.start(
            yaml_text=yaml.safe_dump(
                {
                    "version": 2,
                    "name": "request-ttl",
                    "defaults": {"timeout": f"{2 * DEFAULT_HOLD_TTL_MS // 1000}s"},
                    "nodes": [{"id": "work", "owner": AGENT, "task": "do work"}],
                }
            ),
            sender=USER,
            operation_key="request-ttl",
        )["graphId"]
        workflow._tick()
        store = PacGraphStore(workflow.database, read_only=True)
        try:
            request_message_id = store._db.execute(
                "SELECT message_id FROM workflow_deliveries"
            ).fetchone()[0]
        finally:
            store.close()

        assert [item.message_id for item in inbox.pending_all()] == [
            "long-turn",
            request_message_id,
        ]
        clock[0] += DEFAULT_HOLD_TTL_MS + 1
        pruned = inbox.prune_inbox(now_ms=clock[0])
        node = workflow.status(run_id=graph_id)["nodes"][0]

        assert ([item.message_id for item in pruned], node["state"]) == (
            ["long-turn"],
            "requested",
        )
        assert inbox.next(AGENT).message_id == request_message_id
    finally:
        workflow.close()
        inbox.close()


def _workflow(tmp_path, sender: RecordingSender):
    clock = [1_000]
    service = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=lambda: clock[0],
        start_thread=False,
    )
    graph_id = service.start(
        yaml_text=yaml.safe_dump(
            {
                "version": 2,
                "name": "request-prune",
                "nodes": [{"id": "work", "owner": AGENT, "task": "do work"}],
            }
        ),
        sender=USER,
        operation_key="request-prune",
    )["graphId"]
    service._tick()
    return service, graph_id


def _bound_message(service: GraphWorkflowService) -> str:
    store = PacGraphStore(service.database, read_only=True)
    try:
        return str(
            store._db.execute(
                "SELECT message_id FROM workflow_deliveries ORDER BY rowid DESC"
            ).fetchone()[0]
        )
    finally:
        store.close()


def test_pruned_current_request_fails_node_and_names_reason_in_notice(tmp_path) -> None:
    sender = RecordingSender()
    workflow, graph_id = _workflow(tmp_path, sender)
    try:
        assert workflow.record_request_pruned(
            message_id=_bound_message(workflow), recipient=AGENT
        )
        workflow._tick()

        snapshot = workflow.status(run_id=graph_id)
        assert snapshot["state"] == "failed"
        assert snapshot["nodes"][0]["state"] == "failed"
        assert snapshot["nodes"][0]["reasonRef"] == "pac:request-expired"
        assert any(
            "node work failed: pac:request-expired" in item["text"]
            and item["recipient"] == USER
            for item in sender.messages.values()
        )
    finally:
        workflow.close()


def test_prune_of_stale_request_does_not_fail_current_generation(tmp_path) -> None:
    sender = RecordingSender()
    workflow, graph_id = _workflow(tmp_path, sender)
    try:
        stale_message_id = _bound_message(workflow)
        fresh_request = "workflow-request:fresh"
        fresh_message = "workflow-fresh-message"
        store = PacGraphStore(workflow.database)
        try:
            with store.write():
                store._db.execute(
                    "UPDATE workflow_nodes SET request_id=?,generation=generation+1 "
                    "WHERE graph_id=? AND node_id='work'",
                    (fresh_request, graph_id),
                )
                store._db.execute(
                    "INSERT INTO workflow_deliveries VALUES (?,?,?,?)",
                    (fresh_message, graph_id, "work", fresh_request),
                )
        finally:
            store.close()

        assert not workflow.record_request_pruned(
            message_id=stale_message_id, recipient=AGENT
        )
        node = workflow.status(run_id=graph_id)["nodes"][0]
        assert node["state"] == "requested"
        assert node["requestId"] == fresh_request
        assert node["reasonRef"] is None
    finally:
        workflow.close()


def test_daemon_routes_pruned_request_to_remote_pac_authority() -> None:
    calls = []
    application = SimpleNamespace(
        _workflow_service=SimpleNamespace(
            record_request_pruned=lambda **kwargs: calls.append(("local", kwargs))
            and False
        ),
        _remote_workflow=SimpleNamespace(
            request_pruned=lambda message_id, recipient: calls.append(
                ("remote", {"message_id": message_id, "recipient": recipient})
            )
            or True
        ),
        _log=lambda *args, **kwargs: calls.append(("log", (args, kwargs))),
    )
    item = InboxPruneItem(
        message_id="workflow-message",
        recipient=AGENT,
        reason="TTL_EXPIRED",
        created_at_ms=0,
        received_at_ms=1,
    )

    DaemonApplication._record_pruned_workflow_request(application, item)

    assert calls[0] == (
        "local",
        {"message_id": "workflow-message", "recipient": AGENT},
    )
    assert calls[1] == (
        "remote",
        {"message_id": "workflow-message", "recipient": AGENT},
    )
    assert calls[2][0] == "log"
