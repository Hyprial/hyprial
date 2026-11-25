"""Acceptance (a): with OrgFS unavailable, messages still flow (design §10.0).

Phase A promise: "断开 OrgFS 时消息照常收发".  A real golden daemon with
enforced links has an established alpha<->beta link; then the link OrgFS
and the cell/conversation OrgFS both go down.  Agent->agent and
agent->user sends still reach a decision (I7a: with no user agent on this
node an unbound sender's push-only copy is refused ``TARGET_UNRESOLVED``),
the agent recipients see them in
``message.pending.list``, read, reply and ack them.  Nothing is recorded or
granted for dropping, and a retention sweep run during the outage, at a
time past the default body retention, drops no body.  When OrgFS returns,
the conversation recorder catches up: the request, the reply and the
operator message land in their conversation records and each is granted.

Two outage shapes: OrgFS calls raise ``unavailable`` (``fs-raises``), or
the link spaces vanish and the OrgFS runtime itself is unreachable
(``space-unreachable``).  Decision (slice C1, 2026-11-19): the recorder no
longer composes the cell runtime (a conversation is a storage category, not
a cell), so the old first-pass behaviour in the second shape changed.

- Old assertion: after OrgFS returned, the first recorder pass was still
  ``unavailable`` — the pass reached for ``cell_runtime(app)``, whose
  composition had failed during the outage and backed off for
  ``COMPOSE_RETRY_S`` (30 s of monotonic time; the test aged the recorded
  failure past it with ``skip_compose_backoff`` instead of sleeping), and
  ``app._cell_runtime`` stayed None with ``_cell_runtime_failed_at`` set.
  That delay was the composition retry throttle the recorder shared by
  needing the runtime for its author and the cell document, not an OrgFS
  gate — OrgFS was already answering again.
- New assertion: after OrgFS returns, the first pass records every row the
  queue still owes a record for (the inbox rows and sender copies of the
  request, the reply and the operator message, counted from the queue just
  before the pass), grants each of them and writes nothing but record
  directories under
  ``cells/conversation`` (no ``<c.id>.json`` cell document any more).  The
  write target is unchanged: the authority comes from the same
  ``local_author`` rule the cell runtime's author used, and the space is
  the same ``CellSpaces.private_space`` owner home the record path always
  wrote to.

I3 (contract i7-0 v2 section 3.2 row 6 and 3.4): the owner copy is decided
from the sender's cell.  When the OrgFS runtime itself is unreachable the
cell runtime cannot compose, the sender's binding is unknown, and this
daemon has no user agent, so the push-only send to ``user:<owner>`` is
refused with ``USER_DELIVERY_UNAVAILABLE`` and nothing is handed to the
proxy.  Agent to agent and the operator's own messages still flow.

PIN: passed on first run.  Seeded red cases (temporary product edits,
reverted): ``LinkIndex.continuity`` returning False (Q5 off) -> the
established link leaves ``link.list`` under the outage; the recorder
cursor advanced past failed rows -> the catch-up records nothing; the
body sweep ignoring the grant (no ``body_payload_sha256`` filter, no digest
check) -> a body is dropped during the outage.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.delivery import DEFAULT_BODY_RETENTION_MS
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.acceptance.acceptance_support import (
    DAY_MS,
    OUTAGE_MODES,
    Outage,
    attach_cell_orgfs,
    body_columns,
    pending_ids,
    send,
    session,
)
from tests.daemon.conversations.conversation_support import home_space, record_lines
from tests.support.golden.app import (
    ALPHA,
    BETA,
    OWNER_USER,
    golden_daemon as golden_daemon,
)


def _delivered(response: dict[str, Any]) -> str:
    [delivery] = response["deliveries"]
    assert delivery["accepted"] is True, response
    assert "holdReason" not in delivery and delivery.get("queued") is False, response
    return str(response["messageId"])


@pytest.mark.parametrize("mode", OUTAGE_MODES)
def test_messages_flow_with_orgfs_down_and_records_catch_up(
    golden_daemon, monkeypatch, mode: str
) -> None:
    daemon = golden_daemon(bypass=False)
    app = daemon.app
    cells = attach_cell_orgfs(app, monkeypatch)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    outage = Outage(daemon, cells, mode)

    outage.down()
    daemon.clock.advance(1_000)
    daemon.tick()

    request = _delivered(send(app, ALPHA, BETA, "down-request"))
    if mode == "space-unreachable":
        with pytest.raises(DaemonRequestError) as refused:
            send(app, ALPHA, OWNER_USER, "down-user")
        assert refused.value.code == ipc_errors.USER_DELIVERY_UNAVAILABLE
    else:
        with pytest.raises(DaemonRequestError) as refused:
            send(app, ALPHA, OWNER_USER, "down-user")
        assert refused.value.code == ipc_errors.TARGET_UNRESOLVED
    from_user = _delivered(send(app, OWNER_USER, BETA, "down-owner"))
    assert sorted(pending_ids(app, BETA)) == sorted([request, from_user])
    assert daemon.user_delivery.requests == []

    body = app.handle("message.read_body", {**session(BETA), "messageId": request})
    assert body["message"] == "body down-request ☃"
    reply = app.handle(
        "message.reply",
        {**session(BETA), "messageId": request, "summary": "answer", "message": "42 ☃"},
    )
    reply_id = str(reply["replyMessageId"])
    assert pending_ids(app, ALPHA) == [reply_id]
    for actor, message_id in ((ALPHA, reply_id), (BETA, request), (BETA, from_user)):
        app.handle("message.read_body", {**session(actor), "messageId": message_id})
        acked = app.handle("message.ack", {**session(actor), "messageId": message_id})
        assert acked["ok"] is True, acked
    assert pending_ids(app, ALPHA) == [] and pending_ids(app, BETA) == []

    assert recorder.run_once().failed is True
    assert outage.refusals() > 0, "the recorder never reached the down OrgFS"
    assert not any(row["granted"] for row in body_columns(app).values())
    app._inbox.prune_inbox(
        now_ms=daemon.clock.ms() + DEFAULT_BODY_RETENTION_MS + DAY_MS
    )
    for table in ("inbox", "sender_copies"):
        assert not any(row["dropped"] for row in body_columns(app, table).values())
    kept = app.handle("message.read_body", {**session(BETA), "messageId": request})
    assert kept["message"] == "body down-request ☃"

    outage.up()
    daemon.clock.advance(1_000)
    daemon.tick()
    owed = sum(len(body_columns(app, table)) for table in ("inbox", "sender_copies"))
    caught_up = recorder.run_once()
    assert caught_up.failed is False and caught_up.unavailable is False
    assert owed >= 6
    assert (caught_up.recorded, caught_up.granted) == (owed, owed)
    space = home_space(cells.inner)
    assert space is not None
    entries = cells.inner.listdir(space, "cells/conversation")
    assert all(cell.kind == "dir" for cell in entries)
    recorded = {
        line.get("messageId")
        for cell in entries
        if cell.kind == "dir"
        for line in record_lines(cells.inner, space, cell.name)
    }
    assert {request, reply_id, from_user} <= recorded
    granted = body_columns(app)
    assert all(
        granted[message_id]["granted"] for message_id in (request, reply_id, from_user)
    )
