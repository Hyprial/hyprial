"""S2 envelope: the kernel class vocabulary and the single class authority.

The rules may read only signals the local daemon minted itself.  A caller
chooses ``conversation_id`` and the recipient name freely, so neither is ever
allowed to make a message SYSTEM.  ``message.send`` also accepts a
caller-chosen ``system:hyprial:<node>`` sender, so that prefix counts only
together with ``intent == "system"``, which no request method can produce.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hyprial.daemon.impl.dispatch.identity import dispatch_message_id
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.envelope import (
    derive_message_class,
    inherit_envelope,
    message_class_of,
)
from hyprial.kernel import (
    DISPATCH_MESSAGE_PREFIX,
    MessageClass,
    mint_cell_id,
    mint_thread_id,
)

THREAD = mint_thread_id(b"envelope-rules-thread")
CELL = mint_cell_id(b"envelope-rules-cell")


def _message(**overrides: object) -> InboxMessage:
    fields: dict[str, object] = {
        "message_id": "m-1",
        "conversation_id": "c-1",
        "sender": "agent:op:node-1:driver",
        "recipient": "agent:op:node-1:reader",
        "payload": b'{"message":"hi"}',
        "intent": "request",
        "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
        "created_at_ms": 1,
    }
    fields.update(overrides)
    return InboxMessage(**fields)  # type: ignore[arg-type]


def test_kernel_message_class_vocabulary() -> None:
    assert [item.value for item in MessageClass] == ["interaction", "system"]
    assert DISPATCH_MESSAGE_PREFIX == "workflow-"
    assert dispatch_message_id("effect-1").startswith(DISPATCH_MESSAGE_PREFIX)


def test_envelope_defaults_keep_the_old_semantics() -> None:
    message = _message()
    assert message.thread_id is None
    assert message.conversation_cell_id is None
    assert message.message_class is None
    assert message.thread_key is None
    assert message_class_of(message) is MessageClass.INTERACTION


def test_with_envelope_sets_only_the_given_fields() -> None:
    message = _message().with_envelope(thread_id=THREAD)
    assert message.thread_id == THREAD
    assert message.thread_key == THREAD
    assert message.conversation_cell_id is None
    stamped = message.with_envelope(
        conversation_cell_id=CELL, message_class=MessageClass.SYSTEM
    )
    assert stamped.thread_id == THREAD
    assert stamped.conversation_cell_id == CELL
    assert stamped.message_class is MessageClass.SYSTEM
    assert _message().with_envelope() == _message()


@pytest.mark.parametrize(
    "overrides",
    [
        {"thread_id": "conversation-1"},
        {"thread_id": CELL},
        {"conversation_cell_id": THREAD},
        {"message_class": "system"},
    ],
)
def test_with_envelope_refuses_malformed_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _message().with_envelope(**overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"sender": "system:hyprial:node-1", "intent": "system"},
        {"message_id": dispatch_message_id("pac-effect")},
        {"message_id": dispatch_message_id("routine-task"), "intent": "request"},
        {
            "intent": "reply",
            "idempotency_key": f"reply:{dispatch_message_id('pac-effect')}",
        },
    ],
    ids=["system-sender", "pac-dispatch-id", "routine-task-id", "pac-worker-reply"],
)
def test_sound_local_signals_derive_system(overrides: dict[str, object]) -> None:
    message = _message(**overrides)
    assert derive_message_class(message) is MessageClass.SYSTEM
    assert message_class_of(message) is MessageClass.SYSTEM


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_id": "workflow"},
        {"conversation_id": f"pac-wf-{uuid4().hex}"},
        {"recipient": "agent:op:node-1:dispatch-coordinator"},
        {"sender": "agent:op:node-1:system"},
        {"sender": "system:other:node-1"},
        {"sender": "system:hyprial:node-1", "intent": "request"},
        {"sender": "system:hyprial:node-1", "intent": "reply"},
        {"intent": "system"},
        {"idempotency_key": "reply:workflow-not-a-uuid"},
        {"idempotency_key": f"reply:{dispatch_message_id('x')}:extra"},
        {"idempotency_key": f"workflow-escalate:{dispatch_message_id('x')}"},
        {"message_id": "Workflow-1"},
    ],
)
def test_caller_chosen_signals_stay_interaction(overrides: dict[str, object]) -> None:
    assert derive_message_class(_message(**overrides)) is MessageClass.INTERACTION


def test_an_explicit_stamp_wins_over_derivation() -> None:
    system_sender = _message(sender="system:hyprial:node-1")
    assert (
        message_class_of(system_sender.with_envelope(message_class=MessageClass.INTERACTION))
        is MessageClass.INTERACTION
    )
    assert (
        message_class_of(_message().with_envelope(message_class=MessageClass.SYSTEM))
        is MessageClass.SYSTEM
    )


def test_a_reply_inherits_the_original_envelope() -> None:
    original = _message().with_envelope(
        thread_id=THREAD, conversation_cell_id=CELL, message_class=MessageClass.SYSTEM
    )
    reply = _message(message_id="r-1", intent="reply", idempotency_key="reply:m-1")
    inherited = inherit_envelope(reply, original)
    assert inherited.thread_id == THREAD
    assert inherited.conversation_cell_id == CELL
    assert inherited.message_class is MessageClass.SYSTEM
    assert inherited.payload == reply.payload


def test_a_reply_keeps_its_own_envelope_values() -> None:
    other_thread = mint_thread_id(b"other")
    original = _message().with_envelope(thread_id=THREAD, conversation_cell_id=CELL)
    reply = _message(message_id="r-1").with_envelope(
        thread_id=other_thread, message_class=MessageClass.INTERACTION
    )
    inherited = inherit_envelope(reply, original)
    assert inherited.thread_id == other_thread
    assert inherited.conversation_cell_id == CELL
    assert inherited.message_class is MessageClass.INTERACTION


def test_inheriting_from_a_legacy_original_changes_nothing() -> None:
    reply = _message(message_id="r-1", intent="reply")
    assert inherit_envelope(reply, _message()) == reply
    derived_system = _message(message_id=dispatch_message_id("pac-effect"))
    assert inherit_envelope(reply, derived_system).message_class is None
