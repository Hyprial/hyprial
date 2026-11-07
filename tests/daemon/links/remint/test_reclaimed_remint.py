"""A reclaimed link must re-mint on a fresh local reference (2026-10-18).

Production (daemon epoch 5eb369b2): after the k=W=2 reclaim storm the
sender's own view kept ``reclaimed`` for peers whose file still referenced
it, so every queued or new send sat in the outbox as ``LINK_MINTING`` with
``attempts=0`` and ``hyprial link add`` answered ``committed`` yet
``reclaimed``.  The in-memory reclaim marker outranked the file copies and
was only cleared once *both* sides had withdrawn, which never happens while
the peer stays established.

Every case drives the real ``LinkService`` over the real ``LinkStore``, the
real inbox custody facade and its SQLite ``link_ledger``; only the orgfs is
the in-memory double.  The peer that keeps its reference lives on another
node (``n2``), so nothing local withdraws it on our behalf.  The window
switch is ``observe``, the production default, so W/cwnd never hold a row.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.composition.links import FacadeLinkInboxPort
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.contracts.api import LinkLedgerOpen
from hyprial.daemon.impl.links.model import LinkEntry
from hyprial.daemon.impl.links.ports import EVENT_LINK_MINTED, Allow, Hold, Mint
from hyprial.kernel import ipc_errors
from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
    ActorRuntime,
    SupervisionPolicy,
)
from tests.support.links.org import (
    FakeClock,
    FakeOrgFs,
    RecordingPublisher,
    establish,
    make_service,
    write_reference,
)

A = "agent:o:n1:a"
LOCAL_PEER = "agent:o:n1:b"
REMOTE_PEER = "agent:o:n2:b"
LOCAL = (A, LOCAL_PEER, "agent:o:n1:c")
TTL_MS = 600_000


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


@dataclass
class _Node:
    coordinator: DeliveryCustodyCoordinator
    facade: DeliveryCustodyFacade
    port: FacadeLinkInboxPort
    service: Any
    publisher: RecordingPublisher
    database: Path
    now: int

    def tick(self, times: int = 1) -> None:
        for _ in range(times):
            self.service.tick(self.now)

    def close(self) -> None:
        assert self.coordinator.drain(2.0).complete is not False


def _boot(tmp_path: Path, fs: FakeOrgFs) -> _Node:
    now = time.time_ns() // 1_000_000
    database = tmp_path / "inbox.sqlite3"
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
    publisher = RecordingPublisher()
    harness = make_service(
        fs=fs,
        inbox=port,
        clock=FakeClock(now),
        local_names=("a", "b", "c"),
        publisher=publisher,
        window_mode="observe",
    )
    slot.bind(harness.service)
    return _Node(coordinator, facade, port, harness.service, publisher, database, now)


def _org() -> FakeOrgFs:
    fs = FakeOrgFs()
    fs.add_space("s1")
    return fs


def _msg(message_id: str, target: str, *, now: int) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=A,
        recipient=target,
        payload=json.dumps({"text": "hi"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=now,
        expires_at_ms=now + TTL_MS,
        idempotency_key=f"idem-{message_id}",
    )


def _send(node: _Node, message_id: str, target: str) -> Any:
    """The send boundary: admit, durable submit with its ledger row, observe."""

    now = node.now
    decision = node.service.admit_send(
        sender=A, target=target, intent="request", reply_to=None, now_ms=now
    )
    if isinstance(decision, Mint):
        hold: str | None = ipc_errors.LINK_MINTING
    elif isinstance(decision, Hold):
        hold = decision.reason
    else:
        assert isinstance(decision, Allow)
        hold = None
    node.facade.submit(
        _msg(message_id, target, now=now),
        now_ms=now,
        link_hold=hold,
        link_open=LinkLedgerOpen(
            actor=A,
            message_id=message_id,
            peer=target,
            intent="request",
            reply_to=None,
            state="held" if hold else "open",
            opened_at_ms=now,
            expires_at_ms=now + TTL_MS,
        ),
    )
    node.service.submitted(
        sender=A,
        target=target,
        message_id=message_id,
        reply_to=None,
        hold_reason=hold,
        now_ms=now,
    )
    return decision


def _state(node: _Node, actor: str, peer: str) -> str | None:
    for link in node.service.list_links(actor)["links"]:
        if link["peer"] == peer:
            return link["state"]
    return None


def _held_ids(node: _Node) -> list[str]:
    return sorted(row.message_id for row in node.port.held_outbox())


def _ledger_state(database: Path, message_id: str) -> str | None:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT state FROM link_ledger WHERE actor = ? AND message_id = ?"
            " AND direction = 'out'",
            (A, message_id),
        ).fetchone()
    return None if row is None else row[0]


def _reclaim_while_peer_stays(node: _Node, fs: FakeOrgFs, peer: str) -> None:
    establish(fs, A, peer, now_ms=node.now)
    node.tick()
    assert _state(node, A, peer) == "established"
    node.service.drop(actor=A, peer=peer, now_ms=node.now)
    node.tick()
    assert _state(node, A, peer) == "reclaimed"


def test_new_send_to_a_reclaimed_but_still_referencing_peer_re_establishes(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _reclaim_while_peer_stays(node, fs, REMOTE_PEER)

        assert isinstance(_send(node, "m-new", REMOTE_PEER), Mint)
        node.tick(3)

        assert _state(node, A, REMOTE_PEER) == "established"
        assert _held_ids(node) == []
        assert _ledger_state(node.database, "m-new") == "open"
        minted = [
            payload
            for payload in node.publisher.payloads(EVENT_LINK_MINTED)
            if payload["peer"] == REMOTE_PEER
        ]
        assert len(minted) == 2
    finally:
        node.close()


def test_rows_queued_while_reclaimed_are_released_in_order_after_re_mint(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _reclaim_while_peer_stays(node, fs, REMOTE_PEER)
        _send(node, "m-old-1", REMOTE_PEER)
        node.now += 1
        _send(node, "m-old-2", REMOTE_PEER)
        node.tick(3)

        assert _held_ids(node) == []
        assert _ledger_state(node.database, "m-old-1") == "open"
        assert _ledger_state(node.database, "m-old-2") == "open"
        assert _state(node, A, REMOTE_PEER) == "established"
    finally:
        node.close()


def test_operator_add_on_a_reclaimed_link_re_establishes_and_releases(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _reclaim_while_peer_stays(node, fs, REMOTE_PEER)
        added = node.service.add(actor=A, peer=REMOTE_PEER, now_ms=node.now)
        assert added["state"] != "reclaimed", added
        node.tick(2)
        assert _state(node, A, REMOTE_PEER) == "established"

        assert isinstance(_send(node, "m-after-add", REMOTE_PEER), Allow)
        assert _held_ids(node) == []
    finally:
        node.close()


def test_operator_add_after_a_stuck_send_releases_the_stuck_row(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _reclaim_while_peer_stays(node, fs, REMOTE_PEER)
        _send(node, "m-stuck", REMOTE_PEER)
        node.tick(3)
        added = node.service.add(actor=A, peer=REMOTE_PEER, now_ms=node.now)
        node.tick(2)

        assert added["state"] != "reclaimed", added
        assert _state(node, A, REMOTE_PEER) == "established"
        assert _held_ids(node) == []
        assert _ledger_state(node.database, "m-stuck") == "open"
    finally:
        node.close()


def test_reclaim_and_re_mint_in_one_pass_converge_both_sides(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        establish(fs, A, LOCAL_PEER, now_ms=node.now)
        node.tick()
        assert _state(node, A, LOCAL_PEER) == "established"
        node.service._reclaim(A, LOCAL_PEER, "evicted", node.now)
        assert isinstance(_send(node, "m-evict-remint", LOCAL_PEER), Mint)
        node.tick(3)

        assert _state(node, LOCAL_PEER, A) == "established"
        assert _state(node, A, LOCAL_PEER) == "established"
        assert _held_ids(node) == []
        assert _ledger_state(node.database, "m-evict-remint") == "open"
    finally:
        node.close()


def test_a_stale_copy_of_the_withdrawn_reference_stays_reclaimed(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        write_reference(
            fs,
            A,
            {
                REMOTE_PEER: LinkEntry(
                    peer=REMOTE_PEER, minted_at_ms=node.now, revision=5
                )
            },
            revision=5,
        )
        write_reference(
            fs,
            REMOTE_PEER,
            {A: LinkEntry(peer=A, minted_at_ms=node.now, revision=2)},
            revision=2,
        )
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        node.service._reclaim(A, REMOTE_PEER, "evicted", node.now)
        with node.service._mem:
            node.service._state(A).pending_file = None
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
    finally:
        node.close()
