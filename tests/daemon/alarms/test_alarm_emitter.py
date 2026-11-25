from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon import Alarm, AlarmEmitter
from hyprial.kernel.impl.log import Logger


def entries(logger: Logger) -> list[dict[str, object]]:
    return [json.loads(line) for line in logger.path.read_text().splitlines()]


def alarm(*, audience: str = "agent", reason: str = "TARGET_NOT_FOUND") -> Alarm:
    return Alarm(
        correlation_id="msg-original",
        message_id="msg-original",
        conversation_id="conversation-1",
        sender="agent:alice:mac:sender",
        recipient="agent:bob:pc:missing",
        reason=reason,
        audience=audience,  # type: ignore[arg-type]
    )


def test_agent_alarm_delivers_and_logs_the_complete_vocabulary(
    tmp_path: Path,
) -> None:
    logger = Logger.daemon(tmp_path, name="node-a")
    delivered: list[tuple[Alarm, str]] = []
    emitter = AlarmEmitter(
        logger,
        deliver_agent=lambda item, text: not delivered.append((item, text)),
    )

    result = emitter.emit(alarm())

    assert result.status == "delivered"
    assert delivered[0][1] == (
        "System notice: delivery failed for message msg-original; "
        "reason=TARGET_NOT_FOUND."
    )
    records = entries(logger)
    assert [item["event"] for item in records] == [
        "terminal.failure",
        "alarm.raised",
        "alarm.delivered",
    ]
    assert {item["correlationId"] for item in records} == {"msg-original"}


def test_same_conversation_and_reason_is_throttled_within_the_window(
    tmp_path: Path,
) -> None:
    logger = Logger.daemon(tmp_path, name="node-a")
    claims: set[tuple[str, str, int]] = set()
    delivered: list[str] = []

    def claim(conversation: str, reason: str, window: int, _now: int) -> bool:
        key = (conversation, reason, window)
        if key in claims:
            return False
        claims.add(key)
        return True

    emitter = AlarmEmitter(
        logger,
        deliver_agent=lambda item, _text: not delivered.append(item.message_id),
        claim=claim,
        clock_ms=lambda: 61_000,
    )

    assert emitter.emit(alarm()).status == "delivered"
    assert emitter.emit(alarm()).status == "throttled"

    assert delivered == ["msg-original"]
    records = entries(logger)
    assert [item["event"] for item in records].count("terminal.failure") == 2
    assert [item["event"] for item in records].count("alarm.delivered") == 1
    assert any(item.get("throttled") is True for item in records)


def test_bounce_delivery_failure_logs_once_and_never_recurses(tmp_path: Path) -> None:
    logger = Logger.daemon(tmp_path, name="node-a")
    attempts = 0

    def fail(_alarm: Alarm, _text: str) -> bool:
        nonlocal attempts
        attempts += 1
        raise OSError("platform unavailable")

    claims: set[tuple[str, str, int]] = set()

    def claim(conversation: str, reason: str, window: int, _now: int) -> bool:
        key = (conversation, reason, window)
        if key in claims:
            return False
        claims.add(key)
        return True

    emitter = AlarmEmitter(logger, deliver_agent=fail, claim=claim)
    result = emitter.emit(alarm())
    repeated = emitter.emit(alarm())

    assert result.status == "failed"
    assert repeated.status == "throttled"
    assert attempts == 1
    records = entries(logger)
    assert [item["event"] for item in records[:3]] == [
        "terminal.failure",
        "alarm.raised",
        "alarm.failed",
    ]
    assert records[2]["failure"] == "OSError"
    assert [item["event"] for item in records[3:]] == [
        "terminal.failure",
        "alarm.raised",
    ]
    assert records[-1]["throttled"] is True


def test_ttl_reason_is_reported_as_a_deadline_not_a_refusal() -> None:
    for reason in ("TTL_EXPIRED",):
        human = AlarmEmitter.render(
            Alarm(
                correlation_id="c",
                message_id="m-1",
                conversation_id="conv",
                sender="channel:lark:app",
                recipient="agent:a:n:x",
                reason=reason,
                audience="human",
            )
        )
        assert "TTL 到期" in human, reason
        assert "投递失败" not in human, reason
        assert "m-1" in human

        agent = AlarmEmitter.render(
            Alarm(
                correlation_id="c",
                message_id="m-1",
                conversation_id="conv",
                sender="agent:a:n:sender",
                recipient="agent:a:n:x",
                reason=reason,
                audience="agent",
            )
        )
        assert "reached its TTL" in agent, reason
        assert "delivery failed" not in agent, reason


def test_a_refusal_still_reads_as_a_failure() -> None:
    """Positive control: a reason that names a refusal IS evidence about the
    recipient, so it must keep the old wording -- otherwise the change would
    have made every outcome equally vague, which carries no information."""

    for reason in ("TARGET_NOT_FOUND", "PROVIDER_BLOCKED", "unsupported-message-type"):
        human = AlarmEmitter.render(
            Alarm(
                correlation_id="c",
                message_id="m-2",
                conversation_id="conv",
                sender="channel:lark:app",
                recipient="agent:a:n:x",
                reason=reason,
                audience="human",
            )
        )
        assert human == f"消息投递失败：{reason}（消息 m-2）。", reason


def _claiming_emitter(tmp_path: Path, delivered: list[str]) -> AlarmEmitter:
    claims: set[tuple[str, str, int]] = set()

    def claim(scope: str, reason: str, window: int, _now: int) -> bool:
        key = (scope, reason, window)
        if key in claims:
            return False
        claims.add(key)
        return True

    return AlarmEmitter(
        Logger.daemon(tmp_path, name="node-a"),
        deliver_agent=lambda item, _text: not delivered.append(item.message_id),
        claim=claim,
        clock_ms=lambda: 61_000,
    )


def _default_conversation_alarm(message_id: str) -> Alarm:
    from hyprial.daemon.impl.conversations.ids import default_conversation_id

    return Alarm(
        correlation_id=message_id,
        message_id=message_id,
        conversation_id=default_conversation_id(
            "agent:alice:mac:sender", "agent:bob:pc:missing"
        ),
        sender="agent:alice:mac:sender",
        recipient="agent:bob:pc:missing",
        reason="TARGET_NOT_FOUND",
        audience="agent",
    )


def test_two_failing_sends_in_one_default_conversation_each_alarm(
    tmp_path: Path,
) -> None:
    """A default conversation spans every no-id send of a pair, so its
    throttle unit is the message: a second failing send is not swallowed by
    the first one's window, while the same message failing again is."""

    delivered: list[str] = []
    emitter = _claiming_emitter(tmp_path, delivered)

    assert emitter.emit(_default_conversation_alarm("msg-1")).status == "delivered"
    assert emitter.emit(_default_conversation_alarm("msg-2")).status == "delivered"
    assert emitter.emit(_default_conversation_alarm("msg-1")).status == "throttled"

    assert delivered == ["msg-1", "msg-2"]


def test_an_explicit_conversation_keeps_its_conversation_throttle(
    tmp_path: Path,
) -> None:
    delivered: list[str] = []
    emitter = _claiming_emitter(tmp_path, delivered)
    first = alarm()
    second = Alarm(
        correlation_id="msg-other",
        message_id="msg-other",
        conversation_id=first.conversation_id,
        sender=first.sender,
        recipient=first.recipient,
        reason=first.reason,
        audience=first.audience,
    )

    assert emitter.emit(first).status == "delivered"
    assert emitter.emit(second).status == "throttled"
    assert delivered == ["msg-original"]


def test_every_derived_default_conversation_is_recognised_by_the_dedup_scope() -> None:
    from hyprial.daemon.impl.conversations.ids import default_conversation_id
    from hyprial.daemon.impl.correlation.alarm import (
        failure_dedup_scope,
        is_default_conversation_id,
    )

    pairs = (
        ("agent:op:node-1:alpha", "agent:op:node-1:beta"),
        ("a.b4kuo3kxusjtp6ug25qzqycshe", "user:op"),
        ("adapter:lark:app:chat", "route:r:dm"),
        ("", ""),
    )
    for a, b in pairs:
        conversation = default_conversation_id(a, b)
        assert is_default_conversation_id(conversation), conversation
        assert failure_dedup_scope(conversation, "m-1") == "request:m-1"
        assert failure_dedup_scope(conversation, None) == conversation
    for explicit in ("conversation-1", "lark:oc_chat", "", "dc.short", "DC.ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        assert not is_default_conversation_id(explicit), explicit
        assert failure_dedup_scope(explicit, "m-1") == explicit
