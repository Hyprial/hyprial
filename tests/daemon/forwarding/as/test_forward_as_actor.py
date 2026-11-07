"``DaemonApplication._forward_as_actor``: the daemon-owned send boundary.\n\nThe runtime seam (tests/daemon/forwarding/seam/test_forward_seam.py) trusts the outcome's code; this\npins where the code comes from.  An address that can never resolve must come\nback as ``FORWARD_TARGET_UNKNOWN`` (permanent, the sender is told once); a\nreal recipient must be accepted under the daemon service identity with the\nworker retained as visible provenance, idempotently.\n"

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import FORWARD_TARGET_UNKNOWN, DaemonApplication
from hyprial.daemon.impl.alias_resolver import (
    AliasKind,
    AliasRecord,
    AliasResolver,
    StaticAliasSource,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

PROXY = "agent:op:node-1:allen-proxy"


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


def _app(tmp_path: Path) -> DaemonApplication:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()
    app.handle("agent.create", {"name": "allen-proxy"})
    return app


@pytest.fixture
def app(tmp_path: Path):
    instance = _app(tmp_path)
    try:
        yield instance
    finally:
        # The injected status-only harness facade owns no processes. All real
        # daemon actors and database handles still require explicit close.
        instance._harnesses = None
        try:
            instance._close()
        finally:
            instance._close_home_lease()


def _original(message_id: str = "m-1") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c-1",
        sender="adapter:lark:allen-feishu",
        recipient=PROXY,
        payload=b'{"message":"@asker hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_a_registered_agent_is_sent_to_as_the_proxy(app: DaemonApplication) -> None:
    app.handle("agent.create", {"name": "asker"})

    outcome = app._forward_as_actor(_original(), "asker", "hi")

    assert outcome.accepted, outcome
    sent = [item.message for item in app._inbox.outbox_items()]
    assert [(m.sender, m.recipient) for m in sent] == [
        (app._dispatch_service_actor, "agent:op:node-1:asker")
    ]
    assert json.loads(sent[0].payload)["message"] == "hi"
    assert json.loads(sent[0].payload)["origin"] == {
        "via": "daemon",
        "onBehalfOf": PROXY,
    }


def test_a_redelivered_forward_does_not_send_twice(app: DaemonApplication) -> None:
    app.handle("agent.create", {"name": "asker"})

    app._forward_as_actor(_original(), "asker", "hi")
    app._forward_as_actor(_original(), "asker", "hi")

    assert len(app._inbox.outbox_items()) == 1


def test_a_name_that_matches_nothing_is_an_unknown_target(
    app: DaemonApplication,
) -> None:
    outcome = app._forward_as_actor(_original(), "nobody", "hi")

    assert not outcome.accepted
    assert outcome.failure_code == FORWARD_TARGET_UNKNOWN
    # The underlying refusal stays readable for the person who gets the notice.
    assert outcome.error and outcome.error.startswith("TARGET_IS_NODE")


def test_a_canonical_unknown_local_target_keeps_the_queued_send_contract(
    app: DaemonApplication,
) -> None:
    """P2 adds no request-level refusal for an unclaimed canonical local
    target: message.send queues it with TARGET_UNRESOLVED (dev contract,
    org-improver ruling 2026-10-07), so the forward is accepted exactly as
    on dev.  Changing that is a separate, explicit contract PR."""
    user = AliasRecord(AliasKind.USER, "op", "u.owner")
    device = AliasRecord(AliasKind.DEVICE, "node-1", "d.device")
    app._alias_resolver = AliasResolver(
        StaticAliasSource(
            (user, device),
            local={AliasKind.USER: user, AliasKind.DEVICE: device},
        )
    )

    outcome = app._forward_as_actor(
        _original(), "agent:op:node-1:nobody", "hi"
    )

    assert outcome.accepted


def test_a_route_on_an_unconfigured_adapter_is_an_unknown_target(
    app: DaemonApplication,
) -> None:
    outcome = app._forward_as_actor(_original(), "route:no-such-adapter:dm", "hi")

    assert not outcome.accepted
    assert outcome.failure_code == FORWARD_TARGET_UNKNOWN


def test_a_malformed_route_is_an_unknown_target(app: DaemonApplication) -> None:
    outcome = app._forward_as_actor(_original(), "route:only-two", "hi")

    assert not outcome.accepted
    assert outcome.failure_code == FORWARD_TARGET_UNKNOWN
