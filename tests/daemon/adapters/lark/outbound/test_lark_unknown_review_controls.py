"""Independent-review regressions: local settlement, receipt fences and discovery."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.daemon.adapters.lark.outbound.test_lark_submission_outcome_unknown import _Routes
from tests.daemon.adapters.lark.outbound.test_lark_submission_outcome_unknown import make_rig as make_rig
from hyprial.daemon.impl.adapters.lark.state.records import DeadLetter


def test_valid_receipt_then_local_settlement_failure_is_not_terminal(make_rig, monkeypatch):
    rig = make_rig(phase="unblocked")
    _original = rig.adapter._state.record_inbound
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("isolated local receipt write failure")
    with monkeypatch.context() as patch:
        patch.setattr(rig.adapter._state, "record_inbound", fail)
        outcome = rig.adapter.handle_inbound(rig.message)
    assert rig.recipient.has_received(rig.message_id)
    assert outcome.ack_error == "SubmitOutcomeUnknownError"
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id)
    assert not rig.adapter._state.seen_message(rig.message.message_id)
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert not any("投递失败" in text or "not delivered" in text for _, text, _ in rig.platform.replies)
    assert rig.adapter.recover_message(rig.message.message_id).status == "forwarded"
    assert len(rig.recipient.pending_messages(_Routes.target.actor_id)) == 1


def receipt_for(frame):
    params = frame["params"]
    target = params["to"][0]
    message_id = str(uuid5(NAMESPACE_URL, f'hyprial:send:{params["idempotencyKey"]}:{target}:0'))
    return {"version": 1, "id": frame["id"], "result": {"ok": True, "operationId": params["idempotencyKey"], "conversationId": params["conversationId"], "messageId": message_id, "deliveries": [{"target": target, "messageId": message_id, "accepted": True, "queued": False}]}}


@pytest.mark.parametrize("mismatch", ["request-id", "version", "operation", "conversation", "message-id", "delivery-id", "target", "accepted", "queued", "negative-queued", "unknown-accepted", "negative-request-id"])
def test_unrelated_or_invalid_receipt_never_retires_frozen_custody(make_rig, mismatch):
    rig = make_rig(phase="unblocked")
    def unrelated(line):
        frame = json.loads(line)
        response = receipt_for(frame)
        result = response["result"]
        if mismatch == "request-id":
            response["id"] = "unrelated-request"
        elif mismatch == "version":
            response["version"] = 2
        elif mismatch == "operation":
            result["operationId"] = "another-operation"
        elif mismatch == "conversation":
            result["conversationId"] = "another-conversation"
        elif mismatch == "message-id":
            result["messageId"] = "unrelated-message"
        elif mismatch == "delivery-id":
            result["deliveries"][0]["messageId"] = "unrelated-message"
        elif mismatch == "target":
            result["deliveries"][0]["target"] = "agent:other:node:target"
        elif mismatch == "accepted":
            result["deliveries"][0]["accepted"] = "true"
        elif mismatch == "queued":
            result["deliveries"][0]["queued"] = "false"
        elif mismatch == "unknown-accepted":
            result["deliveries"][0]["queued"] = True
            result["deliveries"][0]["code"] = "SUBMIT_OUTCOME_UNKNOWN"
        elif mismatch == "negative-queued":
            result["ok"] = False
            result["deliveries"][0]["accepted"] = False
            result["deliveries"][0]["queued"] = True
        else:
            return {"version": 1, "id": "unrelated-request", "error": {"code": "UNSUPPORTED_TARGET", "message": "foreign refusal"}}
        return response
    rig.app._response = unrelated
    outcome = rig.adapter.handle_inbound(rig.message)
    assert rig.recipient.pending_messages(_Routes.target.actor_id) == ()
    assert outcome.status == "error" and outcome.ack_error == "SubmitOutcomeUnknownError"
    assert not rig.adapter._state.seen_message(rig.message.message_id)
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id)
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)


@pytest.mark.parametrize("recovery", ["manual", "automatic"])
def test_frozen_request_recovers_without_audit_history_or_platform_body(make_rig, monkeypatch, recovery):
    from hyprial.daemon.impl.adapters.lark.state import store as state_module
    rig = make_rig(phase="before-commit")
    rig.message = replace(rig.message, create_time="1000")
    assert rig.adapter.handle_inbound(rig.message).ack_error == "SubmitOutcomeUnknownError"
    rig.release.set()
    assert rig.settled.wait(3)
    monkeypatch.setattr(state_module, "MAX_DEAD_LETTERS", 1)
    rig.adapter._state.record_dead_letter(DeadLetter("other-native", "other-event", "oc_other", "p2p", "lark:oc_other:oc_other", "ou_other", "user", "other", "no-target", "2026-09-30T00:00:00+00:00"))
    assert [row.message_id for row in rig.adapter.dead_letters()] == ["other-native"]
    assert rig.message.chat_id not in rig.adapter._state.recent_chats()
    rig.platform.get_inbound_message = lambda *_args: None
    rig.platform.list_chat_messages = lambda *_args, **_kwargs: ()
    if recovery == "manual":
        assert rig.adapter.recover_message(rig.message.message_id).status == "forwarded"
    else:
        report = rig.adapter.reconcile_recent(lookback_seconds=1, now_ms=2_000_000_000_000)
        assert report.forwarded == 1
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    assert len(rig.recipient.pending_messages(_Routes.target.actor_id)) == 1


def test_freeze_before_ipc_crash_is_discoverable_without_any_audit(make_rig, monkeypatch):
    rig = make_rig(phase="unblocked")
    def crash(_request): raise KeyboardInterrupt("isolated crash after freeze before IPC")
    with monkeypatch.context() as patch:
        patch.setattr(rig.adapter._harness, "send_request", crash)
        assert rig.adapter.handle_inbound(rig.message).status == "error"
    assert rig.server.requests == []
    assert rig.adapter.dead_letters() == ()
    assert rig.adapter._state.recent_chats() == ()
    rig.platform.get_inbound_message = lambda *_args: None
    rig.platform.list_chat_messages = lambda *_args, **_kwargs: ()
    assert rig.adapter.reconcile_recent().forwarded == 1
    assert len(rig.recipient.pending_messages(_Routes.target.actor_id)) == 1


def _freeze_without_ipc(rig, monkeypatch, messages):
    def crash(_request): raise KeyboardInterrupt("freeze-only crash")
    with monkeypatch.context() as patch:
        patch.setattr(rig.adapter._harness, "send_request", crash)
        for message in messages:
            assert rig.adapter.handle_inbound(message).status == "error"
    assert rig.server.requests == []


def test_pending_replay_keeps_order_and_failure_budget_survives_rebuild(make_rig, monkeypatch):
    from hyprial.daemon.impl.adapters.lark import LarkAdapter, LarkStateStore, HarnessReceipt
    from hyprial.daemon.impl.adapters.lark.inbound import reconcile as adapter_module
    from hyprial.kernel import ipc_errors
    rig = make_rig(phase="unblocked")
    good = replace(rig.message, event_id="evt-good", message_id="om_z_good")
    _freeze_without_ipc(rig, monkeypatch, (rig.message, good))
    calls = []
    class Requests:
        def send_request(self, request, *, timeout=None):
            calls.append(request.message_id)
            assert timeout is not None and 0 < timeout <= adapter_module.PENDING_SUBMISSION_REPLAY_BUDGET_SECONDS
            if request.message_id == rig.message.message_id:
                raise ipc_errors.SubmitOutcomeUnknownError("poison remains unknown")
            key = f"lark-inbound:{request.from_actor_id}:{request.message_id}"
            return HarnessReceipt(str(uuid5(NAMESPACE_URL, f"hyprial:send:{key}:{request.to.actor_id}:0")))
    rig.adapter._harness = Requests()
    monkeypatch.setattr(adapter_module, "PENDING_SUBMISSION_REPLAY_BATCH", 1)
    first = rig.adapter.reconcile_recent()
    assert first.pending_attempted == 1 and first.forwarded == 0
    assert first.pending_remaining == 2
    rig.adapter.close()
    rig.adapter = LarkAdapter(state=LarkStateStore(rig.state_path), lark=rig.platform, harness=Requests(), routes=_Routes(), channel_actor_id="channel:lark:test", org_context_path=rig.root / "no-context")
    second = rig.adapter.reconcile_recent()
    assert second.pending_attempted == 1 and second.forwarded == 0
    assert second.pending_remaining == 2 and second.retryable_errors
    third = rig.adapter.reconcile_recent()
    assert third.dead_lettered == 1 and third.pending_remaining == 1
    fourth = rig.adapter.reconcile_recent()
    assert fourth.forwarded == 1 and fourth.pending_remaining == 0
    assert calls == [rig.message.message_id] * 3 + [good.message_id]
    assert rig.adapter._state.pending_inbound_dead_lettered(rig.message.message_id)
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id)
    assert rig.adapter._state.pending_inbound_submission(good.message_id) is None


def test_recovery_deadline_returns_while_poison_is_held_then_bounded_retries_ungate(make_rig, monkeypatch):
    import threading
    import time
    from hyprial.daemon.impl.adapters.lark import HarnessReceipt
    from hyprial.daemon.impl.adapters.lark.inbound import reconcile as adapter_module
    from hyprial.kernel import ipc_errors
    rig = make_rig(phase="unblocked")
    good = replace(rig.message, event_id="evt-good", message_id="om_z_good")
    _freeze_without_ipc(rig, monkeypatch, (rig.message, good))
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Requests:
        def send_request(self, request, *, timeout=None):
            calls.append(request.message_id)
            if request.message_id == rig.message.message_id:
                entered.set()
                assert release.wait(10)
                raise ipc_errors.SubmitOutcomeUnknownError("still unknown")
            key = f"lark-inbound:{request.from_actor_id}:{request.message_id}"
            return HarnessReceipt(str(uuid5(NAMESPACE_URL, f"hyprial:send:{key}:{request.to.actor_id}:0")))
    rig.adapter._harness = Requests()
    monkeypatch.setattr(adapter_module, "PENDING_SUBMISSION_REPLAY_BATCH", 1)
    monkeypatch.setattr(adapter_module, "PENDING_SUBMISSION_REPLAY_BUDGET_SECONDS", 1.0)
    reports = []
    thread = threading.Thread(target=lambda: reports.append(rig.adapter.reconcile_recent()))
    thread.start()
    try:
        assert entered.wait(3)  # Orchestration slack.
        thread.join(3)
        assert not thread.is_alive() and not release.is_set()
        assert reports[0].pending_remaining == 2
    finally:
        release.set()
        thread.join(3)
    deadline = time.monotonic() + 3
    while rig.adapter._inbound._authority.custody_count() and time.monotonic() < deadline:
        time.sleep(.005)
    assert rig.adapter._inbound._authority.custody_count() == 0
    second = rig.adapter.reconcile_recent()
    assert second.forwarded == 0 and second.retryable_errors
    third = rig.adapter.reconcile_recent()
    assert third.dead_lettered == 1
    assert rig.adapter.reconcile_recent().forwarded == 1
    assert calls == [rig.message.message_id] * 3 + [good.message_id]


def test_frozen_recovery_preserves_chat_retirement_until_fresh_platform_evidence(make_rig, monkeypatch):
    from datetime import UTC, datetime
    rig = make_rig(phase="unblocked")
    _freeze_without_ipc(rig, monkeypatch, (rig.message,))
    rig.adapter._state.retire_chat(rig.message.chat_id, code=230002, now=datetime.now(UTC))
    assert rig.adapter.recover_message(rig.message.message_id).status == "submission-held"
    report = rig.adapter.reconcile_recent()
    assert report.pending_held == 1 and report.pending_remaining == 1
    assert rig.server.requests == []
    assert rig.adapter._state.retired_chat_code(rig.message.chat_id) == 230002
    # An actual new platform event uses the existing membership-evidence path.
    assert rig.adapter.handle_inbound(rig.message).status == "forwarded"
    assert rig.adapter._state.retired_chat_code(rig.message.chat_id) is None
