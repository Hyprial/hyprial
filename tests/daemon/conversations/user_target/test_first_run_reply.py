"""UA2: the first-run attempt confirms an Agent's reply that is delivered to the owner.

In-process mirror of
``tests/integration/request_http/onboarding/test_first_run_reply_http.py``:
the real ``FirstRunAttemptService`` with the daemon's own send and reply
authorities (``first_run.selection``) over the golden application, which
holds the owner's user agent and runs the conversation recorder.  Agent
selection and the login epoch are fixed fakes; the HTTP surface and the
identity registry are not under test here.

Before UA2 a reply to ``user:<owner>`` had no local recipient: it waited in
the outbox (``queued`` true) and the owner had no pending row.  Now it is
delivered to this node's ``user:<owner>`` actor (``queued`` false), pending
until the recorder records it and acknowledges it.  Confirmation reads the
retained durable row, so it holds both before and after that ack.

R2 reviewed this pin deliberately, I7a keeps it: ``replyPathUnavailable`` on
the owner's send means "no agent cell here names a chat" (a reply reaches
GUI and CLI only), no longer "the squire profile or its open id is
incomplete".  The golden node binds no adapter, so the pinned ``True``
stands; with the user agent cell naming a chat the flag is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.first_run.selection import (
    reply_reader,
    send_binding_reader,
)
from hyprial.daemon.impl.application.onboarding.first_run.service import (
    AgentSelection,
    FirstRunAttemptService,
)
from hyprial.daemon.impl.onboarding.first_run import (
    FIRST_RUN_REPLY_UNVERIFIED,
    FirstRunStore,
    FirstRunVerificationError,
)
from hyprial.daemon.impl.onboarding.first_run_attempt import (
    FirstRunAttemptStore,
    FirstRunCredentialBinding,
)
from hyprial.identity import OnboardingSubject
from tests.daemon.acceptance.acceptance_support import session
from tests.daemon.conversations.bypass.bypass_support import settle
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, ALPHA, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import bind_user_agent

SUBJECT = OnboardingSubject(
    issuer="https://issuer.example.test",
    user_id="p3-synthetic-user",
    device_uid="12345678123442348234123456789abc",
)
SELECTION = AgentSelection(
    agent_uri=ALPHA,
    agent_created_at_ms=1_760_000_000_000,
    provider="deepseek",
    model="deepseek-flash",
    harness="pi",
    config_revision=None,
    credential=FirstRunCredentialBinding(
        kind="key", grant_id="p3-probe", entry_id="p3-probe",
        entity_token="entity-1", revision=1, route_required=False,
    ),
)


def _service(app, root: Path) -> FirstRunAttemptService:
    onboarding = root / "onboarding"
    return FirstRunAttemptService(
        home=root,
        onboarding_root=onboarding,
        attempts=FirstRunAttemptStore(onboarding),
        receipts=FirstRunStore(onboarding),
        login_epoch=lambda _home: 1,
        select_agent=lambda uri: SELECTION if uri == ALPHA else None,
        send_binding=send_binding_reader(app._inbox),
        reply_for=reply_reader(app._inbox),
    )


def _pending(app) -> list[str]:
    listed = app.handle("message.pending.list", {"from": OWNER_USER})["messages"]
    return [str(item["messageId"]) for item in listed]


def test_first_run_confirms_the_reply_delivered_to_the_owner(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    service = _service(app, tmp_path)
    attempt = service.begin(SUBJECT, operation_key="p3-http-probe-0123456789", agent_uri=ALPHA)
    assert attempt.state == "pending"

    sent = app.handle("message.send", {
        "from": OWNER_USER, "to": [ALPHA],
        "message": "P3 controlled request", "summary": "P3 controlled request",
        "idempotencyKey": attempt.send_key, "conversationId": "p3-probe-conversation",
    })
    attempt = service.bind(
        SUBJECT, trusted_sender=OWNER_USER, attempt_id=attempt.attempt_id,
        expected_revision=attempt.revision, operation_key=attempt.operation_key,
        conversation_id=sent["conversationId"], request_message_id=sent["messageId"],
    )

    def confirm():
        return service.confirm(
            SUBJECT, trusted_sender=OWNER_USER, attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision, expected_receipt_revision=0,
            operation_key=attempt.operation_key,
        )

    with pytest.raises(FirstRunVerificationError) as premature:
        confirm()
    assert premature.value.code == FIRST_RUN_REPLY_UNVERIFIED

    owner_rows = [row for row in app.handle("targets", {"kind": "user"})["targets"]
                  if row["targetUri"] == OWNER_USER]
    assert [row["status"] for row in owner_rows] == ["user-agent"]
    replied = app.handle("message.reply", {
        **session(ALPHA), "messageId": sent["messageId"],
        "summary": "Controlled reply", "message": "Controlled reply",
    })
    assert sent["replyPathUnavailable"] is True
    assert replied["replied"] is True
    assert replied["queued"] is False
    assert _pending(app) == [replied["replyMessageId"]]

    confirmed = confirm()
    assert confirmed.reply_message_id == replied["replyMessageId"]
    assert confirmed.first_run.confirmed == ("configuration", "reply")
    assert confirmed.attempt.state == "confirmed"
    settle(booted)
    assert _pending(app) == []
    assert confirm() == confirmed


def test_with_a_bound_user_agent_cell_the_owner_send_has_a_reply_path(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    bind_user_agent(booted.app, ADAPTER)

    sent = booted.app.handle("message.send", {
        "from": OWNER_USER, "to": [ALPHA],
        "message": "with a proxy", "summary": "with a proxy",
        "idempotencyKey": "p3-with-proxy-0123456789", "conversationId": "p3-proxy",
    })

    assert "replyPathUnavailable" not in sent
