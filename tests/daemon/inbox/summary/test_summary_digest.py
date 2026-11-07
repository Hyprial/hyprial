"""T17: the reply digest stays stable and becomes summary-sensitive.

A retried reply joins its own receipt only when the whole command -- summary
included -- is byte-identical.  ``summary=None`` rows written before this
change must keep the exact digest they had, or their receipts would stop
matching after an upgrade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.actor.custody.submit import DeliveryCustodySubmitMixin
from hyprial.daemon.impl.inbox.actor.internal import _SubmissionReceiptConflict
from hyprial.daemon.impl.inbox.contracts.ports import SubmitMessageCommand

# The pre-summary digest of ``_reply(None)``; pinned so a storage change that
# silently rewrites already-settled receipts fails loudly.
_PINNED_LEGACY_DIGEST = (
    "sha256:685df1039a1992087884254481d0432aa55aef40f48761d985e245c7f4536498"
)


def _reply(summary: str | None) -> InboxMessage:
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


class _Probe(DeliveryCustodySubmitMixin):
    def __init__(self, service: InboxService) -> None:
        self._service = service


def test_a_summary_less_command_keeps_its_historical_digest() -> None:
    assert (
        DeliveryCustodySubmitMixin._submission_command_digest(_reply(None))
        == _PINNED_LEGACY_DIGEST
    )


def test_the_digest_becomes_summary_sensitive() -> None:
    first = DeliveryCustodySubmitMixin._submission_command_digest(_reply("S1"))
    second = DeliveryCustodySubmitMixin._submission_command_digest(_reply("S2"))
    assert first != _PINNED_LEGACY_DIGEST
    assert first != second
    assert first == DeliveryCustodySubmitMixin._submission_command_digest(_reply("S1"))


def _store_receipt(service: InboxService, command: SubmitMessageCommand) -> None:
    service._db.execute(
        """INSERT INTO actor_submission_receipts (
               correlation_id, command_digest, message_id, accepted, queued,
               recorded_at_ms)
           VALUES (?, ?, ?, 1, 0, 1)""",
        (
            command.correlation_id,
            DeliveryCustodySubmitMixin._submission_command_digest(command.message),
            command.message.message_id,
        ),
    )
    service._db.commit()


def test_the_same_correlation_with_a_different_summary_conflicts(
    tmp_path: Path,
) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        probe = _Probe(service)
        correlation = "reply-correlation-1"
        _store_receipt(service, SubmitMessageCommand(correlation, _reply(None)))

        joined = probe._read_submission_receipt(
            SubmitMessageCommand(correlation, _reply(None))
        )
        assert joined is not None
        assert joined.result.message_id == "reply:m-1"

        with pytest.raises(_SubmissionReceiptConflict):
            probe._read_submission_receipt(
                SubmitMessageCommand(correlation, _reply("S1"))
            )
        # The stored receipt is untouched by the refused retry.
        row = service._db.execute(
            "SELECT command_digest FROM actor_submission_receipts WHERE correlation_id = ?",
            (correlation,),
        ).fetchone()
        assert row is not None
        assert row["command_digest"] == _PINNED_LEGACY_DIGEST
    finally:
        service.close()
