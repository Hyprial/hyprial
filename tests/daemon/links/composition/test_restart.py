"""WP2-B3 L14: the composed inbox port over the real durable ledger.

Spec: docs/design/send-links-impl/wp2-composition.md §10.4.  The service is
rebuilt after an interruption (object dropped, ``close()`` never called) and
must recover the open outbound count from the inbox SQLite ledger.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.composition.links import FacadeLinkInboxPort
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.links import policy
from hyprial.daemon.impl.links.ports import Allow, Mint
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.contracts.api import LinkLedgerOpen
from hyprial.daemon.impl.inbox.contracts.ports import UpdateLinkLedgerCommand
from hyprial.daemon.impl.links.ports import TOP_FIELD_OUTBOUND_IN_FLIGHT
from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
    ActorRuntime,
    SupervisionPolicy,
)
from tests.support.links.app import stub_app
from tests.support.links.org import FakeClock, FakeOrgFs, establish, make_service

A = "agent:o:n1:a"
B = "agent:o:n1:b"
LOCAL = (A, B, "agent:o:n1:c")


class _Events:
    def publish(self, event: object) -> None:
        del event


def _runtime() -> ActorRuntime:
    return ActorRuntime(
        policies={
            STATE_AUTHORITY: SupervisionPolicy(
                max_restarts=3,
                restart_window=10.0,
                base_backoff=0.0,
                max_backoff=0.0,
                jitter_ratio=0.0,
            )
        },
        random_source=lambda: 0.5,
    )


@pytest.fixture
def custody(tmp_path: Path):
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        runtime=_runtime(),
        node_id="n1",
    )
    facade = DeliveryCustodyFacade(coordinator, database, timeout=2.0)
    try:
        yield facade
    finally:
        assert coordinator.drain(2.0).complete is not False


def _port(facade: DeliveryCustodyFacade) -> FacadeLinkInboxPort:
    return FacadeLinkInboxPort(facade, local_agents=lambda: LOCAL)


def _write_open_outbound(facade: DeliveryCustodyFacade, message_id: str) -> None:
    facade.update_link_ledger(
        UpdateLinkLedgerCommand(
            correlation_id=f"test:{message_id}",
            open_outbound=(
                LinkLedgerOpen(
                    actor=A,
                    message_id=message_id,
                    peer=B,
                    intent="request",
                    reply_to=None,
                    state="open",
                    opened_at_ms=100,
                    expires_at_ms=10**15,
                ),
            ),
        )
    )


def _service(facade: DeliveryCustodyFacade):
    harness = make_service(
        fs=FakeOrgFs(), inbox=_port(facade), local_names=("a", "b", "c")
    )
    harness.service.start()
    return harness.service


def _interrupt(service) -> None:
    """Drop the service without ``close()`` (thread stops, store stays)."""

    service._stop.set()
    service._wake.set()
    thread = service._thread
    if thread is not None:
        thread.join(2.0)


def test_durable_outbound_row_survives_a_service_restart(custody) -> None:
    _write_open_outbound(custody, "m-open")

    first = _service(custody)
    try:
        assert first.top_fields(A)[TOP_FIELD_OUTBOUND_IN_FLIGHT] == 1
    finally:
        _interrupt(first)

    second = _service(custody)
    try:
        assert second.top_fields(A)[TOP_FIELD_OUTBOUND_IN_FLIGHT] == 1
    finally:
        second.close(2.0)


def test_ledger_snapshot_reads_real_rows_through_the_port(custody) -> None:
    _write_open_outbound(custody, "m-open")
    rows = _port(custody).ledger_snapshot(10**14)
    assert [row.message_id for row in rows] == ["m-open"]
    assert rows[0].actor == A and rows[0].peer == B and rows[0].state == "open"
    assert rows[0].sent_at_ms is None


def test_stub_app_can_be_composed_over_the_real_port(custody, tmp_path: Path) -> None:
    from hyprial.daemon.impl.composition.links import compose_link_service

    app = stub_app(tmp_path)
    service = compose_link_service(app, None, inbox_port=_port(custody))
    assert service is not None
    try:
        assert service.top_fields(A)[TOP_FIELD_OUTBOUND_IN_FLIGHT] == 0
    finally:
        service.close(2.0)
    assert service._thread is None or not service._thread.is_alive()


# -- L14: four interruption points over the real durable ledger -------------
#
# Spec: wp2-composition.md §10.4 / wp2.md §4.6 L14.  Every state below is
# produced through the engine (admit_send / submitted / admit_dispatch /
# tick / drop) and the real inbox facade; the service is dropped without
# ``close()`` and a brand-new coordinator, admission slot and service are
# built over the same SQLite file.

TTL_MS = 600_000


class _Crash(BaseException):
    """Raised by a patched seam to model the process dying right there."""


def _msg(
    message_id: str, sender: str, recipient: str, *, now: int
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=sender,
        recipient=recipient,
        payload=json.dumps({"text": "hi"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=now,
        expires_at_ms=now + TTL_MS,
        idempotency_key=f"idem-{message_id}",
    )


@dataclass
class _Node:
    """One daemon incarnation: coordinator + slot + facade + link service."""

    coordinator: DeliveryCustodyCoordinator
    facade: DeliveryCustodyFacade
    port: FacadeLinkInboxPort
    service: Any
    database: Path
    now: int

    def tick(self) -> None:
        self.service.tick(self.now)

    def close_authority(self) -> None:
        assert self.coordinator.drain(2.0).complete is not False


def _boot(database: Path, fs: FakeOrgFs, now: int) -> _Node:
    slot = LinkAdmission()
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        runtime=_runtime(),
        node_id="n1",
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, database, timeout=2.0)
    port = FacadeLinkInboxPort(facade, local_agents=lambda: LOCAL)
    harness = make_service(
        fs=fs, inbox=port, clock=FakeClock(now), local_names=("a", "b", "c")
    )
    slot.bind(harness.service)
    return _Node(coordinator, facade, port, harness.service, database, now)


def _send(node: _Node, message_id: str, *, observe: bool) -> None:
    """The send boundary: admit, durable submit with the ledger row, observe."""

    now = node.now
    decision = node.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    )
    hold = None
    if isinstance(decision, Mint):
        hold = ipc_errors.LINK_MINTING
    else:
        assert isinstance(decision, Allow)
    node.facade.submit(
        _msg(message_id, A, B, now=now),
        now_ms=now,
        link_hold=hold,
        link_open=LinkLedgerOpen(
            actor=A,
            message_id=message_id,
            peer=B,
            intent="request",
            reply_to=None,
            state="held" if hold else "open",
            opened_at_ms=now,
            expires_at_ms=now + TTL_MS,
        ),
    )
    if observe:
        node.service.submitted(
            sender=A,
            target=B,
            message_id=message_id,
            reply_to=None,
            hold_reason=hold,
            now_ms=now,
        )


def _inject_inbound(node: _Node, message_id: str) -> None:
    """B's request reaches A: admit at dispatch, then the carrier pulls it."""

    now = node.now
    node.facade.receive(_msg(message_id, B, A, now=now), now_ms=now)
    assert node.service.admit_dispatch(
        recipient=A, sender=B, message_id=message_id, now_ms=now
    )
    pulled = node.facade.fetch_pending(A, now_ms=now, only=frozenset({message_id}))
    assert [m.message_id for m in pulled] == [message_id]


def _unclosed(node: _Node) -> int:
    with node.service._mem:
        return policy.unclosed(node.service._view(A, node.now))


def _inbox_consumed(database: Path, message_id: str) -> bool:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT consumed FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row is None or bool(row[0])


def _durable_inbound(
    database: Path, message_id: str
) -> tuple[bool, int | None, str | None]:
    """The durable ``(consumed, acknowledged_at_ms, ledger state)`` of a row."""

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """SELECT inbox.consumed, inbox.acknowledged_at_ms,
                      link_ledger.state
                 FROM inbox
                 LEFT JOIN link_ledger
                   ON link_ledger.actor = inbox.recipient
                  AND link_ledger.message_id = inbox.message_id
                  AND link_ledger.direction = 'in'
                WHERE inbox.message_id = ?""",
            (message_id,),
        ).fetchone()
    if row is None:
        return False, None, None
    return bool(row[0]), row[1], row[2]


def _arrange_submit_before_observer(node: _Node, fs: FakeOrgFs) -> None:
    establish(fs, A, B, now_ms=node.now)
    node.tick()
    _send(node, "out1", observe=False)  # crash before ``submitted``


def _check_submit_before_observer(node: _Node, fs: FakeOrgFs) -> None:
    node.tick()
    assert node.service.top_fields(A)[TOP_FIELD_OUTBOUND_IN_FLIGHT] == 1
    assert _unclosed(node) == 1


def _arrange_admitted_before_tick(node: _Node, fs: FakeOrgFs) -> None:
    establish(fs, A, B, now_ms=node.now)
    node.tick()
    _send(node, "out1", observe=True)
    _inject_inbound(node, "in1")  # crash before the next tick


def _check_admitted_before_tick(node: _Node, fs: FakeOrgFs) -> None:
    node.tick()
    assert node.service.top_fields(A)[TOP_FIELD_OUTBOUND_IN_FLIGHT] == 1
    held = _unclosed(node)
    assert held == 2  # one outbound + one admitted inbound
    for _ in range(2):  # re-asking never takes a second slot
        assert node.service.admit_dispatch(
            recipient=A, sender=B, message_id="in1", now_ms=node.now
        )
    assert _unclosed(node) == held


def _arrange_hold_cleared_before_merge(node: _Node, fs: FakeOrgFs) -> None:
    node.tick()
    _send(node, "out1", observe=True)  # no link yet: minted and held
    establish(fs, A, B, now_ms=node.now)  # the peer references back

    def die(*args: Any, **kwargs: Any) -> None:
        raise _Crash

    node.service._merge_apply = die  # the release commits, the merge never runs
    with pytest.raises(_Crash):
        node.tick()
    assert node.port.held_outbox() == ()  # the hold really was cleared


def _check_hold_cleared_before_merge(node: _Node, fs: FakeOrgFs) -> None:
    node.tick()
    assert node.port.held_outbox() == ()
    assert node.service.top_fields(A)[TOP_FIELD_OUTBOUND_IN_FLIGHT] == 1
    assert _unclosed(node) == 1  # the released row is not counted as held too


def _arrange_drain_acked_before_merge(node: _Node, fs: FakeOrgFs) -> None:
    establish(fs, A, B, now_ms=node.now)
    node.tick()
    _inject_inbound(node, "in1")

    def die(*args: Any, **kwargs: Any) -> None:
        raise _Crash

    node.service._emit = die  # the drain + ack commit; the engine then dies
    with pytest.raises(_Crash):
        node.service.drop(actor=A, peer=B, now_ms=node.now)
        node.tick()  # the merged engine queues drops (ruling P-3); the tick drains them
    assert _inbox_consumed(node.database, "in1")  # ack really landed


def _check_drain_acked_before_merge(node: _Node, fs: FakeOrgFs) -> None:
    node.tick()
    assert node.service.settle_hint(actor=A, message_id="in1") == "ack"
    assert _unclosed(node) == 0
    with pytest.raises(DaemonRequestError) as caught:
        node.service.admit_send(
            sender=A, target=B, intent="reply", reply_to="in1", now_ms=node.now
        )
    assert caught.value.code == ipc_errors.LINK_RECLAIMED


_POINTS = {
    "submit-before-observer": (
        _arrange_submit_before_observer,
        _check_submit_before_observer,
    ),
    "admitted-before-tick": (
        _arrange_admitted_before_tick,
        _check_admitted_before_tick,
    ),
    "hold-cleared-before-merge": (
        _arrange_hold_cleared_before_merge,
        _check_hold_cleared_before_merge,
    ),
    "drain-acked-before-merge": (
        _arrange_drain_acked_before_merge,
        _check_drain_acked_before_merge,
    ),
}


@pytest.mark.parametrize("point", sorted(_POINTS))
def test_l14_state_survives_an_interruption_and_rebuild(
    tmp_path: Path, point: str
) -> None:
    arrange, check = _POINTS[point]
    database = tmp_path / "inbox.sqlite3"
    fs = FakeOrgFs()
    fs.add_space("s1")
    now = time.time_ns() // 1_000_000
    first = _boot(database, fs, now)
    try:
        arrange(first, fs)
    finally:
        first.close_authority()  # the service itself is dropped, never closed

    second = _boot(database, fs, now)
    try:
        # ``start()`` seeds the engine from the durable facts (``_rebuild``);
        # stop its thread so the checks drive ``tick`` deterministically.
        second.service.start()
        _interrupt(second.service)
        check(second, fs)
    finally:
        second.close_authority()


def test_r22_reclaimed_uninjected_request_drains_durably(tmp_path: Path) -> None:
    """R22 / E26-08: reclaim drains an inbox request the ledger never saw.

    B's request lands in A's inbox but the carrier never pulls it, so no
    ledger row exists for it.  Dropping the link must still book the durable
    terminal (inbox ``consumed``/``acknowledged_at_ms`` and a ``drained``
    ledger row) so a restart cannot re-inject the request.
    """

    database = tmp_path / "inbox.sqlite3"
    fs = FakeOrgFs()
    fs.add_space("s1")
    now = time.time_ns() // 1_000_000
    first = _boot(database, fs, now)
    try:
        establish(fs, A, B, now_ms=now)
        first.tick()
        first.facade.receive(_msg("in1", B, A, now=now), now_ms=now)
        first.service.drop(actor=A, peer=B, now_ms=now)
        first.tick()
        consumed, acked_at_ms, ledger_state = _durable_inbound(database, "in1")
        assert consumed is True
        assert acked_at_ms is not None
        assert ledger_state == "drained"
    finally:
        first.close_authority()

    # A brand-new incarnation over the same SQLite file must still see the
    # request as terminal: drained, never re-injected.
    second = _boot(database, fs, now)
    try:
        second.service.start()
        _interrupt(second.service)
        consumed, acked_at_ms, ledger_state = _durable_inbound(database, "in1")
        assert consumed is True and acked_at_ms is not None
        assert ledger_state == "drained"
        assert second.service.settle_hint(actor=A, message_id="in1") == "ack"
        with pytest.raises(DaemonRequestError) as caught:
            second.service.admit_send(
                sender=A, target=B, intent="reply", reply_to="in1", now_ms=now
            )
        assert caught.value.code == ipc_errors.LINK_RECLAIMED
    finally:
        second.close_authority()
