from __future__ import annotations

import json
from pathlib import Path
import pytest
import hyprial.daemon.impl.application as daemon_application
from hyprial.kernel import CHANNEL_LIVENESS_TTL_SECONDS
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxAuthorityUnavailable, SubmissionResult

from ..support import (
    CANONICAL,
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    pending_message,
    register,
    system_notice,
)


def test_interactive_turn_reports_are_fenced_deduplicated_and_reset(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    registration = {
        "actor": "alice",
        "cwd": "/work/alice",
        "command": ["claude"],
        "source": "claude-channel",
        "runtime": "claude_interactive",
        "channelConfirmed": True,
        "turnReporting": True,
    }
    app.handle("session.register", {**registration, "sessionRef": "session-1"})

    first = app.handle(
        "session.turn.ended",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "reportIds": ["turn-a", "turn-b"],
        },
    )
    duplicate = app.handle(
        "session.turn.ended",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "reportIds": ["turn-b"],
        },
    )

    assert first["turnCount"] == 2
    assert isinstance(first["lastTurnEndedAtMs"], int)
    assert duplicate["turnCount"] == 2
    assert duplicate["lastTurnEndedAtMs"] == first["lastTurnEndedAtMs"]

    app.handle("session.register", {**registration, "sessionRef": "session-2"})
    with pytest.raises(DaemonRequestError) as stale:
        app.handle(
            "session.turn.ended",
            {
                "actor": "alice",
                "sessionRef": "session-1",
                "reportIds": ["turn-c"],
            },
        )
    assert stale.value.code == "SESSION_SUPERSEDED"

    reset = app.handle(
        "session.turn.ended",
        {
            "actor": "alice",
            "sessionRef": "session-2",
            "reportIds": ["turn-d"],
        },
    )
    assert reset["turnCount"] == 1


def test_system_notice_stays_listed_until_its_own_ack_without_fifo_settlement(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message(), (system_notice(),))
    app = application(tmp_path, inbox)
    register(app)

    first = app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-1"}
    )
    second = app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-1"}
    )

    assert [item["intent"] for item in first["messages"]] == ["system", "request"]
    assert first["messages"][0]["messageId"] == "notice-1"
    # A notice has no body to fetch: its whole text is the summary and its
    # bodyBytes is zero (WP3-B: the pending row no longer carries `message`).
    assert first["messages"][0]["kind"] == "notice"
    assert first["messages"][0]["bodyBytes"] == 0
    assert "original-1" in first["messages"][0]["summary"]
    # Observation is a doorbell, never a drain: the same notice comes back on
    # every list until the caller acks that one id (design: notice peek).
    assert [item["intent"] for item in second["messages"]] == ["system", "request"]
    assert second["messages"][0]["messageId"] == "notice-1"
    assert inbox.acks == []


def test_different_text_retry_reports_already_submitted_without_ack_while_pending(
    tmp_path: Path,
) -> None:
    class PendingThenConflictInbox(FakeInbox):
        def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
            if self.submitted:
                raise InboxAuthorityUnavailable(
                    "SUBMISSION_RECEIPT_CONFLICT: correlation_id is already "
                    "settled for a different inbox command"
                )
            self.submitted.append(message)
            return SubmissionResult(message.message_id, accepted=True, queued=True)

        def reply_submission_result(self, message_id: str) -> SubmissionResult:
            return SubmissionResult(message_id, accepted=True, queued=True)

    inbox = PendingThenConflictInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)

    first = app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "first answer",
            "summary": "first answer",
        },
    )
    second = app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "different answer",
            "summary": "different answer",
        },
    )

    assert first["code"] == "REPLY_SETTLEMENT_PENDING"
    assert second == {
        "ok": False,
        "messageId": "message-1",
        "replyMessageId": "c4ff4b95-7cef-54b3-a843-1643bc36c56d",
        "replied": False,
        "acknowledged": False,
        "queued": True,
        "code": "REPLY_ALREADY_SUBMITTED",
        "retryable": False,
        "settled": False,
    }
    assert len(inbox.submitted) == 1
    assert inbox.acks == []
    assert inbox.pending_messages(CANONICAL) != ()


def test_targets_preserve_canonical_interactive_actor_uri(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Presence:
        def online_actors(self) -> tuple[str, ...]:
            return ("agent:owner:host:hq-adjutant",)

    app._presence = Presence()  # type: ignore[assignment]

    result = app.handle("targets", {})

    assert result["targets"] == [
        {
            "targetKind": "agent",
            "targetUri": "agent:owner:host:hq-adjutant",
            "actor": "agent:owner:host:hq-adjutant",
            "status": "online",
            "deliverable": True,
        }
    ]


def test_ps_exposes_unknown_orphan_process_for_operator_retry(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    orphan = {
        "orphanId": "worker:4242:birth",
        "harnessId": "worker",
        "state": "unknown",
        "observed": True,
        "pid": 4242,
        "marker": "birth",
        "gcAttempts": 3,
        "unknownForMs": 61_000,
        "unknownOverThreshold": True,
    }

    class Providers:
        def status(self) -> tuple[()]:
            return ()

        def orphan_status(self) -> tuple[dict[str, object], ...]:
            return (orphan,)

    app._harnesses = Providers()  # type: ignore[assignment]

    assert app.handle("ps", {})["orphanProcesses"] == [orphan]


def test_ps_exposes_interactive_channel_registration(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    app.handle(
        "session.register",
        {
            "actor": "worker-node",
            "sessionRef": "session-1",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": "lease-token-that-is-long-enough-0001",
        },
    )

    status = app.handle("ps", {})
    observed_age = status["interactiveSessions"][0].pop(
        "channelLastObservedAgeMs"
    )
    assert isinstance(observed_age, int) and observed_age >= 0

    assert status["interactiveSessions"] == [
        {
            "actor": "agent:op:node-1:worker-node",
            "sessionRef": "session-1",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "online": True,
            "channelConfirmed": True,
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "channelBuildStale": True,
            "ownerFence": True,
            "channelRegistered": True,
            "channelGeneration": "current",
            "channelCurrentThisGeneration": True,
            "channelCurrentEpoch": app.epoch,
            "channelRecentlyObserved": True,
            "channelAlive": True,
            "channelLiveness": "alive",
        }
    ]


def test_persisted_channel_is_not_current_until_guarded_refresh(
    tmp_path: Path,
) -> None:
    first = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    first._harnesses = Providers()  # type: ignore[assignment]
    params = {
        "actor": "worker-node",
        "sessionRef": "session-1",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        "runtime": "claude_interactive",
        "channelConfirmed": True,
        "channelBuildVersion": "0.4.1.dev0",
        "channelProtocolVersion": 2,
        "ownerFence": True,
        "channelLeaseToken": "lease-token-that-is-long-enough-0002",
    }
    registered = first.handle("session.register", params)
    assert registered["channelCurrentThisGeneration"] is True
    assert registered["channelCurrentEpoch"] == first.epoch

    second = application(tmp_path, FakeInbox(pending_message()))
    second._harnesses = Providers()  # type: ignore[assignment]
    assert second.epoch != first.epoch

    restored = second.handle("ps", {})["interactiveSessions"][0]
    assert restored["channelCurrentThisGeneration"] is False
    assert restored["channelCurrentEpoch"] is None
    stale_identity = second.handle(
        "identity.whoami", {"actor": "worker-node", "sessionRef": "session-1"}
    )
    assert stale_identity["sessionRegistered"] is True
    assert stale_identity["channelCurrentThisGeneration"] is False
    assert stale_identity["channelCurrentEpoch"] is None
    assert stale_identity["daemonEpoch"] == second.epoch

    # A normal IPC/tool call with matching identity cannot mark the persisted
    # Channel current in this daemon generation.
    second._response(json.dumps({
        "version": 1,
        "id": "ordinary-ipc",
        "method": "identity.whoami",
        "params": {"actor": "worker-node", "sessionRef": "session-1"},
    }).encode())
    assert second.handle("ps", {})["interactiveSessions"][0][
        "channelCurrentThisGeneration"
    ] is False

    refresh_response = second._response(json.dumps({
        "version": 1,
        "id": "channel-refresh",
        "method": "session.refresh",
        "params": {
            "actor": "worker-node",
            "sessionRef": "session-1",
            "channelLeaseToken": "lease-token-that-is-long-enough-0002",
        },
    }).encode())
    assert "error" not in refresh_response
    current = second.handle("ps", {})["interactiveSessions"][0]
    assert current["channelCurrentThisGeneration"] is True
    assert current["channelCurrentEpoch"] == second.epoch


def test_operational_channel_lease_expires_without_losing_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(daemon_application.time, "monotonic", lambda: now)
    app = application(tmp_path, FakeInbox(pending_message()))
    token = "lease-token-that-is-long-enough-0003"
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": token,
        },
    )

    fresh = app.handle("ps", {})["interactiveSessions"][0]
    assert fresh["channelRegistered"] is True
    assert fresh["channelCurrentThisGeneration"] is True
    assert fresh["channelRecentlyObserved"] is True
    assert fresh["channelAlive"] is True
    assert "channelLeaseToken" not in fresh
    assert "channelLeaseDigest" not in fresh

    now += CHANNEL_LIVENESS_TTL_SECONDS + 0.1
    # Identity and inbox IPC know actor/sessionRef, but possess no unexported
    # lease marker and therefore cannot renew liveness as a side effect. This
    # is accidental-misuse isolation inside a trusted same-user boundary.
    app.handle(
        "identity.whoami", {"actor": "alice", "sessionRef": "session-live"}
    )
    app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-live"}
    )
    stale = app.handle("ps", {})["interactiveSessions"][0]
    assert stale["channelRegistered"] is True
    assert stale["channelCurrentThisGeneration"] is True
    assert stale["channelRecentlyObserved"] is False
    assert stale["channelAlive"] is False
    assert stale["channelLiveness"] == "stale"

    with pytest.raises(DaemonRequestError) as rejected:
        app.handle(
            "session.heartbeat",
            {
                "actor": "alice",
                "sessionRef": "session-live",
                "channelLeaseToken": "wrong-token-that-is-long-enough-0000",
            },
        )
    assert rejected.value.code == "INVALID_CHANNEL_LEASE"
    assert app.handle("ps", {})["interactiveSessions"][0]["channelAlive"] is False

    renewed = app.handle(
        "session.heartbeat",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "channelLeaseToken": token,
        },
    )
    assert renewed["alive"] is True
    assert app.handle("ps", {})["interactiveSessions"][0]["channelAlive"] is True


def test_expired_operational_channel_withdraws_route_but_keeps_offline_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(daemon_application.time, "monotonic", lambda: now)
    app = application(tmp_path, FakeInbox(pending_message()))
    token = "lease-token-that-is-long-enough-0005"
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": token,
        },
    )

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    endpoint = Resource()
    liveliness = Resource()

    class Routes:
        active = True

        def drop(self, _actor: str, *, owner_lease: str | None = None):  # type: ignore[no-untyped-def]
            del owner_lease
            self.active = False
            endpoint.close()
            liveliness.close()
            return type("Completion", (), {"changed": True})()

        def owners(self, _actor: str) -> tuple[str, ...]:
            return ("session:session-live",) if self.active else ()

    routes = Routes()
    app._routes = routes  # type: ignore[assignment]

    class Presence:
        def online_actors(self) -> tuple[str, ...]:
            return (CANONICAL,) if routes.active else ()

    app._presence = Presence()  # type: ignore[assignment]
    assert app.handle("targets", {})["targets"][0]["actor"] == CANONICAL

    now += CHANNEL_LIVENESS_TTL_SECONDS + 0.1
    # Status reads are projection-only (f4a9e5f16); route expiry is owned by
    # the maintenance tick (messaging/maintenance.py), so run that step here.
    app._expire_stale_channel_routes(now)
    assert app.handle("targets", {})["targets"] == [
        {
            "targetKind": "agent",
            "targetUri": CANONICAL,
            "actor": CANONICAL,
            "status": "offline",
            "deliverable": True,
            "hosted": False,
            "hostedBy": None,
        }
    ]
    assert endpoint.closed is True
    assert liveliness.closed is True


def test_daemon_restore_does_not_publish_persisted_channel_as_online(
    tmp_path: Path,
) -> None:
    first = application(tmp_path, FakeInbox(pending_message()))
    first.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": "lease-token-that-is-long-enough-0006",
        },
    )
    second = application(tmp_path, FakeInbox(pending_message()))
    restored: list[str] = []
    second._ensure_interactive_route = (  # type: ignore[method-assign]
        lambda actor: restored.append(actor) or True
    )

    second._restore_interactive_routes()

    assert restored == []


def test_guarded_refresh_cannot_steal_from_superseding_channel(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    base = {
        "actor": "alice",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        "runtime": "claude_interactive",
    }
    app.handle("session.register", {**base, "sessionRef": "old-session"})
    app.handle("session.register", {**base, "sessionRef": "new-session"})

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "session.refresh",
            {"actor": "alice", "sessionRef": "old-session"},
        )

    assert failure.value.code == "SESSION_SUPERSEDED"
    persisted = app.desired_state.load().interactive_sessions[0]
    assert persisted.session_ref == "new-session"

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    status = app.handle("ps", {})["interactiveSessions"][0]
    assert status["sessionRef"] == "new-session"
    assert status["channelCurrentThisGeneration"] is True
    assert status["channelCurrentEpoch"] == app.epoch


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("channelBuildVersion", "not a version", "safe package-version"),
        ("channelProtocolVersion", False, "positive integer"),
        ("ownerFence", 1, "boolean"),
    ],
)
def test_registration_rejects_unsafe_channel_telemetry(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    params: dict[str, object] = {
        "actor": "worker-node",
        "sessionRef": "session-1",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        field: value,
    }

    with pytest.raises(DaemonRequestError, match=match):
        app.handle("session.register", params)
