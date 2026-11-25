"""UUID addressing §2.3: the send boundary fails loud on local name addresses.

With id-form minting on, a local ``agent:`` URI whose actor segment is a
name no longer delivers to a registered agent: a live agent's actor or
display name is refused with ``AGENT_ADDRESS_RETIRED`` (carrying the id
address), and an id or name nothing on this node claims -- a destroyed
agent's name included -- is refused with a fatal ``TARGET_UNRESOLVED``
instead of queueing forever.
Daemon service actors (not squire, an ordinary agent since Allen's
2026-11-06 ruling), unregistered claimed identities and bare-name input keep
working.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.kernel import canonical_agent_uri
from tests.daemon.routing.failloud.conftest import NODE, OWNER, name_uri, send

UNKNOWN_ID = "a.aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _refusal(app: DaemonApplication, target: str) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as failure:
        send(app, target)
    return failure.value


def test_live_actor_name_is_retired_with_the_id_address(flipped) -> None:
    worker = flipped.agents.require("worker")
    assert worker.uri == canonical_agent_uri(OWNER, NODE, worker.agent_id)

    error = _refusal(flipped, name_uri("worker"))

    assert error.code == "AGENT_ADDRESS_RETIRED"
    assert error.data == {
        "target": name_uri("worker"),
        "address": worker.uri,
        "agentId": worker.agent_id,
    }
    assert worker.uri in str(error)


def test_live_and_previous_display_names_are_retired(flipped) -> None:
    worker = flipped.agents.require("worker")
    flipped.agents.rename("worker", "builder")

    live = _refusal(flipped, name_uri("builder"))
    assert live.code == "AGENT_ADDRESS_RETIRED"
    assert live.data == {
        "target": name_uri("builder"),
        "address": worker.uri,
        "agentId": worker.agent_id,
    }

    flipped.agents.rename("worker", "maker")
    previous = _refusal(flipped, name_uri("builder"))
    assert previous.code == "TARGET_UNRESOLVED"
    assert previous.data == {"target": name_uri("builder")}


def test_name_of_a_destroyed_agent_is_unresolved(flipped) -> None:
    flipped.handle("agent.create", {"name": "temp"})
    assert flipped.agents.destroy("temp")

    error = _refusal(flipped, name_uri("temp"))

    assert error.code == "TARGET_UNRESOLVED"
    assert error.data == {"target": name_uri("temp")}


def test_unknown_id_is_unresolved_and_fatal(flipped) -> None:
    error = _refusal(flipped, name_uri(UNKNOWN_ID))

    assert error.code == "TARGET_UNRESOLVED"
    assert error.data == {"target": name_uri(UNKNOWN_ID)}


def test_unclaimed_name_is_unresolved_and_fatal(flipped) -> None:
    outbox_before = flipped._inbox.outbox_count()

    error = _refusal(flipped, name_uri("ghost"))

    assert error.code == "TARGET_UNRESOLVED"
    assert error.data == {"target": name_uri("ghost")}
    assert flipped._inbox.outbox_count() == outbox_before


def test_id_address_and_bare_name_deliver(flipped) -> None:
    worker = flipped.agents.require("worker")

    for target in (worker.uri, "worker"):
        result = send(flipped, target)
        (delivery,) = result["deliveries"]
        assert delivery["target"] == worker.uri
        assert delivery["accepted"] is True
        assert "code" not in delivery


@pytest.mark.parametrize("service", ["pac-expansion", "_turn-hooks"])
def test_service_actor_names_keep_name_delivery(flipped, service) -> None:
    result = send(flipped, name_uri(service))

    (delivery,) = result["deliveries"]
    assert delivery["target"] == name_uri(service)
    assert delivery["accepted"] is True


def test_squire_is_an_ordinary_agent_at_the_send_boundary(flipped) -> None:
    """Allen 2026-11-06: squire is not a service actor.  Its name address
    gets exactly the agent rows: unclaimed before it exists, retired while
    it lives, and unclaimed again once destroyed -- never delivered by name
    to an inbox row nobody drains."""

    never = _refusal(flipped, name_uri("squire"))
    assert never.code == "TARGET_UNRESOLVED"
    assert never.data == {"target": name_uri("squire")}

    flipped.handle("agent.create", {"name": "squire"})
    squire = flipped.agents.require("squire")
    live = _refusal(flipped, name_uri("squire"))
    assert live.code == "AGENT_ADDRESS_RETIRED"
    assert live.data == {
        "target": name_uri("squire"),
        "address": squire.uri,
        "agentId": squire.agent_id,
    }
    (delivery,) = send(flipped, squire.uri)["deliveries"]
    assert delivery["accepted"] is True

    assert flipped.agents.destroy("squire")
    gone = _refusal(flipped, name_uri("squire"))
    assert gone.code == "TARGET_UNRESOLVED"
    assert gone.data == {"target": name_uri("squire")}


def test_presence_claimed_unregistered_name_delivers(flipped) -> None:
    claimed = name_uri("streamer")
    flipped._presence = SimpleNamespace(  # type: ignore[assignment]
        actor_online=lambda actor: actor == claimed,
        online_actors=lambda: (claimed,),
        online_mailboxes=lambda: (),
    )

    (delivery,) = send(flipped, claimed)["deliveries"]

    assert delivery["target"] == claimed
    assert delivery["accepted"] is True


def test_interactive_session_without_a_record_delivers(flipped, monkeypatch) -> None:
    session_actor = name_uri("console")
    sessions = flipped._agent_session_domains.session
    monkeypatch.setattr(
        type(sessions),
        "read_sessions",
        lambda _self: (SimpleNamespace(actor=session_actor),),
    )

    (delivery,) = send(flipped, session_actor)["deliveries"]

    assert delivery["target"] == session_actor
    assert delivery["accepted"] is True


def test_routine_and_pac_escalation_refuse_a_retired_name(flipped) -> None:
    import yaml

    from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text

    worker = flipped.agents.require("worker")
    workflow = load_workflow_text(
        yaml.safe_dump(
            {
                "version": 2,
                "name": "retired-escalation",
                "escalate_to": name_uri("worker"),
                "nodes": [{"id": "work", "owner": worker.uri, "task": "work"}],
            }
        )
    )
    with pytest.raises(DaemonRequestError) as pac:
        flipped._workflow_admit(workflow, "user:op")  # type: ignore[attr-defined]
    assert pac.value.code == "AGENT_ADDRESS_RETIRED"
    assert pac.value.data["address"] == worker.uri

    with pytest.raises(DaemonRequestError) as routine:
        flipped._validate_input_alias(  # type: ignore[attr-defined]
            name_uri("worker"), surface=_routine_surface()
        )
    assert routine.value.code == "AGENT_ADDRESS_RETIRED"


def _routine_surface():
    from hyprial.daemon.impl.alias_resolver import AliasSurface

    return AliasSurface.ROUTINE


def test_reply_to_a_retired_sender_fails_loud_and_keeps_the_request(
    flipped,
) -> None:
    from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

    driver = flipped.agents.require("driver")
    request = InboxMessage(
        message_id="pre-flip-request",
        conversation_id="c-retired",
        sender=name_uri("worker"),
        recipient=driver.uri,
        payload=b'{"message":"please answer"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert flipped._inbox.receive(request).accepted

    with pytest.raises(DaemonRequestError) as failure:
        flipped.handle(
            "message.reply",
            {
                "from": "driver",
                "messageId": "pre-flip-request",
                "message": "answer",
                "summary": "answer",
            },
        )

    worker = flipped.agents.require("worker")
    assert failure.value.code == "AGENT_ADDRESS_RETIRED"
    assert failure.value.data["address"] == worker.uri
    pending = flipped._inbox.pending_messages(driver.uri)
    assert [item.message_id for item in pending] == ["pre-flip-request"]


def test_switch_off_keeps_the_legacy_queue_and_name_delivery(app) -> None:
    worker = app.agents.require("worker")
    assert worker.uri == name_uri("worker")

    (named,) = send(app, name_uri("worker"))["deliveries"]
    assert named["accepted"] is True

    (unknown,) = send(app, name_uri("ghost"))["deliveries"]
    assert unknown["queued"] is True
    assert unknown["code"] == "TARGET_UNRESOLVED"


GUEST = "guest"


def _guest_uri(segment: str) -> str:
    return canonical_agent_uri(GUEST, NODE, segment)


def _hosted(flipped: DaemonApplication):
    return flipped.agents.create_transfer_hosted("visitor", pinned_owner=GUEST)


def _boundary_refusal(app: DaemonApplication, target: str) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as failure:
        app._refuse_undeliverable_local_target(target)  # type: ignore[attr-defined]
    return failure.value


def test_hosted_owner_unclaimed_name_is_unresolved(flipped) -> None:
    _hosted(flipped)

    error = _boundary_refusal(flipped, _guest_uri("ghost"))

    assert error.code == "TARGET_UNRESOLVED"
    assert error.data == {"target": _guest_uri("ghost")}
    assert flipped._local_recipient_claimed(_guest_uri("ghost")) is False


def test_hosted_owner_unknown_id_is_unresolved(flipped) -> None:
    _hosted(flipped)

    error = _boundary_refusal(flipped, _guest_uri(UNKNOWN_ID))

    assert error.code == "TARGET_UNRESOLVED"
    assert flipped._local_recipient_claimed(_guest_uri(UNKNOWN_ID)) is False


def test_hosted_owner_name_is_retired_and_its_id_is_claimed(flipped) -> None:
    visitor = _hosted(flipped)
    assert visitor.uri == _guest_uri(visitor.agent_id)

    error = _boundary_refusal(flipped, _guest_uri("visitor"))

    assert error.code == "AGENT_ADDRESS_RETIRED"
    assert error.data == {
        "target": _guest_uri("visitor"),
        "address": visitor.uri,
        "agentId": visitor.agent_id,
    }
    flipped._refuse_undeliverable_local_target(visitor.uri)  # type: ignore[attr-defined]
    assert flipped._local_recipient_claimed(visitor.uri) is True
    assert flipped._local_recipient_claimed(_guest_uri("visitor")) is False


def test_own_owner_name_of_a_hosted_actor_is_not_claimed(flipped) -> None:
    _hosted(flipped)

    error = _boundary_refusal(flipped, name_uri("visitor"))

    assert error.code == "TARGET_UNRESOLVED"
    assert flipped._local_recipient_claimed(name_uri("visitor")) is False


def test_a_destroyed_agents_name_is_an_ordinary_unregistered_name(
    flipped,
) -> None:
    """No tombstone (2026-11-19): once its agent is destroyed a name is
    claimed by whatever unregistered claimant carries it, like any name."""

    flipped.handle("agent.create", {"name": "temp"})
    assert flipped.agents.destroy("temp")
    claimed = name_uri("temp")
    flipped._presence = SimpleNamespace(  # type: ignore[assignment]
        actor_online=lambda actor: actor == claimed,
        online_actors=lambda: (claimed,),
        online_mailboxes=lambda: (),
    )

    (delivery,) = send(flipped, claimed)["deliveries"]

    assert delivery["accepted"] is True
    assert flipped._local_recipient_claimed(claimed) is True


def test_a_live_agent_does_not_claim_its_actor_name_uri(flipped) -> None:
    """H2: no name-based read alias or delivery claim is left."""

    worker = flipped.agents.require("worker")
    flipped.agents.rename("worker", "builder")

    assert flipped._local_recipient_claimed(name_uri("worker")) is False
    assert flipped._local_recipient_claimed(name_uri("builder")) is False
    assert flipped._local_recipient_claimed(worker.uri) is True
    assert flipped._consumer_read_keys(worker.uri) == (worker.uri,)


def test_destroyed_hosted_owner_dead_id_is_forwarded_not_refused_locally(
    flipped,
) -> None:
    """Accepted 2026-11-19: once a hosted owner's last agent is destroyed,
    nothing on this node records that owner, so its addresses are foreign
    again: the dead ``a.<id>`` is neither refused nor claimed here and goes
    to the forwarding path, where it fails to resolve elsewhere."""

    visitor = _hosted(flipped)
    assert flipped.agents.destroy(visitor.agent_id)
    assert not any(agent.owner == GUEST for agent in flipped.agents.list())

    for target in (visitor.uri, _guest_uri("visitor"), _guest_uri("ghost")):
        assert flipped._local_address(target) is None  # type: ignore[attr-defined]
        flipped._refuse_undeliverable_local_target(target)  # type: ignore[attr-defined]
        assert flipped._local_recipient_claimed(target) is True


def test_an_owner_nothing_on_this_node_records_stays_foreign(flipped) -> None:
    stranger = canonical_agent_uri("stranger", NODE, "ghost")

    flipped._refuse_undeliverable_local_target(stranger)  # type: ignore[attr-defined]
    assert flipped._local_recipient_claimed(stranger) is True


def test_hosted_owner_session_claims_only_by_its_full_uri(
    flipped, monkeypatch
) -> None:
    _hosted(flipped)
    sessions = flipped._agent_session_domains.session
    monkeypatch.setattr(
        type(sessions),
        "read_sessions",
        lambda _self: (
            SimpleNamespace(actor="console"),
            SimpleNamespace(actor=_guest_uri("desk")),
        ),
    )

    flipped._refuse_undeliverable_local_target(_guest_uri("desk"))  # type: ignore[attr-defined]
    assert flipped._local_recipient_claimed(_guest_uri("desk")) is True
    with pytest.raises(DaemonRequestError) as raised:
        flipped._refuse_undeliverable_local_target(_guest_uri("console"))  # type: ignore[attr-defined]
    assert raised.value.code == "TARGET_UNRESOLVED"
    assert flipped._local_recipient_claimed(_guest_uri("console")) is False


def _send_as(app: DaemonApplication, caller: dict[str, str]) -> dict:
    worker = app.agents.require("worker")
    return app.handle(
        "message.send",
        {**caller, "to": [worker.uri], "summary": "hi", "message": "hi"},
    )


def _sender_refusal(app: DaemonApplication, caller: dict[str, str]) -> DaemonRequestError:
    outbox_before = app._inbox.outbox_count()
    with pytest.raises(DaemonRequestError) as failure:
        _send_as(app, caller)
    assert app._inbox.outbox_count() == outbox_before
    return failure.value


def test_name_uri_sender_of_a_registered_agent_is_retired(flipped) -> None:
    """Integrator decision D8: not the misleading SENDER_UNRESOLVED."""

    driver = flipped.agents.require("driver")
    assert driver.uri == canonical_agent_uri(OWNER, NODE, driver.agent_id)

    error = _sender_refusal(flipped, {"from": name_uri("driver")})

    assert error.code == "AGENT_ADDRESS_RETIRED"
    assert error.data == {
        "target": name_uri("driver"),
        "address": driver.uri,
        "agentId": driver.agent_id,
    }
    assert driver.uri in str(error)


def test_destroyed_agent_name_sender_is_unresolved(flipped) -> None:
    flipped.handle("agent.create", {"name": "temp"})
    assert flipped.agents.destroy("temp")

    error = _sender_refusal(flipped, {"from": name_uri("temp")})

    assert error.code == "SENDER_UNRESOLVED"


def test_on_behalf_of_a_name_uri_is_retired_not_rewritten(flipped) -> None:
    driver = flipped.agents.require("driver")

    error = _sender_refusal(
        flipped, {"from": "user:op", "onBehalfOf": name_uri("driver")}
    )

    assert error.code == "AGENT_ADDRESS_RETIRED"
    assert error.data == {
        "target": name_uri("driver"),
        "address": driver.uri,
        "agentId": driver.agent_id,
    }


def test_id_and_bare_name_senders_still_send(flipped) -> None:
    driver = flipped.agents.require("driver")

    for caller in (
        {"from": driver.uri},
        {"from": "driver"},
        {"from": "user:op", "onBehalfOf": driver.uri},
    ):
        (delivery,) = _send_as(flipped, caller)["deliveries"]
        assert delivery["accepted"] is True


def test_switch_off_name_uri_senders_keep_sending(app) -> None:
    assert app.agents.require("driver").uri == name_uri("driver")

    for caller in (
        {"from": name_uri("driver")},
        {"from": "user:op", "onBehalfOf": name_uri("driver")},
    ):
        (delivery,) = _send_as(app, caller)["deliveries"]
        assert delivery["accepted"] is True