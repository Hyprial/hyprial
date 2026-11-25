"""The GUI's non-consuming message query uses the existing daemon reader."""

from pathlib import Path

from hyprial.daemon.impl.application.messaging.inbox_surface.queries import _MessageQueriesMixin
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY


def test_query_binds_existing_non_consuming_reader_to_owner_user() -> None:
    bound = METHOD_REGISTRY.get("message.query")
    assert bound is not None
    assert bound.spec.http_exposed is True
    assert bound.spec.mutates is False and bound.spec.retry == "safe"
    assert bound.spec.effective_http_binding.identity.mode == "local-owner-user-selector"
    assert bound.spec.effective_http_binding.identity.param == "actor"
    assert bound.spec.effective_http_binding.readiness == "inbox-service"

    class App:
        def _ipc_message_query(self, params):
            assert params == {"actor": "user:owner", "view": "outbox"}
            return {"ok": True, "actor": "user:owner", "view": "outbox", "entries": []}

    result = METHOD_REGISTRY.invoke(App(), "message.query", {"actor": "user:owner", "view": "outbox"})
    assert result["entries"] == []


def test_query_validates_real_nonempty_queued_outbox_without_consuming_it(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())

    class App(_MessageQueriesMixin):
        def __init__(self) -> None:
            self._inbox = service

        def _message_consumer_keys(self, params):
            assert params == {"actor": "user:owner", "view": "outbox"}
            return ("user:owner",)

    try:
        for message_id, recipient in (
            ("queued-agent", "agent:owner:node-2:recipient"),
            ("queued-legacy", "channel:legacy"),
        ):
            submitted = service.submit(InboxMessage(
                message_id=message_id, conversation_id="queued-conversation",
                sender="user:owner", recipient=recipient,
                payload=b'{"message":"hello"}', intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE, created_at_ms=1,
            ), now_ms=1)
            assert submitted.accepted and submitted.queued

        result = METHOD_REGISTRY.invoke(
            App(), "message.query", {"actor": "user:owner", "view": "outbox"}
        )
        assert {entry["messageId"]: entry["undeliverableScheme"] for entry in result["entries"]} == {
            "queued-agent": False, "queued-legacy": True,
        }
        assert service.outbox_count() == 2
    finally:
        service.close()
