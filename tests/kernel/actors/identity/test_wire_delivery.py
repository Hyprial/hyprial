"""actor_identity: wire delivery scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
from pathlib import Path
from hyprial.daemon.impl.inbox import (
    InboxService,
)

from .support import (
    PI_WORKER,
    PI_WORKER_WITH_NICKNAME,
    _AutoConfirmTransport,
    _registered,
    application,
)


def test_wire_delivery_carries_the_canonical_uri_recipient(tmp_path: Path) -> None:
    """Send to a short name, then confirm the message on the delivery wire
    carries the canonical URI — the exact key the daemon pump queries and
    the registered inbox endpoint listens on."""

    transport = _AutoConfirmTransport()
    transport.set_online("agent:op:node-1:kanban-sim", True)
    app = application(tmp_path, providers=(PI_WORKER,))
    app._inbox = InboxService(tmp_path / "inbox-wire.sqlite3", transport)
    app.handle(
        "message.send",
        {"from": _registered(app, "requester"), "to": ["kanban-sim"], "message": "do the work", "summary": "do the work"},
    )
    (delivered,) = transport.frames
    assert delivered.recipient == "agent:op:node-1:kanban-sim"
    # The bare CLI sender canonicalizes at the daemon boundary (Allen A).
    assert delivered.sender == "agent:op:node-1:requester"


def test_wire_delivery_from_nickname_carries_the_canonical_uri_recipient(
    tmp_path: Path,
) -> None:
    """The regression the nickname bug demands: send to the operator-facing
    nickname and confirm the frame on the delivery wire carries the canonical
    URI minted from the managed name — the exact key the daemon pump queries
    and the registered inbox endpoint listens on.  Before the fix the frame
    carried the raw nickname and queued under a key nobody drained."""

    transport = _AutoConfirmTransport()
    transport.set_online("agent:op:node-1:kanban-sim", True)
    app = application(tmp_path, providers=(PI_WORKER_WITH_NICKNAME,))
    app._inbox = InboxService(tmp_path / "inbox-nick.sqlite3", transport)
    app.handle(
        "message.send",
        {"from": _registered(app, "requester"), "to": ["kanban"], "message": "do the work", "summary": "do the work"},
    )
    (delivered,) = transport.frames
    assert delivered.recipient == "agent:op:node-1:kanban-sim"
    # The bare CLI sender canonicalizes at the daemon boundary (Allen A).
    assert delivered.sender == "agent:op:node-1:requester"
