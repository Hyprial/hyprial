"""R23 / E26-08: a reclaimed request's drained residue is not readable.

H3 booked a durable ``drained`` terminal row for an inbound request the
ledger had never seen (never injected).  That row entered the link gate's
``settled`` set, so ``admit_dispatch`` answered True for it and the retained
row fallback of ``message.read_body`` handed out the body of a request that
no reader ever held.

This is the real composition: a ``DaemonApplication`` over a real
``DeliveryCustodyFacade`` (the same SQLite file holds the inbox and the link
ledger), a real ``LinkService`` bound into the app's one admission slot, the
app's own ``message.read_body`` handler, and only the orgfs ``link/`` files
supplied by the shared link doubles.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.composition.links import FacadeLinkInboxPort
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
    ActorRuntime,
    SupervisionPolicy,
)
from tests.daemon.delivery.origin.test_inbound_origin import _app
from tests.support.links.org import FakeClock, FakeOrgFs, establish, make_service
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)

READER = "agent:op:node-1:reader"
DRIVER = "agent:op:node-1:driver"
SESSION = {"actor": "reader", "sessionRef": "session-reader"}
#: The inbox projection reads live rows against the wall clock, so the whole
#: arrangement must sit in the present, not at a fake epoch.  Taken per test
#: (see ``_present``): a module-import clock goes stale once a full run
#: reaches these tests more than ``TTL_MS`` after collection.
NOW = 0
TTL_MS = 600_000


@pytest.fixture(autouse=True)
def _present() -> None:
    global NOW
    NOW = time.time_ns() // 1_000_000


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


def _composed(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real app + real inbox ledger + a real link gate on one slot."""

    app = _app(tmp_path)
    app._inbox.close()
    database = tmp_path / "custody.sqlite3"
    slot = LinkAdmission()
    app._link_admission = slot
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        runtime=_runtime(),
        node_id="node-1",
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    app._inbox = facade
    app._inbox_coordinator = coordinator
    fs = FakeOrgFs()
    fs.add_space("s1")
    harness = make_service(
        fs=fs,
        inbox=FacadeLinkInboxPort(
            facade, local_agents=app.agents.local_actors
        ),
        owner="op",
        node="node-1",
        local_names=("driver", "reader"),
        clock=FakeClock(NOW),
    )
    slot.bind(harness.service)
    return app, facade, harness


def _request(message_id: str = "rC", body: str = "rC-body") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=DRIVER,
        recipient=READER,
        payload=json.dumps({"message": body}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=NOW,
        expires_at_ms=NOW + TTL_MS,
    )


def _ledger_rows(app: Any, message_id: str) -> list[Any]:
    return [
        row
        for row in app._inbox.link_ledger_snapshot(NOW + 10_000)
        if row.message_id == message_id
    ]


def _arrange_unevaluated_request(app, facade, harness) -> None:  # type: ignore[no-untyped-def]
    """C's request reaches B's inbox and B reclaims the link before a fetch."""

    establish(harness.fs, READER, DRIVER, now_ms=NOW)
    harness.tick(NOW)
    assert facade.receive(_request(), now_ms=NOW).accepted
    # The E2E lists B's pending rows before the drop; that listing admits the
    # request at dispatch (``admitted_pending``), so the reclaim drains an id
    # the gate has already admitted but no reader has ever held.
    listed = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in listed["messages"]] == ["rC"]
    dropped = harness.service.drop(actor=READER, peer=DRIVER, now_ms=NOW)
    assert dropped["state"] == "reclaiming"
    harness.tick(NOW + 1)


def test_r23_a_drained_request_that_was_never_injected_is_not_readable(
    tmp_path: Path,
) -> None:
    app, facade, harness = _composed(tmp_path)
    _arrange_unevaluated_request(app, facade, harness)

    # The durable terminal H3 booked stays: drained ledger row, consumed and
    # acknowledged inbox row, settle hint ack, never fetched.
    assert harness.service.settle_hint(actor=READER, message_id="rC") == "ack"
    assert facade.has_fetched("rC") is False
    rows = _ledger_rows(app, "rC")
    assert len(rows) == 1
    assert (rows[0].direction, rows[0].state) == ("in", "drained")

    with pytest.raises(DaemonRequestError) as error:
        app.handle("message.read_body", {**SESSION, "messageId": "rC"})

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE


def test_r23_a_processed_settled_row_stays_readable(tmp_path: Path) -> None:
    """The refusal is for the drain, not for every settled inbound id."""

    app, facade, harness = _composed(tmp_path)
    establish(harness.fs, READER, DRIVER, now_ms=NOW)
    harness.tick(NOW)
    assert facade.receive(_request(), now_ms=NOW).accepted
    # The explicit fetch admits and stamps the row: it was held by a reader.
    fetched = app.handle("message.pending.list", {**SESSION, "fetched": True})
    assert [row["messageId"] for row in fetched["messages"]] == ["rC"]
    assert app.handle(
        "message.ack", {**SESSION, "messageId": "rC"}
    )["acknowledged"] is True

    body = app.handle("message.read_body", {**SESSION, "messageId": "rC"})

    assert body["message"] == "rC-body"
    assert body["pending"] is False
    assert [row.state for row in _ledger_rows(app, "rC")] == ["acked"]
