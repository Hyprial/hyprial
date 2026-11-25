"""Re-creating a destroyed agent's name makes a new agent (2026-11-19).

The registry keeps no tombstone of used names: ``agent.create`` with the name
of a destroyed agent succeeds with a new id, and nothing of the old agent is
inherited through the name -- no grants, pending rows, routines or
default conversations.  Mail addressed to the old name-form URI is not
delivered to the new agent, and a name-form address of a live agent is still
refused at the send boundary with its id address.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.conversations.ids import default_conversation_id, endpoint_key
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.kernel import DaemonRequestError, canonical_agent_uri, ipc_errors
from tests.kernel.actors.identity.support import application
from tests.support.agent_destroy import destroy_agent
from tests.support.runtime._owned_applications import close_application

OWNER = "op"
NODE = "node-1"
NAME_URI = canonical_agent_uri(OWNER, NODE, "worker")
USER = f"user:{OWNER}"


@pytest.fixture
def app(tmp_path: Path) -> Iterator[DaemonApplication]:
    instance = application(tmp_path)
    try:
        instance.handle("agent.create", {"name": "driver"})
        yield instance
    finally:
        close_application(instance)


def _send(app: DaemonApplication, target: str) -> dict:
    return app.handle(
        "message.send",
        {"from": "driver", "to": [target], "message": "hi", "summary": "hi"},
    )


def _refused(app: DaemonApplication, target: str) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as caught:
        _send(app, target)
    return caught.value


def _row(message_id: str, recipient: str = NAME_URI) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-late",
        sender="channel:lark:test",
        recipient=recipient,
        payload=json.dumps({"message": "for the old worker"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_recreating_a_destroyed_name_is_a_new_agent_that_inherits_nothing(app) -> None:
    app.handle("agent.create", {"name": "worker"})
    old = app.agents.require("worker")
    registry = app._agent_session_domains._registry
    app.agents.grant_capability(
        "worker", grant_id="tool-1", capability="tool-surface",
        scope='["test-tool"]', granted_by=USER, revision=1,
    )
    assert app._inbox.receive(_row("queued", old.uri)).accepted
    assert app._inbox.pending_messages(old.uri)

    destroy_agent(app, "worker")
    app.handle("agent.create", {"name": "worker"})
    new = app.agents.require("worker")

    assert new.agent_id != old.agent_id
    assert new.entity_token != old.entity_token
    assert new.uri == canonical_agent_uri(OWNER, NODE, new.agent_id)
    assert all(grant.grant_id != "tool-1" for grant in registry.capability_grants("worker"))
    assert not app._inbox.pending_messages(new.uri)
    assert app._consumer_read_keys(new.uri) == (new.uri,)
    service = app._routine_service
    app._routine_service = SimpleNamespace(  # type: ignore[assignment]
        list=lambda: {
            "routines": [
                {"name": "old", "actorOwnership": "borrowed", "actor": old.uri}
            ]
        }
    )
    try:
        assert app._routines_bound_to(new.uri) == []
    finally:
        app._routine_service = service
    assert default_conversation_id(endpoint_key(new.uri), endpoint_key(USER)) != (
        default_conversation_id(endpoint_key(old.uri), endpoint_key(USER))
    )


def test_mail_for_the_old_name_never_reaches_a_new_same_name_agent(app) -> None:
    """H2: a row arriving after the destroy drain is not delivered to the new agent."""

    app.handle("agent.create", {"name": "worker"})
    old = app.agents.require("worker")
    destroy_agent(app, "worker")

    assert app._inbox.receive(_row("late-before")).accepted
    assert app._local_recipient_claimed(NAME_URI) is False
    unresolved = _refused(app, NAME_URI)
    assert unresolved.code == ipc_errors.TARGET_UNRESOLVED
    assert _refused(app, old.uri).code == ipc_errors.TARGET_UNRESOLVED

    app.handle("agent.create", {"name": "worker"})
    new = app.agents.require("worker")
    assert app._inbox.receive(_row("late-after")).accepted

    assert app._local_recipient_claimed(NAME_URI) is False
    assert app._local_recipient_claimed(new.uri) is True
    listed = app.handle("message.pending.list", {"actor": "worker"})
    assert listed["messages"] == []
    assert app.handle("message.pending.list", {"actor": new.uri})["messages"] == []


def test_a_live_agents_name_form_address_is_refused_with_its_id_address(app) -> None:
    """What a name still resolves to: the bare name, never the name-form URI.

    The CLI's bare ``worker`` resolves to the live agent's id address; the
    name-form URI of a live agent is AGENT_ADDRESS_RETIRED carrying that id
    address, before and after the name was reused.
    """

    app.handle("agent.create", {"name": "worker"})
    first = app.agents.require("worker")
    (delivery,) = _send(app, "worker")["deliveries"]
    assert delivery["target"] == first.uri
    retired = _refused(app, NAME_URI)
    assert retired.code == ipc_errors.AGENT_ADDRESS_RETIRED
    assert retired.data == {"target": NAME_URI, "address": first.uri, "agentId": first.agent_id}

    destroy_agent(app, "worker")
    app.handle("agent.create", {"name": "worker"})
    second = app.agents.require("worker")
    (delivery,) = _send(app, "worker")["deliveries"]
    assert delivery["target"] == second.uri
    assert _refused(app, NAME_URI).data["address"] == second.uri
