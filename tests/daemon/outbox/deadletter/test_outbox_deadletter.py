"""Outbox dead-letter defects C1/C2/C3 (Allen 2026-08-22 亲点).

C1: prune criteria — host-class rows are dead under the CURRENT send gate
    (TARGET_IS_NODE), and provably-nonexistent local targets prune while
    merely-offline ones never do.
C2: an offline recipient spends no retry attempts; its row is parked until an
    online event or the TTL deadline.
C3: the retry/delivery path has NO registration/presence side effects —
    a message to a nonexistent actor must never mint it into existence.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from hyprial.daemon.impl.application import _undeliverable_outbox_recipient
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.service.policy import OFFLINE_RECHECK_INTERVAL_MS

SENDER = "agent:o:node-a:runner"
DEAD_LOCAL = "agent:o:node-a:ghost"
LIVE_BUT_OFFLINE = "agent:o:node-a:sleeper"


def _message(message_id: str, recipient: str, created_at_ms: int = 0) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conv-dl",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


# ── C1: scheme predicate follows the CURRENT send gate ─────────────────────


def test_host_rows_are_dead_under_the_current_send_gate() -> None:
    """C1: TARGET_IS_NODE rejects node sends today, so host rows can never be
    consumed.  The pre-migration reason to keep them is gone."""
    assert _undeliverable_outbox_recipient("hq-adjutant")  # bare 2-seg node form
    assert _undeliverable_outbox_recipient("peer-node")
    assert not _undeliverable_outbox_recipient("agent:o:node-a:w1")
    assert not _undeliverable_outbox_recipient("user:allen")
    assert not _undeliverable_outbox_recipient("route:lark:main")


# ── C2: offline holds are event-driven ─────────────────────────────────────


class C2Tests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.transport = MemoryDeliveryTransport()
        self.service = InboxService(
            Path(self._tmp.name) / "inbox.sqlite3", self.transport
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_offline_recipient_reaches_dlq_only_at_ttl(self) -> None:
        self.service.submit(_message("m-off", DEAD_LOCAL), now_ms=0)
        held = self.service.outbox_item("m-off")
        assert held.attempts == 0
        # Bounded offline recheck (PR #1463 finding 1): the row is never
        # parked at its own deadline; it rechecks on the shared cadence and
        # still dies at the TTL if the recipient never appears.
        assert held.next_attempt_ms < held.expires_at_ms
        self.service.retry_due(now_ms=held.expires_at_ms - 1)
        assert self.service.outbox_count() == 1
        [expired] = self.service.retry_due(now_ms=held.expires_at_ms)
        assert expired.code == "TTL_EXPIRED"
        assert self.service.outbox_count() == 0
        assert self.service.dlq_count() == 1
        delivered_before = list(self.transport.delivered)
        custody_before = list(self.transport.custody_transfers)
        self.service.retry_due(now_ms=10**12)
        assert self.transport.delivered == delivered_before
        assert self.transport.custody_transfers == custody_before
        assert self.service.outbox_count() == 0

    def test_offline_pumps_leave_attempts_and_due_unchanged(self) -> None:
        self.service.submit(_message("m-bo", DEAD_LOCAL), now_ms=0)
        held = self.service.outbox_item("m-bo")
        self.service.retry_due(now_ms=1_000)
        unchanged = self.service.outbox_item("m-bo")
        assert unchanged.attempts == 0
        # The presence recheck is not a delivery attempt: the not-yet-due row
        # keeps the submit-time bounded recheck, never the deadline itself.
        assert (
            unchanged.next_attempt_ms
            == OFFLINE_RECHECK_INTERVAL_MS
            < held.expires_at_ms
        )


# ── C3: retry has no registration side effects ─────────────────────────────


class _NullRegistration:
    def close(self) -> None:
        pass


class SpyTransport(MemoryDeliveryTransport):
    """Records every liveness/registration call the retry path might make.

    The override is the point: without it ``assert declarations == []`` is
    vacuously true (e2e-verifier caught this — the original spy recorded into
    a list nothing could ever append to).
    """

    def __init__(self) -> None:
        super().__init__()
        self.liveliness_declarations: list[str] = []

    def declare_liveliness(self, key: str):
        self.liveliness_declarations.append(key)
        return _NullRegistration()


class C3Tests(unittest.TestCase):
    def test_retrying_a_dead_actor_mints_nothing(self) -> None:
        """C3: the retry-driven WHOLE path declares no liveness for a dead actor.

        Verification-point note (e2e-verifier review): what this test actually
        covers is `retry_due` AS A WHOLE, NOT 'no registration at delivery
        time' — a dead actor's message is NEVER delivered (`is_online` is
        False, so `_attempt_direct` is skipped outright).  The meaningful
        claim is therefore 'the retry-driven path (queue, defer, custody
        attempt, exhaust, DLQ) never calls declare_liveliness', and the spy
        must override declare_liveliness to be load-bearing (the original
        spy did not — a vacuous assertion caught at review).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            transport = SpyTransport()
            service = InboxService(Path(tmp) / "inbox.sqlite3", transport)
            # A message to an actor that does not exist anywhere.
            service.submit(_message("m-ghost", DEAD_LOCAL), now_ms=0)
            deadline = service.outbox_item("m-ghost").expires_at_ms
            now = 0
            while service.dlq_count() == 0:
                now = max(now + 1, service.outbox_item("m-ghost").next_attempt_ms)
                assert now <= deadline
                service.retry_due(now_ms=now)
            # The row reached TTL — and NOTHING about DEAD_LOCAL was
            # declared, registered, or advertised anywhere on the way.
            assert service.dlq_count() == 1
            assert transport.liveliness_declarations == []
            assert not transport.is_online(DEAD_LOCAL)
            service.close()


# ── C1: unresolvable predicate — provably dead vs merely offline ────────────


def _app_with(tmp_path: Path):
    from tests.daemon.outbox.deadletter.test_outbox_prune import application

    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app = application(tmp_path, service)
    return app, service


def test_unresolvable_prunes_local_nonexistent_keeps_offline_and_remote(
    tmp_path: Path,
) -> None:
    app, service = _app_with(tmp_path)
    try:
        owner = app.owner
        node = app.node_id
        dead = f"agent:{owner}:{node}:ghost"
        offline_registered = f"agent:{owner}:{node}:sleeper"
        remote = f"agent:{owner}:other-node:worker"
        # The sleeper exists in the registry (even though nothing runs it).
        app.agents.create("sleeper")

        assert app._unresolvable_outbox_recipient(dead) is True
        assert app._unresolvable_outbox_recipient(offline_registered) is False
        assert app._unresolvable_outbox_recipient(remote) is False
        assert app._unresolvable_outbox_recipient("not-a-uri") is False

        # …and a present target is never unresolvable.
        app._presence = type(
            "P", (), {"actor_online": staticmethod(lambda _a: True)}
        )()
        assert app._unresolvable_outbox_recipient(dead) is False
    finally:
        app._close()


def test_prune_moves_unresolvable_local_but_spares_offline_registered(
    tmp_path: Path,
) -> None:
    app, service = _app_with(tmp_path)
    try:
        owner = app.owner
        node = app.node_id
        dead = f"agent:{owner}:{node}:ghost"
        sleeper = f"agent:{owner}:{node}:sleeper"
        app.agents.create("sleeper")
        service.submit(_message("m-dead", dead), now_ms=0)
        service.submit(_message("m-sleeper", sleeper), now_ms=0)

        pruned = service.prune_outbox(
            undeliverable=lambda _r: False,
            unresolvable=app._unresolvable_outbox_recipient,
            now_ms=1,
        )

        assert [item.message_id for item in pruned] == ["m-dead"]
        assert pruned[0].reason == "TARGET_UNRESOLVABLE"
        remaining = {item.message.message_id for item in service.outbox_items()}
        assert remaining == {"m-sleeper"}
    finally:
        app._close()


if __name__ == "__main__":
    unittest.main()
