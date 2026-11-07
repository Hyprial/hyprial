"""HQ review 1142 regressions for owner-local agent-bot delivery."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark import LarkApiError
from hyprial.daemon.impl.adapters.lark.outbound.gateway import (
    GatewayAdmissionRefused,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.agent_bot_attempts import (
    ATTEMPT_ACCEPTED,
    ATTEMPT_DEFINITELY_NOT_SENT,
    ATTEMPT_IN_FLIGHT,
    ATTEMPT_UNCERTAIN,
    AgentBotAttemptLedger,
)
from hyprial.daemon.impl.squire import UserDeliveryResult
from hyprial.kernel import DaemonRequestError

from .test_agent_bot_dm import (
    ADAPTER,
    SENDER,
    SESSION,
    _close_test_applications as _close_test_applications,
    _eligible_app,
    _Gateway,
    _seed_account,
    _send,
)


def _send_to(app: Any, target: str, *, operation: str) -> dict[str, object]:
    return app.handle(
        "message.send",
        {
            "from": SENDER,
            "sessionRef": SESSION,
            "to": [target],
            "summary": "agent report",
            "message": "agent report",
            "idempotencyKey": operation,
        },
    )


def test_offline_fallback_is_sticky_when_adapter_comes_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(
        tmp_path, monkeypatch, gateway=gateway, online=False
    )

    first = _send(app, operation="sticky-offline")
    app._adapters.online = True
    replay = _send(app, operation="sticky-offline")

    assert first["ok"] is True
    assert replay["ok"] is True
    assert len(fallback.requests) == 2
    assert gateway.calls == []
    attempt = app._agent_bot_attempts.get("sticky-offline:user:alice")
    assert attempt is not None
    assert attempt.reason == "adapter-not-running"


def test_unknown_proxy_outcome_is_sticky_when_adapter_comes_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(
        tmp_path, monkeypatch, gateway=gateway, online=False
    )
    fallback.result = UserDeliveryResult(
        message_id="proxy-unknown",
        accepted=False,
        code="SUBMIT_OUTCOME_UNKNOWN",
        message="proxy submission remains uncertain",
    )

    with pytest.raises(DaemonRequestError) as first:
        _send(app, operation="sticky-proxy-unknown")
    app._adapters.online = True
    with pytest.raises(DaemonRequestError) as replay:
        _send(app, operation="sticky-proxy-unknown")

    assert first.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert replay.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(fallback.requests) == 2
    assert gateway.calls == []


def test_unresolved_open_id_fallback_is_sticky_after_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(
        tmp_path, monkeypatch, gateway=gateway, seed_open_id=False
    )

    first = _send(app, operation="sticky-open-id")
    _seed_account(app, adapter=ADAPTER, open_id="ou_bound_later")
    replay = _send(app, operation="sticky-open-id")

    assert first["ok"] is True
    assert replay["ok"] is True
    assert len(fallback.requests) == 2
    assert gateway.calls == []


@pytest.mark.parametrize(
    "retry_error",
    [
        LarkApiError("send owner DM", 99991400, "too many requests"),
        LarkApiError(
            "send owner DM", 230013, "Bot has NO availability to this user"
        ),
        GatewayAdmissionRefused("overloaded"),
    ],
    ids=["rate-limit", "unreachable", "gateway-overloaded"],
)
def test_locked_attempt_is_never_downgraded_by_a_later_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_error: BaseException,
) -> None:
    gateway = _Gateway(TimeoutError("post recorded, reply timed out"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    first = _send(app, operation="locked-refusal")
    gateway.error = retry_error
    replay = _send(app, operation="locked-refusal")

    assert first["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(gateway.posts) == 1
    assert fallback.requests == []
    assert app._agent_bot_attempts.get(
        "locked-refusal:user:alice"
    ).state == ATTEMPT_UNCERTAIN


def test_uncertain_attempt_older_than_lark_dedupe_window_is_not_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [10_000]
    gateway = _Gateway(TimeoutError("post recorded, reply timed out"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    app._agent_bot_attempts._clock_ms = lambda: now[0]

    _send(app, operation="expired-dedupe")
    now[0] += 60 * 60 * 1000 + 1
    gateway.error = None
    replay = _send(app, operation="expired-dedupe")

    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_non_owner_user_target_uses_receiver_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    monkeypatch.setattr(
        app._identity_resolver,
        "owner_open_id",
        lambda _owner, _adapter: "ou_non_owner",
    )

    result = _send_to(app, "user:bob", operation="non-owner")

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert fallback.requests[0].owner == "bob"
    assert gateway.calls == []
    assert "via" not in result["deliveries"][0]


def test_ipc_agent_claim_without_session_cannot_use_agent_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = app._ipc_message_send(
        "message.send",
        {
            "from": SENDER,
            "to": ["user:alice"],
            "summary": "spoofed agent report",
            "message": "spoofed agent report",
            "idempotencyKey": "ipc-spoof",
        },
        "ipc",
    )

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert gateway.calls == []


def test_explicit_in_process_daemon_origin_can_use_agent_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = app._ipc_message_send(
        "message.send",
        {
            "from": SENDER,
            "to": ["user:alice"],
            "summary": "trusted daemon report",
            "message": "trusted daemon report",
            "idempotencyKey": "trusted-daemon",
        },
        "daemon",
    )

    assert result["deliveries"][0]["via"] == "agent-bot"
    assert fallback.requests == []
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("mutation", ["delegated", "remote-machine", "other-owner"])
def test_sender_authority_mutations_remain_ineligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    agent = app.agents.require("worker")
    sender = SENDER
    subject = SENDER
    if mutation == "delegated":
        subject = "agent:alice:sender-mac:delegate"
    elif mutation == "remote-machine":
        agent = replace(agent, machine="remote-mac")
        sender = subject = agent.uri
    else:
        agent = replace(agent, owner="mallory")
        sender = subject = agent.uri
    monkeypatch.setattr(app.agents, "get", lambda _actor: agent)
    monkeypatch.setattr(
        app,
        "_authenticated_message_caller",
        lambda *_args, **_kwargs: SimpleNamespace(
            sender=sender,
            subject=subject,
            via="daemon",
            on_behalf_of=subject if subject != sender else None,
        ),
    )

    result = app._ipc_message_send(
        "message.send",
        {
            "from": sender,
            "to": ["user:alice"],
            "summary": "mutated authority",
            "message": "mutated authority",
            "idempotencyKey": f"authority-{mutation}",
        },
        "daemon",
    )

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert gateway.calls == []


def test_fresh_gateway_admission_refusal_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(GatewayAdmissionRefused("closed"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = _send(app, operation="fresh-gateway-refusal")

    assert result["ok"] is True
    assert len(fallback.requests) == 1


def test_accepted_send_survives_ledger_settle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    original_settle = app._agent_bot_attempts.settle

    def settle(*args: object, **kwargs: object) -> object:
        if kwargs.get("state") == ATTEMPT_ACCEPTED:
            raise OSError("simulated accepted-settle failure")
        return original_settle(*args, **kwargs)

    monkeypatch.setattr(app._agent_bot_attempts, "settle", settle)

    result = _send(app, operation="settle-failure")

    assert result["deliveries"][0]["accepted"] is True
    assert result["deliveries"][0]["nativeMessageId"] == "om-1"
    assert fallback.requests == []
    assert any(
        event == "user-delivery.agent-bot-settle-failed" for event, _fields in events
    )


class _BoundedGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_owner_dm(
        self, _open_id: str, _text: str, *, idempotency_key: str
    ) -> str:
        self.calls.append(idempotency_key)
        return "om-bounded"

    def send_owner_dm_settled(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("unbounded settled gateway path was used")


def test_agent_bot_uses_gateway_authoritys_bounded_ipc_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _BoundedGateway()
    app, fallback, _events = _eligible_app(
        tmp_path, monkeypatch, gateway=gateway  # type: ignore[arg-type]
    )

    result = _send(app, operation="bounded-gateway")

    assert result["deliveries"][0]["nativeMessageId"] == "om-bounded"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


class _ConcurrentGateway:
    def __init__(self) -> None:
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.second_posted = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def send_owner_dm(
        self, _open_id: str, _text: str, *, idempotency_key: str
    ) -> str:
        with self._lock:
            self.calls += 1
            number = self.calls
        if number == 1:
            self.first_entered.set()
            assert self.release_first.wait(2)
        else:
            self.second_posted.set()
        return "om-concurrent"


def test_concurrent_same_key_posts_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _ConcurrentGateway()
    app, fallback, _events = _eligible_app(
        tmp_path, monkeypatch, gateway=gateway  # type: ignore[arg-type]
    )
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def send() -> None:
        try:
            results.append(_send(app, operation="concurrent-key"))
        except BaseException as error:  # pragma: no cover - assertion reports it
            errors.append(error)

    first = threading.Thread(target=send)
    second = threading.Thread(target=send)
    first.start()
    assert gateway.first_entered.wait(1)
    second.start()
    assert not gateway.second_posted.wait(0.2)
    gateway.release_first.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert not first.is_alive() and not second.is_alive()
    assert gateway.calls == 1
    assert len(results) == 2
    assert fallback.requests == []


def test_count_eviction_preserves_inflight_and_uncertain_records(
    tmp_path: Path,
) -> None:
    now = [100]
    ledger = AgentBotAttemptLedger(
        tmp_path / "bounded-ledger.json", max_entries=2, clock_ms=lambda: now[0]
    )
    ledger.begin("locked", adapter="bot", lark_idempotency_key="locked-key")
    now[0] += 1
    ledger.settle(
        "uncertain",
        adapter="bot",
        lark_idempotency_key="uncertain-key",
        state=ATTEMPT_UNCERTAIN,
    )
    now[0] += 1
    ledger.settle(
        "accepted",
        adapter="bot",
        lark_idempotency_key="accepted-key",
        state=ATTEMPT_ACCEPTED,
        delivery={"accepted": True},
    )

    # No row here locks nothing, so none is evicted: the bound gives way to
    # duplicate safety and age retention still trims the file (review 1148 N3).
    assert ledger.get("locked").state == ATTEMPT_IN_FLIGHT
    assert ledger.get("uncertain").state == ATTEMPT_UNCERTAIN
    assert ledger.get("accepted").state == ATTEMPT_ACCEPTED


# -- review 1148 ---------------------------------------------------------------


def _ipc_replay(app: Any, *, operation: str) -> dict[str, object]:
    """The same idempotencyKey replayed over IPC without a sessionRef."""

    return app._ipc_message_send(
        "message.send",
        {
            "from": SENDER,
            "to": ["user:alice"],
            "summary": "agent report",
            "message": "agent report",
            "idempotencyKey": operation,
        },
        "ipc",
    )


def test_accepted_bot_send_then_ineligible_replay_is_a_duplicate_not_a_proxy_dm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    first = _send(app, operation="n1-accepted")
    replay = _ipc_replay(app, operation="n1-accepted")

    assert first["deliveries"][0]["via"] == "agent-bot"
    assert replay["deliveries"][0]["duplicate"] is True
    assert replay["deliveries"][0]["nativeMessageId"] == first["deliveries"][0]["nativeMessageId"]
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_uncertain_bot_send_then_ineligible_replay_never_reaches_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(TimeoutError("post recorded, reply timed out"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    _send(app, operation="n1-uncertain")
    gateway.error = None
    replay = _ipc_replay(app, operation="n1-uncertain")

    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_ineligible_sender_with_unreadable_bot_ledger_keeps_the_receiver_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken bot ledger must never silence system alarms: a system-service
    sender (no agent identity, so it can own no bot record) still reaches the
    receiver path."""

    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    path = app._agent_bot_attempts.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        app,
        "_authenticated_message_caller",
        lambda *_args, **_kwargs: SimpleNamespace(
            sender="inbox-watchdog",
            subject="inbox-watchdog",
            via="daemon",
            on_behalf_of=None,
        ),
    )

    result = app._ipc_message_send(
        "message.send",
        {
            "from": "inbox-watchdog",
            "to": ["user:alice"],
            "summary": "watchdog alert",
            "message": "watchdog alert",
            "idempotencyKey": "n1-corrupt-system",
        },
        "daemon",
    )

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert gateway.calls == []


def test_agent_shaped_ineligible_sender_with_unreadable_ledger_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent-shaped sender may be replaying what its bot already sent, so
    an unreadable ledger is a typed refusal, never a possible second DM."""

    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    path = app._agent_bot_attempts.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(DaemonRequestError) as raised:
        _ipc_replay(app, operation="n1-corrupt-agent")

    assert raised.value.code == "USER_DELIVERY_UNAVAILABLE"
    assert fallback.requests == []
    assert gateway.calls == []


def test_failed_prune_save_does_not_discard_a_read_accepted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N5: get() prunes and re-saves; a failed save (ENOSPC) must still
    return the record it read, so an ineligible replay stays a duplicate."""

    now = [10_000]
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    ledger = app._agent_bot_attempts
    ledger._clock_ms = lambda: now[0]
    ledger._retention_ms = 60_000
    first = _send(app, operation="n5-prune")
    # An unrelated old row the next read will prune.
    now[0] -= 120_000
    ledger.settle(
        "old:user:alice",
        adapter="bot",
        lark_idempotency_key="old",
        state=ATTEMPT_DEFINITELY_NOT_SENT,
        reason="adapter-not-running",
    )
    now[0] += 120_000

    def no_space(_values: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ledger, "_save", no_space)
    replay = _ipc_replay(app, operation="n5-prune")

    assert replay["deliveries"][0]["duplicate"] is True
    assert replay["deliveries"][0]["nativeMessageId"] == first["deliveries"][0]["nativeMessageId"]
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_accepted_record_without_delivery_on_ineligible_replay_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    app._agent_bot_attempts.settle(
        "n-missing:user:alice",
        adapter="bot",
        lark_idempotency_key="k",
        state=ATTEMPT_ACCEPTED,
        delivery=None,
    )

    replay = _ipc_replay(app, operation="n-missing")

    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert fallback.requests == []
    assert gateway.calls == []


def test_dedupe_window_runs_from_the_first_send_not_the_late_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uncertain settle can trail the POST by the 65 s call timeout; the
    Lark uuid window started at the POST."""

    now = [10_000]
    gateway = _Gateway(TimeoutError("post recorded, reply timed out"))
    send = gateway.send_owner_dm

    def slow_send(open_id: str, text: str, *, idempotency_key: str) -> str:
        try:
            return send(open_id, text, idempotency_key=idempotency_key)
        finally:
            now[0] += 65_000

    gateway.send_owner_dm = slow_send  # type: ignore[method-assign]
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    app._agent_bot_attempts._clock_ms = lambda: now[0]

    _send(app, operation="n2-window")
    # One hour after the POST, but only ~59 minutes after the settle.
    now[0] = 10_000 + 60 * 60 * 1000 + 1
    gateway.error = None
    replay = _send(app, operation="n2-window")

    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


@pytest.mark.parametrize(
    ("sender", "subject", "falls_through"),
    [
        pytest.param("inbox-watchdog", "inbox-watchdog", True, id="system-service"),
        pytest.param(None, None, True, id="dispatch-service"),
        # The operator speaking for an agent is that agent.
        pytest.param("user:alice", SENDER, False, id="operator-on-behalf-of-agent"),
        pytest.param(SENDER, SENDER, False, id="agent"),
    ],
)
def test_unreadable_ledger_fallthrough_is_decided_by_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: str | None,
    subject: str | None,
    falls_through: bool,
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    sender = sender or app._dispatch_service_actor
    subject = subject or app._dispatch_service_actor
    path = app._agent_bot_attempts.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        app,
        "_authenticated_message_caller",
        lambda *_args, **_kwargs: SimpleNamespace(
            sender=sender,
            subject=subject,
            via="daemon",
            on_behalf_of=None if subject == sender else subject,
        ),
    )

    def send() -> dict[str, object]:
        return app._ipc_message_send(
            "message.send",
            {
                "from": sender,
                "to": ["user:alice"],
                "summary": "relay",
                "message": "relay",
                "idempotencyKey": f"subject-{sender}",
            },
            "daemon",
        )

    if falls_through:
        assert send()["ok"] is True
        assert len(fallback.requests) == 1
    else:
        with pytest.raises(DaemonRequestError) as raised:
            send()
        assert raised.value.code == "USER_DELIVERY_UNAVAILABLE"
        assert fallback.requests == []
    assert gateway.calls == []
