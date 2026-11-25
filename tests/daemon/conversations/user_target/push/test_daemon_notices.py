"""R2, owner rulings 2026-11-19: the daemon no longer sends to the user.

- Daemon monitoring notices (quota and inbox watchdogs, the operator alarm,
  the OrgFS integrity notice, owner alerts such as "agent blocked") are
  written to the daemon log at warn level with their full text and
  idempotency key, and nothing is sent or queued to ``user:<owner>``.
- A routine or workflow report to the owner is sent by its owning agent as
  an ordinary agent -> ``user:<owner>`` message (queued, recorded in that
  agent's default conversation with the owner, case 3 push).  With no local
  owning agent (the owner's own action, a remote origin, no actor) it is
  logged as ``workflow.user_report.unsent`` and nothing is sent.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryRequest, UserDeliveryResult
from hyprial.daemon.impl.conversations.ids import default_conversation_id, endpoint_key
from hyprial.daemon.impl.dispatch.alarm import DispatchAlarm
from tests.daemon.conversations.bypass.bypass_support import settle
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    record_ends,
)
from tests.support.golden.app import ADAPTER, ALPHA, OWNER_USER, golden_daemon as golden_daemon


class RecordingPort:
    def __init__(self) -> None:
        self.requests: list[UserDeliveryRequest] = []

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        return UserDeliveryResult(request.message_id, True)


def _boot(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch):
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    port = RecordingPort()
    booted.app._user_delivery = port
    return booted, port


def _nothing_sent(app: Any, port: RecordingPort) -> None:
    assert port.requests == []
    assert app._inbox.pending_messages(OWNER_USER) == ()


def _notice(booted: Any, event: str) -> dict[str, Any]:
    [record] = booted.logs.named(event)
    assert record["level"] in ("warn", "error")
    return record


@pytest.mark.parametrize(
    ("method", "event"),
    [
        ("_quota_watchdog_deliver", "quota_watchdog.notice"),
        ("_inbox_watchdog_deliver", "inbox_watchdog.notice"),
    ],
)
def test_a_watchdog_alert_is_logged_with_its_text_and_sends_nothing(
    golden_daemon, monkeypatch, method, event
) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app

    outcome = getattr(app, method)("alert-key-1", "provider quota at 92%")

    assert outcome.delivered is True
    record = _notice(booted, event)
    assert (record["text"], record["idempotencyKey"]) == ("provider quota at 92%", "alert-key-1")
    _nothing_sent(app, port)


def test_the_orgfs_integrity_notice_is_logged_with_its_text_and_sends_nothing(
    golden_daemon, monkeypatch
) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app

    app._notify_orgfs_owner(OWNER_USER, "replica.diverged", {"spaceId": "space-1"})

    record = _notice(booted, "orgfs.owner_notice")
    assert "replica.diverged" in record["text"] and "space-1" in record["text"]
    assert record["idempotencyKey"]
    _nothing_sent(app, port)


def test_an_operator_alarm_is_logged_with_its_text_and_sends_nothing(
    golden_daemon, monkeypatch
) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app

    app._raise_harness_failed_alarm("codex:worker-7")
    deadline = time.monotonic() + 5
    while not booted.logs.named("daemon.operator_alarm.notice"):
        assert time.monotonic() < deadline, "no operator alarm notice logged"
        time.sleep(0.01)

    record = _notice(booted, "daemon.operator_alarm.notice")
    assert "worker-7" in record["text"] and record["idempotencyKey"]
    _nothing_sent(app, port)


def test_a_report_is_sent_by_its_owning_agent_into_its_conversation_with_the_owner(
    golden_daemon, monkeypatch
) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app

    assert app._workflow_deliver_user(OWNER_USER, "graph g1 node n1 completed", "workflow-1", ALPHA)

    [row] = app._inbox.pending_messages(OWNER_USER)
    assert row.sender == ALPHA
    assert row.conversation_id == default_conversation_id(
        endpoint_key(ALPHA), endpoint_key(OWNER_USER)
    )
    assert port.requests == []
    settle(booted)
    assert record_ends(app, row.message_id) == {
        ("inbox", ALPHA, OWNER_USER), ("sender_copies", ALPHA, OWNER_USER),
    }



def test_a_report_from_an_adapter_bound_agent_is_pushed_to_its_chat(
    golden_daemon, monkeypatch
) -> None:
    """I3 closes KP-113: a report from a bound owning agent is pushed to its chat.

    The report is queued and recorded like any message from that agent; the
    daemon posts the owner's copy to the chat the agent's cell names, and
    the user-delivery transport is not handed a copy.
    """

    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    from tests.support.identity.cell_push import edit_gateway
    from tests.support.identity.cell_runtime import bind_adapters, registry_cell_runtime

    edit_gateway(app, defaultRoute="dm")
    registry_cell_runtime(app)
    bind_adapters(app, app.agents.require("alpha"), (ADAPTER,))
    sends: list[dict[str, Any]] = []
    handle = app.handle

    def observed(method: str, params: Any = None, *args: Any, **kwargs: Any) -> Any:
        result = handle(method, params, *args, **kwargs)
        if method == "message.send":
            sends.append(result)
        return result

    monkeypatch.setattr(app, "handle", observed)

    assert app._workflow_deliver_user(OWNER_USER, "graph g1 node n3 completed", "workflow-3", ALPHA)

    [sent] = sends
    [delivery] = sent["deliveries"]
    assert (delivery["accepted"], delivery["pushed"], delivery["via"]) == (True, True, "cell")
    [row] = app._inbox.pending_messages(OWNER_USER)
    assert row.sender == ALPHA
    assert port.requests == []
    [(chat, text, _key)] = booted.daemon.gateway.calls
    assert chat == "oc_dm_chat" and text.endswith("graph g1 node n3 completed")
    settle(booted)
    assert record_ends(app, row.message_id) == {
        ("inbox", ALPHA, OWNER_USER), ("sender_copies", ALPHA, OWNER_USER),
    }


@pytest.mark.parametrize(
    "sender",
    [None, OWNER_USER, "agent:carol:other-node:helper", "pac:graph"],
    ids=["none", "owner-own-action", "remote-agent", "no-agent"],
)
def test_a_report_with_no_local_owning_agent_is_logged_and_not_sent(
    golden_daemon, monkeypatch, sender
) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app

    assert app._workflow_deliver_user(OWNER_USER, "node n2 is overdue", "workflow-2", sender)

    record = _notice(booted, "workflow.user_report.unsent")
    assert (record["text"], record["idempotencyKey"]) == (
        "node n2 is overdue", "workflow-escalate:workflow-2",
    )
    _nothing_sent(app, port)


def test_a_routine_escalation_is_sent_by_the_routine_agent(golden_daemon, monkeypatch) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    alarm = DispatchAlarm(app._inbox.alarm_emitter, app._workflow_deliver_user, app._logger)

    result = alarm.escalate(
        to=OWNER_USER, text="routine r1 timed out", reason="ROUTINE_ALARM",
        conversation_id="routine:r1", idempotency_key="routine-alarm:r1:1", sender=ALPHA,
    )

    assert result.status == "delivered"
    [row] = app._inbox.pending_messages(OWNER_USER)
    assert row.sender == ALPHA
