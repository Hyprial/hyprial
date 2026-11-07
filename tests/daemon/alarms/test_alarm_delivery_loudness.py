"""Acceptance tests: alarms and reports must reach a readable address (2026-09-14).

Spec: hq/notes/spec-alarm-and-report-delivery-to-nowhere-2026-09-14.md
(run-5a7a4fdc5cb8). Every guarantee here has a mutation counterpart named in
the module docstring of the corresponding group; removing the implementation
must turn the matching test red, not merely less assertive.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from hyprial.daemon import Alarm, AlarmEmitter
from hyprial.daemon.impl.dispatch.alarm import DispatchAlarm as _EmitterAlarm
from hyprial.daemon.impl.inbox.actor.worker import DeliveryIoWorker
from hyprial.daemon.impl.inbox.actor.events import DispatchOutcomeKind
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter,
    InboxIoError,
)
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService


class Recorder:
    """Duck-typed Logger: captures events so tests assert on them."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def log(self, level: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, dict(fields)))

    def of(self, event: str) -> list[dict[str, Any]]:
        return [fields for _, name, fields in self.events if name == event]


def _alarm(sender: str, *, text: str | None = None, reason: str = "WORKFLOW_TARGET_TIMEOUT") -> Alarm:
    return Alarm(
        correlation_id="corr-1",
        message_id="msg-1",
        conversation_id="workflow",
        sender=sender,
        recipient="workflow",
        reason=reason,
        audience="agent",
        text=text,
    )


# ── A. the delivery layer never treats a bare name as delivered ────────────
#
# Mutation check (spec 验收 2): reverting _deliver_system_notice /
# _dispatch_alarm to the "unparseable => local delivered" shape makes both
# tests in this group red.


class BareNameDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.recorder = Recorder()
        self.transport = MemoryDeliveryTransport()
        self.service = InboxService(
            Path(self._tmp.name) / "inbox.sqlite3",
            self.transport,
            node_id="node-a",
            logger=self.recorder,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_bare_name_alarm_recipient_fails_loud_and_writes_no_notice(self) -> None:
        emitter = AlarmEmitter(
            self.recorder,  # type: ignore[arg-type]
            deliver_agent=self.service._deliver_system_notice,
            claim=self.service._claim_alarm,
        )
        result = emitter.emit(_alarm("hq-adjutant"))
        assert result.status == "failed"
        # No notice row keyed by a bare name: nobody reads those.
        assert self.service.system_notices("hq-adjutant") == ()
        failures = self.recorder.of("alarm.failed")
        assert failures, "bare-name alarm must record alarm.failed"
        assert failures[0]["sender"] == "hq-adjutant"
        # noticeRecipient/originalRecipient split (spec item 5c).
        assert failures[0]["noticeRecipient"] == "hq-adjutant"
        assert failures[0]["originalRecipient"] == "workflow"

    def test_full_uri_local_alarm_writes_readable_notice_with_text(self) -> None:
        text = "routine sd-lark-flycheck paused (circuit breaker tripped)"
        emitter = AlarmEmitter(
            self.recorder,  # type: ignore[arg-type]
            deliver_agent=self.service._deliver_system_notice,
            claim=self.service._claim_alarm,
        )
        result = emitter.emit(_alarm("agent:o:node-a:hq-adjutant", text=text))
        assert result.status == "delivered"
        notices = self.service.system_notices("agent:o:node-a:hq-adjutant")
        assert len(notices) == 1
        # Text fidelity (spec item 3): the routine name survives to the reader.
        assert "sd-lark-flycheck" in notices[0].payload.decode()
        assert "circuit breaker" in notices[0].payload.decode()

    def test_async_worker_bare_name_is_unroutable_and_carries_no_notice(self) -> None:
        worker = DeliveryIoWorker(
            self.transport,
            lambda *args: None,  # type: ignore[arg-type, return-value]
            lambda *args: True,  # type: ignore[arg-type, return-value]
            node_id="node-a",
        )
        delivered, outcome, notice = worker._dispatch_alarm(_alarm("hq-adjutant"))
        assert delivered is False
        assert outcome is DispatchOutcomeKind.ALARM_UNROUTABLE
        assert notice is None
        assert self.service.system_notices("hq-adjutant") == ()

    def test_async_worker_full_local_uri_still_delivers_locally(self) -> None:
        worker = DeliveryIoWorker(
            self.transport,
            lambda *args: None,  # type: ignore[arg-type, return-value]
            lambda *args: True,  # type: ignore[arg-type, return-value]
            node_id="node-a",
        )
        delivered, outcome, notice = worker._dispatch_alarm(
            _alarm("agent:o:node-a:hq-adjutant")
        )
        assert delivered is True
        assert outcome is DispatchOutcomeKind.ALARM_LOCAL
        assert notice is not None

    def test_two_routines_do_not_share_one_throttle_bucket(self) -> None:
        # B3: the throttle ledger keys on (conversation_id, reason); a shared
        # "workflow" bucket would let routine A's alarm eat routine B's within
        # the same 60s window.  Distinct conversation ids must each deliver.
        emitter = AlarmEmitter(
            self.recorder,  # type: ignore[arg-type]
            deliver_agent=self.service._deliver_system_notice,
            claim=self.service._claim_alarm,
        )

        def emit(conversation_id: str) -> str:
            return emitter.emit(
                Alarm(
                    correlation_id=f"corr-{conversation_id}",
                    message_id=f"msg-{conversation_id}",
                    conversation_id=conversation_id,
                    sender="agent:o:node-a:hq-adjutant",
                    recipient="workflow",
                    reason="ROUTINE_ALARM",
                    audience="agent",
                    text=f"{conversation_id} breaker paused",
                ),
                terminal=False,
            ).status

        assert emit("routine:A") == "delivered"
        assert emit("routine:B") == "delivered"  # distinct bucket, both deliver
        assert emit("routine:A") == "throttled"  # same bucket still throttles


class EmitterAlarmTextFidelityTests(unittest.TestCase):
    """B5: text passthrough must be observable through _EmitterAlarm.escalate,
    the real routine/workflow escalation path — not only by feeding text
    directly to AlarmEmitter (which skips _EmitterAlarm)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.recorder = Recorder()
        self.transport = MemoryDeliveryTransport()
        self.service = InboxService(
            Path(self._tmp.name) / "inbox.sqlite3",
            self.transport,
            node_id="node-a",
            logger=self.recorder,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_escalate_preserves_text_to_the_notice_body(self) -> None:
        # Mutation (B5): setting text=None inside _EmitterAlarm.escalate must
        # redden this — the routine name would vanish from the notice body.
        emitter = AlarmEmitter(
            self.recorder,  # type: ignore[arg-type]
            deliver_agent=self.service._deliver_system_notice,
            claim=self.service._claim_alarm,
        )
        alarm = _EmitterAlarm(emitter, deliver_user=None, logger=self.recorder)
        alarm.escalate(
            to="agent:o:node-a:hq-adjutant",
            text="routine sd-lark-flycheck paused (circuit breaker tripped)",
            reason="ROUTINE_ALARM",
            conversation_id="routine:sd-lark-flycheck",
        )
        notices = self.service.system_notices("agent:o:node-a:hq-adjutant")
        assert len(notices) == 1
        body = notices[0].payload.decode()
        assert "sd-lark-flycheck" in body
        assert "circuit breaker" in body

    def test_user_delivery_failure_preserves_retry_classification(self) -> None:
        emitter = AlarmEmitter(self.recorder)  # type: ignore[arg-type]

        for permanent in (False, True):
            def refuse(
                _recipient: str,
                _text: str,
                _message_id: str,
                *,
                _permanent: bool = permanent,
            ) -> bool:
                raise InboxIoError(
                    "synthetic owner-DM refusal",
                    permanent=_permanent,
                    code=(
                        "TARGET_SQUIRE_UNCONFIGURED"
                        if _permanent
                        else "USER_DELIVERY_TIMEOUT"
                    ),
                )

            result = _EmitterAlarm(
                emitter, deliver_user=refuse, logger=self.recorder
            ).escalate(
                to="user:allen",
                text="routine failed",
                reason="ROUTINE_ALARM",
                conversation_id="routine:one",
                idempotency_key=f"alarm-{permanent}",
            )

            assert result.status == "failed"
            assert result.permanent is permanent

    def test_only_terminal_refusal_codes_retire_a_routine_alarm(self) -> None:
        """The owner-DM path raises most rejections as permanent; only codes
        that can never succeed may retire the alarm, the rest keep retrying."""

        emitter = AlarmEmitter(self.recorder)  # type: ignore[arg-type]
        cases = {
            "TARGET_SQUIRE_UNCONFIGURED": True,
            "INVALID_ARGUMENT": True,
            "SENDER_UNRESOLVED": True,
            "USER_DELIVERY_OVERLOADED": False,
            "USER_DELIVERY_FAILED": False,
            "TARGET_SQUIRE_ADAPTER_UNAVAILABLE": False,
            "DAEMON_UNAVAILABLE": False,
            "IDENTITY_CONFLICT": False,
        }
        for code, retired in cases.items():
            def refuse(
                _recipient: str, _text: str, _message_id: str, *, _code: str = code
            ) -> bool:
                raise InboxIoError("synthetic rejection", permanent=True, code=_code)

            result = _EmitterAlarm(
                emitter, deliver_user=refuse, logger=self.recorder
            ).escalate(
                to="user:allen",
                text="routine failed",
                reason="ROUTINE_ALARM",
                conversation_id="routine:one",
                idempotency_key=f"alarm-{code}",
            )

            assert result.status == "failed", code
            assert result.permanent is retired, code


# ── B. user: recipients split to the owner-DM path at the delivery layer ───
#
# Mutation check (spec 验收 4): routing user: back through the inbox message
# path (dropping the split in InboxDeliveryIoAdapter.deliver) makes every
# test in this group red.


class _ExplodingSink:
    """Command sink that fails the test if the adapter submits an inbox
    message — a user:-addressed inbox message is exactly the doomed DLQ row
    this change forbids."""

    def __init__(self) -> None:
        self.submitted: list[object] = []

    def submit(self, command: object) -> Any:
        self.submitted.append(command)
        raise AssertionError("user: delivery must not submit an inbox message")


class UserDeliverySplitTests(unittest.TestCase):
    def _adapter(self, deliver_user: Any) -> tuple[InboxDeliveryIoAdapter, _ExplodingSink]:
        sink = _ExplodingSink()
        adapter = InboxDeliveryIoAdapter(
            sink,  # type: ignore[arg-type]
            CorrelatedInboxEventRouter(),
            resolve_target=lambda target: target,
            clock_ms=lambda: 1_000,
            completion_timeout=0.2,
            deliver_user=deliver_user,
        )
        return adapter, sink

    def _deliver(self, adapter: InboxDeliveryIoAdapter) -> Any:
        return adapter.deliver(
            effect_id="report-effect-1",
            sender="agent:o:node-a:workflow",
            target="user:allen",
            conversation_id="run-x-report",
            text="PAC report: all targets done",
            summary="PAC report: all targets done",
        )

    def test_user_recipient_rides_dm_path_without_inbox_message(self) -> None:
        seen: list[tuple[str, str, str, str]] = []

        def deliver_user(
            recipient: str, text: str, message_id: str, sender: str
        ) -> bool:
            seen.append((recipient, text, message_id, sender))
            return True

        adapter, sink = self._adapter(deliver_user)
        result = self._deliver(adapter)
        assert seen == [
            (
                "user:allen",
                "PAC report: all targets done",
                result.message_id,
                "agent:o:node-a:workflow",
            )
        ]
        assert result.recipient == "user:allen"
        assert sink.submitted == []

    def test_unwired_dm_path_fails_permanent_without_inbox_message(self) -> None:
        adapter, sink = self._adapter(None)
        with self.assertRaises(InboxIoError) as ctx:
            self._deliver(adapter)
        assert ctx.exception.permanent is True
        assert "no owner-DM route" in str(ctx.exception)
        assert sink.submitted == []

    def test_rejected_dm_path_fails_permanent_without_inbox_message(self) -> None:
        adapter, sink = self._adapter(lambda *_: False)
        with self.assertRaises(InboxIoError) as ctx:
            self._deliver(adapter)
        assert ctx.exception.permanent is True
        assert "rejected" in str(ctx.exception)
        assert sink.submitted == []


# ── C. outbox retry and DLQ write log events ───────────────────────────────
#
# Mutation check (spec 验收 5): removing _log_retry/_log_dlq calls leaves the
# rows in SQLite but turns the event assertions red.


class OutboxObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.recorder = Recorder()
        self.transport = MemoryDeliveryTransport()
        self.service = InboxService(
            Path(self._tmp.name) / "inbox.sqlite3",
            self.transport,
            node_id="node-a",
            logger=self.recorder,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_offline_ttl_logs_terminal_without_retry_events(self) -> None:
        from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage

        message = InboxMessage(
            message_id="m-obs-1",
            conversation_id="conv",
            sender="agent:o:node-a:runner",
            recipient="agent:o:node-a:ghost",
            payload=b'{"message":"hi"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=0,
        )
        self.service.submit(message, now_ms=0)
        while self.service.outbox_count():
            row = self.service.outbox_item("m-obs-1")
            self.service.retry_due(now_ms=int(row.next_attempt_ms))
        retries = self.recorder.of("outbox.retry_scheduled")
        assert retries == []
        terminal = self.recorder.of("outbox.terminal_failed")
        assert terminal, "TTL expiry must log outbox.terminal_failed"
        assert terminal[0]["messageId"] == "m-obs-1"
        assert terminal[0]["recipient"] == "agent:o:node-a:ghost"
        assert terminal[0]["reason"] == "TTL_EXPIRED"
        assert "attempts" in terminal[0]


if __name__ == "__main__":
    unittest.main()
