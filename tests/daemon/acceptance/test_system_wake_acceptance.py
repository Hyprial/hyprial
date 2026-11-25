"""Acceptance (e): system messages never wake a human (observe mode, Q6).

Phase A promise (design §10.0, S5): "system messages do not wake humans".
Executor decision Q6 ships the human-wake gate in observe mode: on every
human wake path a SYSTEM message logs ``delivery.wake.would_suppress``
(mode ``observe``) and the wake still goes out; INTERACTION traffic logs
nothing.  So in phase A the gate suppresses nothing, and the log names
exactly what an enforcing gate would hold back.

SYSTEM producers exercised on a real golden daemon (ALPHA's cell binds the
golden adapter, so a ``user:op`` send is pushed to its chat, I3/I7a), each
through its
production entry:
a PAC notification to ``user:op`` through the PAC notification IO
(``dispatch`` message id), the operator alarm and the OrgFS resident-replica
owner notice (daemon notices, observed at their site before they are
logged), and a dispatch-id request received by the custody inbox for a
``user:`` recipient (``inbox.receive``).  R2 (owner ruling 2026-11-19): the
two owner watchdogs only log their notice and reach no wake path, so they
are not observed at all.

INTERACTION controls that reach the gate and must log nothing: a run
report with an ordinary id (``user.workflow``), an ordinary-id
request received by the custody inbox for ``user:op`` (``inbox.receive``)
and an agent's ``message.send`` to ``user:op`` (``user.send``).  A
spy on ``ObserveWakeGate.observe`` shows that each of them was asked, so
their absence from the log is a class decision, not a missing call.

PIN: passed on first run.  Seeded red cases (temporary product edits,
reverted): ``ObserveWakeGate.observe`` never logging -> no would-suppress
record; ``ObserveWakeGate.observe`` logging every class -> the report and
the ordinary-id receive appear in the log.
"""

from __future__ import annotations

import json
import time
from typing import Any

from hyprial.daemon.impl.delivery import WOULD_SUPPRESS_EVENT, ObserveWakeGate
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX
from tests.daemon.acceptance.acceptance_support import capture_logs, send
from tests.support.golden.app import (
    ALPHA,
    OWNER_USER,
    GoldenDaemon,
    golden_daemon as golden_daemon,
)

DISPATCHER = "agent:op:node-1:dispatch-coordinator"
RECEIVED_ID = f"{DISPATCH_MESSAGE_PREFIX}0f0f0f0f-1111-4222-8333-444455556666"
ORDINARY_RECEIVED_ID = "6c1d2e3f-4a5b-4c6d-8e7f-901a2b3c4d5e"
REPORT_ID = "3b0e9a52-7d41-4f5c-9a43-1d2e3f405162"


def _received(app: Any, clock_ms: int, message_id: str = RECEIVED_ID) -> Any:
    return app._inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id="pac-golden",
            sender=DISPATCHER,
            recipient=OWNER_USER,
            payload=json.dumps({"message": "graph finished"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=clock_ms,
            idempotency_key=f"workflow:received:{message_id}",
            summary="graph finished",
        ),
        now_ms=clock_ms,
    )


def _asked_paths(monkeypatch: Any) -> dict[str, str]:
    asked: dict[str, str] = {}
    original = ObserveWakeGate.observe

    def observe(self: Any, message: Any, *, path: str) -> Any:
        asked.setdefault(str(message.message_id), path)
        return original(self, message, path=path)

    monkeypatch.setattr(ObserveWakeGate, "observe", observe)
    return asked


def _logged_id(logs: Any, event: str) -> str:
    [record] = logs.named(event)
    return str(record["messageId"])


def _alarm_and_notice(daemon: GoldenDaemon, logs: Any) -> list[str]:
    app = daemon.app
    app._raise_harness_failed_alarm("codex:worker-7")
    deadline = time.monotonic() + 5.0
    while not logs.named("daemon.operator_alarm.notice"):
        assert time.monotonic() < deadline, "the operator alarm was never logged"
        time.sleep(0.01)
    app._notify_orgfs_owner(OWNER_USER, "replica.diverged", {"spaceId": "space-1"})
    return [
        _logged_id(logs, "daemon.operator_alarm.notice"),
        _logged_id(logs, "orgfs.owner_notice"),
    ]


def test_system_messages_are_logged_as_would_suppress_and_still_delivered(
    golden_daemon, monkeypatch
) -> None:
    logs = capture_logs(monkeypatch)
    asked = _asked_paths(monkeypatch)
    daemon = golden_daemon()
    app = daemon.app
    from tests.support.golden.app import ADAPTER
    from tests.support.identity.cell_push import edit_gateway, install_cell, push_cell

    alpha = app.agents.require("alpha")
    edit_gateway(app, defaultRoute="dm")
    install_cell(app, push_cell(alpha, {alpha.uri: ("harness", None), "im": ("adapter", ADAPTER)}))

    notified = app._pac_notification_io.deliver(
        effect_id="pac-effect-1",
        sender=DISPATCHER,
        target=OWNER_USER,
        conversation_id="pac-golden",
        text="PAC graph golden finished",
        summary="PAC graph golden finished",
    )
    quota = app._quota_watchdog_deliver("quota-alert-1", "quota is low")
    inbox_alert = app._inbox_watchdog_deliver(
        "inbox-alert-1", "mail collection stalled"
    )
    received = _received(app, daemon.clock.ms())

    interaction = send(app, ALPHA, OWNER_USER, "to-owner")
    assert interaction["deliveries"][0]["accepted"] is True
    assert (
        app._deliver_report_to_user(OWNER_USER, "run report", REPORT_ID, ALPHA) is True
    )
    ordinary = _received(app, daemon.clock.ms(), ORDINARY_RECEIVED_ID)
    assert ordinary.accepted is True and ordinary.wake is True
    alarm_id, notice_id = _alarm_and_notice(daemon, logs)

    assert notified.message_id.startswith(DISPATCH_MESSAGE_PREFIX)
    assert quota.delivered is True and inbox_alert.delivered is True
    assert received.accepted is True and received.wake is True
    watchdog_ids = {
        _logged_id(logs, "quota_watchdog.notice"), _logged_id(logs, "inbox_watchdog.notice"),
    }
    assert daemon.user_delivery.requests == []

    observed = logs.named(WOULD_SUPPRESS_EVENT)
    by_id = {str(record["messageId"]): record for record in observed}
    assert len(by_id) == len(observed), "one observation per message"
    assert set(by_id) == {notified.message_id, RECEIVED_ID, alarm_id, notice_id}
    assert not watchdog_ids & set(asked)
    assert {by_id[message_id]["path"] for message_id in by_id} == {
        "user.workflow",
        "user.operator-alarm",
        "user.orgfs-owner-notice",
        "inbox.receive",
    }
    assert asked[interaction["messageId"]] == "user.send"
    assert asked[REPORT_ID] == "user.workflow"
    assert asked[ORDINARY_RECEIVED_ID] == "inbox.receive"
    for record in observed:
        assert record["mode"] == "observe" and record["messageClass"] == "system", (
            record
        )
    assert not logs.named("delivery.wake.observe_failed")
