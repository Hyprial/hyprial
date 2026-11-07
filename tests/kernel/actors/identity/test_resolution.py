"""actor_identity: resolution scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
from pathlib import Path
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.desired_state import InteractiveSession
from hyprial.kernel import canonical_agent_uri
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.daemon.impl.configuration.identity import write_settings_identity
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
)

from .support import (
    PI_WORKER,
    PI_WORKER_WITH_NICKNAME,
    _registered,
    _sent_recipient,
    application,
)


@pytest.fixture(
    params=(False, True),
    ids=("identity-unseeded", "identity-seeded"),
    autouse=True,
)
def _p1_identity_variant(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Run every legacy resolution scenario with and without P1 identity.

    The seeded leg is load-bearing: without it P2 classifies every canonical
    target outside-domain and the alias validator never exercises local-agent
    admission.
    """

    if not request.param:
        return
    write_settings_identity(
        "op",
        mode="casdoor",
        issuer="https://auth.hyprial.test",
        user_id="user-a",
        username="op",
        hyprial_home=tmp_path,
    )
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id="node-1",
            owner="op",
            server_public="nodekey:server",
            client_public="nodekey:client",
            device_uid="11111111111141118111111111111111",
            name="node-1",
        ),
    )


def test_node_rename_old_uri_stays_explicit_and_visible_in_ps(tmp_path: Path) -> None:
    app = application(tmp_path, providers=(PI_WORKER,))
    assert app._inbox is not None
    old_recipient = "agent:op:old-node:kanban-sim"
    current_recipient = "agent:op:node-1:kanban-sim"
    message = InboxMessage(
        message_id="before-node-rename",
        conversation_id="rename-conversation",
        sender="agent:op:node-9:sender",
        recipient=old_recipient,
        payload=b'{"message":"before rename"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert app._inbox.receive(message).acknowledged is True

    pending = app.handle("message.pending.list", {"from": "kanban-sim"})
    assert pending["messages"] == []
    current_ack = app.handle(
        "message.ack", {"from": "kanban-sim", "messageId": message.message_id}
    )
    assert current_ack == {
        "ok": False,
        "messageId": message.message_id,
        "acknowledged": False,
        "code": "MESSAGE_ACK_UNAVAILABLE",
    }
    assert app.handle("ps", {})["historicalInboxRecipients"] == [
        {
            "recipient": old_recipient,
            "currentRecipient": current_recipient,
            "pending": 1,
        }
    ]

    explicit_pending = app.handle("message.pending.list", {"from": old_recipient})
    assert [item["messageId"] for item in explicit_pending["messages"]] == [
        message.message_id
    ]
    explicit_ack = app.handle(
        "message.ack", {"from": old_recipient, "messageId": message.message_id}
    )
    assert explicit_ack["acknowledged"] is True
    assert app.handle("ps", {})["historicalInboxRecipients"] == []


def test_agent_resolve_reports_alias_node_scheme_and_unknown(
    tmp_path: Path,
) -> None:
    """The CLI's --from check reads this; it must mirror message.send."""

    app = application(tmp_path, providers=(PI_WORKER,))

    alias = app.handle("agent.resolve", {"name": "kanban-sim"})
    assert alias["known"] is True
    assert alias["reason"] == "alias"
    assert alias["resolved"] == "agent:op:node-1:kanban-sim"

    node = app.handle("agent.resolve", {"name": "node-1"})
    assert node["known"] is True
    assert node["reason"] == "node"

    scheme = app.handle("agent.resolve", {"name": "user:bob"})
    assert scheme["known"] is True
    assert scheme["reason"] == "scheme"
    assert scheme["resolved"] == "user:bob"

    unknown = app.handle("agent.resolve", {"name": "nobody-here"})
    assert unknown["known"] is False
    assert unknown["reason"] == "unknown"
    assert unknown["resolved"] == "nobody-here"


def test_agent_resolve_display_form_normalizes_before_resolving(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, providers=(PI_WORKER,))

    answer = app.handle("agent.resolve", {"name": "agent:kanban-sim"})

    assert answer["known"] is True
    assert answer["reason"] == "alias"
    assert answer["resolved"] == "agent:op:node-1:kanban-sim"


# ⭐ RETIRED: test_agent_resolve_ambiguity_is_a_loud_error
#
# Same supersession as ``test_ambiguous_short_name_fails_loudly_with_candidates``:
# it drove the collision by putting ``agent:op:node-9:kanban-sim`` into presence
# beside a managed harness of the same name.  ``agent.resolve`` no longer reads
# presence, so the collision cannot be constructed that way.
#
# The loud-error contract for ``agent.resolve`` is carried by
# ``test_pinned_owner_disagreement_stays_a_loud_error``.


def test_local_streaming_provider_short_name_resolves_to_minted_uri(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, providers=(PI_WORKER,))
    assert _sent_recipient(app, "kanban-sim") == "agent:op:node-1:kanban-sim"


def test_display_prefixed_provider_name_resolves_the_same_way(
    tmp_path: Path,
) -> None:
    """The targets copy-paste path: agent:<name> and the bare name are one
    recipient after alias resolution."""

    app = application(tmp_path, providers=(PI_WORKER,))
    assert _sent_recipient(app, "agent:kanban-sim") == "agent:op:node-1:kanban-sim"


def test_offline_local_provider_still_resolves_from_desired_state(
    tmp_path: Path,
) -> None:
    """A crashed worker keeps its canonical inbox key: sends during the
    outage queue under the URI the connector will drain on recovery, never
    under a short name nobody consumes."""

    app = application(tmp_path, actors=(), providers=(PI_WORKER,))
    assert _sent_recipient(app, "kanban-sim") == "agent:op:node-1:kanban-sim"


def test_bare_name_ignores_another_owner_domain(tmp_path: Path) -> None:
    """A foreign-owner actor must not claim a bare alias — and an
    unresolved bare name is a node address, rejected loudly at the gate."""

    app = application(tmp_path, actors=("agent:mate:peer-host:e2e-sim",))
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send", {"from": _registered(app, "driver"), "to": ["e2e-sim"], "message": "hi", "summary": "hi"}
        )
    assert failure.value.code == "TARGET_IS_NODE"
    assert app._inbox.outbox_count() == 0


def test_bare_name_ignores_foreign_owner_interactive_session(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    app.desired_state.register_interactive(
        InteractiveSession(
            actor="agent:mate:peer-host:e2e-sim",
            cwd="/work",
            command=("claude",),
            source="claude-channel",
            session_ref="foreign-session",
        )
    )
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send", {"from": _registered(app, "driver"), "to": ["e2e-sim"], "message": "hi", "summary": "hi"}
        )
    assert failure.value.code == "TARGET_IS_NODE"
    assert app._inbox.outbox_count() == 0


# ⭐ RETIRED: test_bare_name_resolves_to_unique_actor_in_local_owner_domain
#
# It pinned presence-based resolution across the owner domain: presence held
# ``agent:op:peer-host:e2e-sim`` and the test asserted a bare send resolved to
# that PEER's actor.  The name says it — "local owner domain", matched on owner
# and actor with the machine ignored.
#
# ⚠️ That assertion did not merely become obsolete; owner unification makes it
# describe the defect.  Once owner is a user identity shared by every machine a
# user owns, "unique actor in the owner domain" is no longer unique, and
# resolving a bare name to a peer is the wrong-target failure this step exists
# to remove.  Superseded by Allen's 2026-09-04 table-lookup ruling; the
# behaviour it should have asserted is now
# ``test_peer_actor_of_the_same_name_is_not_a_candidate``.


def test_local_and_presence_views_of_one_actor_stay_one_candidate(
    tmp_path: Path,
) -> None:
    app = application(
        tmp_path,
        actors=("agent:op:node-1:kanban-sim",),
        providers=(PI_WORKER,),
    )
    assert _sent_recipient(app, "kanban-sim") == "agent:op:node-1:kanban-sim"


# ⭐ RETIRED: test_ambiguous_short_name_fails_loudly_with_candidates
#
# It pinned the ambiguity that presence scanning produced: two same-owner
# actors on peer-a and peer-b became two candidates for one bare name.  The
# table lookup removes the source, so this scenario can no longer arise —
# neither peer is consulted at all.
#
# ⚠️ The *intent* (more than one candidate is a loud error, never a guess) is
# NOT retired.  It moved to ``test_pinned_owner_disagreement_stays_a_loud_error``,
# which drives the one disagreement that survives two exact lookups:
# ``spec.pinned_owner`` (decision D-D) makes a transferred harness mint the
# source owner while a registry row for the same name mints this daemon's.


def test_unknown_bare_name_is_a_node_address_and_is_rejected(
    tmp_path: Path,
) -> None:
    """An unresolved bare name IS a node address (the bare namespace is the
    node namespace): rejected at the gate, never queued behind ok:true."""

    app = application(tmp_path)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send", {"from": _registered(app, "driver"), "to": ["ghost"], "message": "hi", "summary": "hi"}
        )
    assert failure.value.code == "TARGET_IS_NODE"
    assert app._inbox.outbox_count() == 0


def test_full_uri_targets_pass_through_untouched(tmp_path: Path) -> None:
    app = application(tmp_path, providers=(PI_WORKER,))
    assert (
        _sent_recipient(app, "agent:mate:elsewhere:kanban-sim")
        == "agent:mate:elsewhere:kanban-sim"
    )


def test_streaming_provider_nickname_resolves_to_the_name_minted_uri(
    tmp_path: Path,
) -> None:
    """The operator-facing addressing path: a connector started with
    ``--nickname kanban`` registers under the URI minted from its managed
    ``name`` (kanban-sim), so addressing the nickname must resolve to that
    same URI — not to ``agent:op:node-1:kanban``, a key nobody drains."""

    app = application(tmp_path, providers=(PI_WORKER_WITH_NICKNAME,))
    assert _sent_recipient(app, "kanban") == "agent:op:node-1:kanban-sim"


def test_nickname_and_managed_name_are_one_recipient(tmp_path: Path) -> None:
    """Whichever alias the caller types — the managed name or the nickname —
    the message lands on the one canonical recipient the connector drains."""

    app = application(tmp_path, providers=(PI_WORKER_WITH_NICKNAME,))
    assert _sent_recipient(app, "kanban-sim") == "agent:op:node-1:kanban-sim"
    assert _sent_recipient(app, "kanban") == "agent:op:node-1:kanban-sim"


def test_ambiguous_nickname_colliding_with_another_name_fails_loudly(
    tmp_path: Path,
) -> None:
    """A nickname that also spells another managed provider's name resolves
    to two distinct minted URIs; guessing is forbidden."""

    other = {
        "provider": "claude",
        "name": "kanban",
        "headless": True,
        "args": [],
        "ownership": "managed",
    }
    app = application(tmp_path, providers=(PI_WORKER_WITH_NICKNAME, other))
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {"from": _registered(app, "driver"), "to": ["kanban"], "message": "hi", "summary": "hi"},
        )
    assert failure.value.code == "AMBIGUOUS_TARGET"
    assert failure.value.data["candidates"] == [
        "agent:op:node-1:kanban",
        "agent:op:node-1:kanban-sim",
    ]


def test_unknown_nickname_is_rejected_not_queued(tmp_path: Path) -> None:
    """An unrecognized nickname resolves to no agent; the unresolved bare
    name is a node address and the gate rejects it instead of letting it
    expire silently in the durable queue."""

    app = application(tmp_path, providers=(PI_WORKER_WITH_NICKNAME,))
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send", {"from": _registered(app, "driver"), "to": ["adjutant"], "message": "hi", "summary": "hi"}
        )
    assert failure.value.code == "TARGET_IS_NODE"
    assert app._inbox.outbox_count() == 0


# ── Short-name resolution is a table lookup (Allen, 2026-09-04) ──────────────
#
# Owner is a *user* identity now: one value across all of a user's machines.
# The old resolver matched candidates on ``owner + actor`` and ignored
# ``machine``, which was safe only because owner used to come from the host
# login and therefore differed per node.  These tests pin the replacement.


def test_short_name_resolves_from_the_local_registry_row(tmp_path: Path) -> None:
    """A registered short name resolves to that row's canonical URI."""

    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    resolved = app._resolve_agent_alias("foo")  # type: ignore[attr-defined]
    assert resolved == canonical_agent_uri("op", "node-1", "foo")


def test_peer_actor_of_the_same_name_is_not_a_candidate(tmp_path: Path) -> None:
    """⭐ The defect owner-unification would have activated.

    A peer running the same actor name shares the owner segment once owner is
    a user identity.  The old resolver scanned ``presence.online_actors()``
    matching owner + actor only, so this raised AMBIGUOUS_TARGET and every
    bare send to a name the peer also used — ``hq-adjutant`` among them —
    stopped working, but only while the peer happened to be online.

    ⚠️ This is the mutation guard: put presence back into the candidate set
    and this test goes red.  It is deliberately written as "resolves to the
    local URI", not "does not raise", so a resolver that silently picked the
    remote row would fail it too.
    """

    peer = canonical_agent_uri("op", "node-2", "foo")
    app = application(tmp_path, actors=(peer,))
    app.handle("agent.create", {"name": "foo"})

    resolved = app._resolve_agent_alias("foo")  # type: ignore[attr-defined]
    assert resolved == canonical_agent_uri("op", "node-1", "foo")
    assert resolved != peer


def test_unknown_short_name_is_returned_unchanged(tmp_path: Path) -> None:
    """Not-found keeps the bare name — it must not raise.

    ⛔ Deliberately NOT "unknown short name is an error".  New bare sends are
    already refused one layer up at the admission gate (TARGET_IS_NODE), while
    ``_destroy_agent_messages`` drains both spellings *because* an unresolvable
    bare name is left untouched here.  Raising would break that drain and the
    legacy consumption path without adding a refusal that does not exist.
    """

    app = application(tmp_path)
    assert app._resolve_agent_alias("nobody-here") == "nobody-here"  # type: ignore[attr-defined]


def test_pinned_owner_disagreement_stays_a_loud_error(tmp_path: Path) -> None:
    """Two exact lookups can still disagree — and that must not be guessed.

    ``_canonical_harness_uri`` honours ``spec.pinned_owner`` (decision D-D) so
    a transferred worker keeps its SOURCE owner, while a registry row for the
    same short name carries this daemon's.  AMBIGUOUS_TARGET is therefore NOT
    dead code after the scans were removed, which is why the guard stays.
    """

    transferred = dict(PI_WORKER)
    transferred["name"] = "moved"
    transferred["pinnedOwner"] = "someone-else"
    app = application(tmp_path, providers=(transferred,))
    app.handle("agent.create", {"name": "moved"})

    with pytest.raises(DaemonRequestError) as ambiguous:
        app._resolve_agent_alias("moved")  # type: ignore[attr-defined]
    assert ambiguous.value.code == "AMBIGUOUS_TARGET"
