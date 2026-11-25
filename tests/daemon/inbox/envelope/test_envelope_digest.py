"""The submission digest never includes envelope fields (plan §1.1, S2).

A retried submit joins its settled receipt by digest.  Folding a local
envelope stamp into it would make a stamped retry of an unstamped command
conflict, and would rewrite every receipt settled before the upgrade.  PIN:
the digest is the historical one whether or not the envelope is set.  The
guard also runs against a seeded red case, a synthetic digest that folds in
``thread_id``, and must reject it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.actor.custody.submit import DeliveryCustodySubmitMixin
from hyprial.kernel import MessageClass, mint_cell_id, mint_thread_id

_PINNED_LEGACY_DIGEST = (
    "sha256:685df1039a1992087884254481d0432aa55aef40f48761d985e245c7f4536498"
)


def _reply(summary: str | None = None) -> InboxMessage:
    return InboxMessage(
        message_id="reply:m-1",
        conversation_id="c-1",
        sender="agent:op:node-1:reader",
        recipient="agent:op:node-1:driver",
        payload=b'{"message":"ok","origin":{"via":"session"}}',
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key="reply:m-1",
        created_at_ms=2,
        summary=summary,
    )


def _stamp(message: InboxMessage, message_class: MessageClass) -> InboxMessage:
    return message.with_envelope(
        thread_id=mint_thread_id(b"digest-thread"),
        conversation_cell_id=mint_cell_id(b"digest-cell"),
        message_class=message_class,
    )


Digest = Callable[[InboxMessage], str]


def _assert_digest_ignores_the_envelope(digest: Digest) -> None:
    assert digest(_reply()) == _PINNED_LEGACY_DIGEST
    for message_class in MessageClass:
        assert digest(_stamp(_reply(), message_class)) == _PINNED_LEGACY_DIGEST
        assert digest(_stamp(_reply("S1"), message_class)) == digest(_reply("S1"))


def _leaky_digest(message: InboxMessage) -> str:
    legacy = DeliveryCustodySubmitMixin._submission_command_digest(message)
    return legacy if message.thread_id is None else f"{legacy}:{message.thread_id}"


def test_the_digest_ignores_the_envelope() -> None:
    _assert_digest_ignores_the_envelope(
        DeliveryCustodySubmitMixin._submission_command_digest
    )


def test_the_digest_guard_rejects_a_digest_that_folds_in_thread_id() -> None:
    with pytest.raises(AssertionError):
        _assert_digest_ignores_the_envelope(_leaky_digest)
