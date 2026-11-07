"""WP7 B9: the shell composition exposes a factory that builds the consumer."""

from __future__ import annotations

from typing import Any

from hyprial.biz.impl.hooks.reminder import (
    REPLY_REMINDER_EVENTS,
    ReplyReminderConsumer,
)
from hyprial.kernel import HookEvent, turn_event_payload
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

A = "agent:owner:node:worker"


class _Port:
    def __init__(self) -> None:
        self.posts: list[tuple[str, tuple[str, ...], str]] = []

    def unreplied_requests(self, actor: str, **_kwargs: Any) -> tuple[str, ...]:
        return ("r1",)

    def post_reply_reminder(
        self, actor: str, request_ids: Any, text: str
    ) -> bool:
        self.posts.append((actor, tuple(request_ids), text))
        return True

    def log(self, level: str, event: str, **fields: Any) -> None:
        return None


def _turn_event(name: str, *, delivery_id: str, start: int, end: int, seq: int) -> HookEvent:
    return HookEvent(
        event=name,
        kind="observe",
        emitted_at_ms=end,
        seq=seq,
        source="daemon.turns",
        actor=A,
        payload=turn_event_payload(
            delivery_id=delivery_id,
            sender="agent:owner:node:peer",
            conversation="conv",
            started_at_ms=start,
            ended_at_ms=end,
            status="completed" if name == "turn.completed" else "started",
            tool_names=(),
            output="",
            prompt="",
            tool_calls=(),
            fidelity="observed",
        ),
    )


def test_shell_factory_builds_a_working_consumer() -> None:
    factory = daemon_dependencies()["reply_reminder_consumer_factory"]
    port = _Port()
    consumer = factory(port)

    assert isinstance(consumer, ReplyReminderConsumer)
    assert REPLY_REMINDER_EVENTS == ("turn.started", "turn.completed", "turn.failed")

    consumer.observe(
        _turn_event("turn.started", delivery_id="r1", start=10, end=10, seq=1)
    )
    consumer.observe(
        _turn_event("turn.completed", delivery_id="r1", start=10, end=20, seq=2)
    )

    assert port.posts == [
        (A, ("r1",), "1 条请求未回复：r1；回复用 reply，无需回复用 ack")
    ]
