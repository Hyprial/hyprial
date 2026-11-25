from __future__ import annotations

import asyncio

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    CustodyReceipt,
    HydratedEvent,
    NativeSubmissionOutcome,
)

from .support import FakeWorker, envelope, instance, native_event, operation, target


class _Sink:
    def __init__(self) -> None:
        self.units = []

    async def stage(self, unit):
        self.units.append(unit)
        return CustodyReceipt("journal-1", "e" * 64, 2000)


def _runtime(worker: FakeWorker):
    from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppRuntime

    return WhatsAppRuntime(
        worker,
        instance=instance(),
        account_jid="15550001@s.whatsapp.net",
        artifact=ArtifactStatus.available_candidate("tryx-test-build"),
    )


def test_callback_custody_cursor_hydration_history_resolution_and_close_are_worker_backed() -> None:
    async def scenario() -> None:
        worker = FakeWorker()
        worker.add("runtime.start", {"sessionId": "session-1", "ingressMode": "pull"})
        worker.add("runtime.commit-checkpoint", {"committed": True})
        worker.add("runtime.hydrate", {"event": native_event()})
        worker.add(
            "runtime.history",
            {"units": [{"event": native_event(message_id="wamid-history"), "cursor": "cursor-h"}]},
        )
        worker.add(
            "runtime.resolve",
            {"status": "resolved", "chatJid": "120363000000@g.us"},
        )
        worker.add("runtime.close", {"state": "closed", "retainedIngress": 0, "retainedDeliveries": 0})
        runtime = _runtime(worker)
        sink = _Sink()
        session = await runtime.start(instance(), sink)
        assert session.session_id == "session-1"

        callback = {"type": "event", "event": native_event(), "cursor": "cursor-1"}
        assert runtime.accept_native_callback(callback) is True
        unit = await runtime.next_ingress()
        assert unit.envelope.payload == envelope().payload
        assert unit.checkpoint.token == "cursor-1"
        message_alias = unit.envelope.observation.aliases[0]
        assert (
            message_alias.platform,
            message_alias.account_scope,
            message_alias.chat_scope,
            message_alias.normalized_id,
        ) == (
            "whatsapp",
            "15550001@s.whatsapp.net",
            "120363000000@g.us",
            "wamid-1",
        )
        custody = await sink.stage(unit)
        await runtime.commit_checkpoint(unit.checkpoint, custody)
        hydrated = await runtime.hydrate(unit.envelope)
        assert isinstance(hydrated, HydratedEvent)
        history = await runtime.history(None, 10)
        assert history[0].checkpoint.token == "cursor-h"
        resolved = await runtime.resolve(target())
        assert resolved.status == "resolved"
        closed = await runtime.close(5000)
        assert closed.state == "closed"
        assert worker.operations() == [
            "runtime.start",
            "runtime.commit-checkpoint",
            "runtime.hydrate",
            "runtime.history",
            "runtime.resolve",
            "runtime.close",
        ]

    asyncio.run(scenario())


def test_unknown_submit_is_never_resent_and_only_reconcile_can_advance_it() -> None:
    async def scenario() -> None:
        worker = FakeWorker()
        worker.add(
            "runtime.submit",
            {"parts": [{"partIndex": 0, "state": "unknown", "providerMessageId": None, "definitelyNotSent": False, "reasonCode": "WHATSAPP_SUBMIT_OUTCOME_UNKNOWN"}]},
        )
        worker.add(
            "runtime.reconcile",
            {"parts": [{"partIndex": 0, "state": "accepted", "providerMessageId": "wamid-out", "definitelyNotSent": False, "reasonCode": None}]},
        )
        runtime = _runtime(worker)
        node = operation()
        first = await runtime.submit(node)
        assert isinstance(first, NativeSubmissionOutcome)
        assert first.submissions[0].state == "unknown"
        with pytest.raises(Exception) as raised:
            await runtime.submit(node)
        assert getattr(raised.value, "code", None) == "WHATSAPP_RECONCILE_REQUIRED"
        assert worker.operations() == ["runtime.submit"]
        reconciled = await runtime.reconcile(node, first)
        assert reconciled.submissions[0].state == "accepted"
        assert worker.operations() == ["runtime.submit", "runtime.reconcile"]

    asyncio.run(scenario())


def test_partial_submission_crash_and_revoke_keep_unknown_custody_blocked() -> None:
    async def scenario() -> None:
        worker = FakeWorker()
        worker.add(
            "runtime.submit",
            {
                "parts": [
                    {"partIndex": 0, "state": "accepted", "providerMessageId": "wamid-ok", "definitelyNotSent": False, "reasonCode": None},
                    {"partIndex": 1, "state": "unknown", "providerMessageId": None, "definitelyNotSent": False, "reasonCode": "WHATSAPP_TIMEOUT_UNKNOWN"},
                    {"partIndex": 2, "state": "failed-final", "providerMessageId": None, "definitelyNotSent": True, "reasonCode": "WHATSAPP_MEDIA_REJECTED"},
                ]
            },
        )
        runtime = _runtime(worker)
        outcome = await runtime.submit(operation())
        assert [part.state for part in outcome.submissions] == ["accepted", "unknown", "failed-final"]
        runtime.accept_native_callback({"type": "crash", "reasonCode": "WHATSAPP_NATIVE_CRASH"})
        health = await runtime.health()
        assert health.state == "blocked"
        assert health.unknown_count == 1
        assert "WHATSAPP_NATIVE_CRASH" in health.reason_codes
        runtime.revoke("claim-revoked")
        with pytest.raises(Exception) as raised:
            await runtime.reconcile(operation(), outcome)
        assert getattr(raised.value, "code", None) == "WHATSAPP_RUNTIME_REVOKED"
        assert worker.operations() == ["runtime.submit", "runtime.revoke"]

    asyncio.run(scenario())


def test_stale_generation_callback_is_rejected_without_queue_or_cursor_advance() -> None:
    async def scenario() -> None:
        runtime = _runtime(FakeWorker())
        payload = {"type": "event", "event": native_event(), "cursor": "cursor-1", "generation": 6}
        assert runtime.accept_native_callback(payload) is False
        assert runtime.pending_ingress == 0

    asyncio.run(scenario())
