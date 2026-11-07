"""WP7 B1-B7 and review A4: the reply-reminder consumer state machine.

The consumer runs on one bus lane: it tracks per-actor turn windows, asks the
daemon port which requests are still unanswered, and writes one system notice
per newly-unanswered group.  These tests drive ``observe`` directly with fake
events and a recording fake port, so no bus or inbox is needed.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from hyprial.biz.impl.hooks.reminder import (
    REPLY_REMINDER_EVENTS,
    ReplyReminderConsumer,
)
from hyprial.kernel import HookEvent, turn_event_payload

A = "agent:owner:node:worker"
B = "agent:owner:node:other"
X = "agent:owner:node:peer"
HINT = "；回复用 reply，无需回复用 ack"


class _Port:
    """A recording port: queued query results, configurable failures."""

    def __init__(self, *, results: list[tuple[str, ...]] | None = None) -> None:
        self.queries: list[dict[str, Any]] = []
        self.posts: list[tuple[str, tuple[str, ...], str]] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        self._results = list(results or [])
        self.default: tuple[str, ...] = ()
        self.query_error: BaseException | None = None
        self.post_error: BaseException | None = None
        self.post_result = True

    def unreplied_requests(
        self,
        actor: str,
        *,
        delivery_ids: Any,
        fetched_since_ms: int | None,
        fetched_until_ms: int,
        since_inclusive: bool,
    ) -> tuple[str, ...]:
        self.queries.append(
            {
                "actor": actor,
                "delivery_ids": frozenset(delivery_ids),
                "fetched_since_ms": fetched_since_ms,
                "fetched_until_ms": fetched_until_ms,
                "since_inclusive": since_inclusive,
            }
        )
        if self.query_error is not None:
            raise self.query_error
        if self._results:
            return tuple(self._results.pop(0))
        return self.default

    def post_reply_reminder(
        self, actor: str, request_ids: Any, text: str
    ) -> bool:
        self.posts.append((actor, tuple(request_ids), text))
        if self.post_error is not None:
            raise self.post_error
        return self.post_result

    def log(self, level: str, event: str, **fields: Any) -> None:
        self.logs.append((level, event, dict(fields)))


def _payload(
    *,
    delivery_id: str = "d1",
    started_at_ms: int = 100,
    ended_at_ms: int = 200,
    status: str = "completed",
) -> MappingProxyType:
    return turn_event_payload(
        delivery_id=delivery_id,
        sender=X,
        conversation="conv",
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        status=status,
        tool_names=(),
        output="",
        prompt="",
        tool_calls=(),
        fidelity="observed",
    )


def _event(
    name: str,
    *,
    actor: str | None = A,
    seq: int = 1,
    source: str = "daemon.turns",
    payload: Any = None,
) -> HookEvent:
    return HookEvent(
        event=name,
        kind="observe",
        emitted_at_ms=int(payload.get("endedAtMs", 0)) if payload else 0,
        seq=seq,
        source=source,
        actor=actor,
        payload=payload or {},
    )


def _started(actor: str = A, *, started_at_ms: int = 100, seq: int = 1) -> HookEvent:
    return _event(
        "turn.started",
        actor=actor,
        seq=seq,
        payload=_payload(
            delivery_id="",
            started_at_ms=started_at_ms,
            ended_at_ms=started_at_ms,
            status="started",
        ),
    )


def _completed(
    actor: str = A,
    *,
    delivery_id: str = "d1",
    ended_at_ms: int = 200,
    seq: int = 2,
) -> HookEvent:
    return _event(
        "turn.completed",
        actor=actor,
        seq=seq,
        payload=_payload(
            delivery_id=delivery_id, ended_at_ms=ended_at_ms, status="completed"
        ),
    )


def test_b1_unreplied_request_produces_one_notice() -> None:
    port = _Port(results=[("d1",)])
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100))
    consumer.observe(_completed(delivery_id="d1", ended_at_ms=200))

    assert port.queries == [
        {
            "actor": A,
            "delivery_ids": frozenset({"d1"}),
            "fetched_since_ms": 100,
            "fetched_until_ms": 200,
            "since_inclusive": True,
        }
    ]
    assert port.posts == [(A, ("d1",), "1 条请求未回复：d1" + HINT)]


def test_b2_everything_replied_posts_nothing() -> None:
    port = _Port(results=[()])
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started())
    consumer.observe(_completed())

    assert len(port.queries) == 1
    assert port.posts == []


def test_b3_idempotency_across_replays_and_subset_groups() -> None:
    port = _Port(results=[("d1",), ("d1",), ("d1",), ("d1", "d2")])
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100, seq=1))
    completed = _completed(delivery_id="d1", ended_at_ms=200, seq=2)
    consumer.observe(completed)
    # (a) the exact same completed event delivered twice posts once.
    consumer.observe(completed)

    # (b) a later turn whose unanswered set is a subset is suppressed.
    consumer.observe(_started(started_at_ms=200, seq=3))
    consumer.observe(_completed(delivery_id="d2", ended_at_ms=300, seq=4))

    # (c) a superset posts once and names every id.
    consumer.observe(_started(started_at_ms=300, seq=5))
    consumer.observe(_completed(delivery_id="d3", ended_at_ms=400, seq=6))

    assert [post[1] for post in port.posts] == [("d1",), ("d1", "d2")]
    assert any(event == "reply_reminder.suppressed" for _level, event, _f in port.logs)


def test_b4_interactive_degradation_without_turn_started() -> None:
    # (a) first completion, no window, no injected id: no query at all.
    port_a = _Port()
    consumer_a = ReplyReminderConsumer(port_a, logger=port_a.log)
    consumer_a.observe(_completed(delivery_id="", ended_at_ms=1000))
    assert port_a.queries == []

    # (b) next completion uses the previous end as an exclusive window start.
    port_b = _Port(results=[()])
    consumer_b = ReplyReminderConsumer(port_b, logger=port_b.log)
    consumer_b.observe(_completed(delivery_id="", ended_at_ms=1000))
    consumer_b.observe(_completed(delivery_id="", ended_at_ms=2000, seq=3))
    assert port_b.queries == [
        {
            "actor": A,
            "delivery_ids": frozenset(),
            "fetched_since_ms": 1000,
            "fetched_until_ms": 2000,
            "since_inclusive": False,
        }
    ]

    # (c) a delivery id alone is I1 even with no window.
    port_c = _Port(results=[("d9",)])
    consumer_c = ReplyReminderConsumer(port_c, logger=port_c.log)
    consumer_c.observe(_completed(delivery_id="d9", ended_at_ms=1000))
    assert port_c.queries == [
        {
            "actor": A,
            "delivery_ids": frozenset({"d9"}),
            "fetched_since_ms": None,
            "fetched_until_ms": 1000,
            "since_inclusive": False,
        }
    ]
    assert port_c.posts[0][1] == ("d9",)


def test_b5_failed_turn_never_reminds_and_sets_the_next_window() -> None:
    port = _Port(results=[("d1",)])
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100))
    consumer.observe(
        _event(
            "turn.failed",
            seq=2,
            payload=_payload(
                delivery_id="d1", ended_at_ms=500, status="failed"
            ),
        )
    )
    assert port.queries == []
    assert port.posts == []

    # The next completion has no started: the failed turn's end is the start.
    consumer.observe(_completed(delivery_id="", ended_at_ms=900, seq=3))
    assert port.queries[0]["fetched_since_ms"] == 500
    assert port.queries[0]["since_inclusive"] is False
    assert port.queries[0]["fetched_until_ms"] == 900


def test_b6_query_failure_is_logged_and_retried_on_replay() -> None:
    port = _Port()
    port.default = ("d1",)
    port.query_error = RuntimeError("authority unavailable")
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100))
    completed = _completed(delivery_id="d1", ended_at_ms=200)
    consumer.observe(completed)

    assert port.posts == []
    assert any(
        event == "reply_reminder.failed" and fields.get("actor") == A
        for _level, event, fields in port.logs
    )

    # Replaying the same event reuses the original window and retries.
    port.query_error = None
    consumer.observe(completed)
    assert [post[1] for post in port.posts] == [("d1",)]


def test_b6_post_exception_is_logged_and_retried_on_replay() -> None:
    port = _Port()
    port.default = ("d1",)
    port.post_error = RuntimeError("notice write failed")
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100))
    completed = _completed(delivery_id="d1", ended_at_ms=200)
    consumer.observe(completed)

    assert len(port.posts) == 1
    assert any(
        event == "reply_reminder.failed" and fields.get("actor") == A
        for _level, event, fields in port.logs
    )

    port.post_error = None
    consumer.observe(completed)
    assert [post[1] for post in port.posts][-1] == ("d1",)
    assert len(port.posts) == 2


def test_b6_false_write_is_logged_and_retried_on_replay() -> None:
    port = _Port()
    port.default = ("d1",)
    port.post_result = False
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100))
    completed = _completed(delivery_id="d1", ended_at_ms=200)
    consumer.observe(completed)

    assert any(
        event == "reply_reminder.failed" and fields.get("actor") == A
        for _level, event, fields in port.logs
    )

    # A False write must not enter the reminded set: the same group retries.
    port.post_result = True
    consumer.observe(completed)
    assert [post[1] for post in port.posts][-1] == ("d1",)
    assert len(port.posts) == 2


def test_b7_ignores_irrelevant_events_and_evicts_oldest_actor() -> None:
    port = _Port()
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_event("delivery.dispatched", actor=A, payload={"deliveryId": "d"}))
    consumer.observe(_event("turn.completed", actor=None, payload=_payload()))
    assert port.queries == []

    # Fill the actor LRU beyond its 1024 cap: actor 0 is evicted.
    for index in range(1025):
        consumer.observe(
            _started(actor=f"agent:owner:node:{index}", started_at_ms=100 + index)
        )
    evicted = "agent:owner:node:0"
    consumer.observe(_completed(actor=evicted, delivery_id="", ended_at_ms=9999))
    assert port.queries == []

    # A survivor still has its window from turn.started.
    port.default = ()
    survivor = "agent:owner:node:1024"
    consumer.observe(_completed(actor=survivor, delivery_id="", ended_at_ms=9999))
    assert port.queries[-1]["fetched_since_ms"] == 100 + 1024


def test_a4_reminded_ids_evict_fifo_and_actors_do_not_cross_suppress() -> None:
    port = _Port()
    consumer = ReplyReminderConsumer(port, logger=port.log)

    # Two actors using the same id must each remind once.
    port.default = ("shared",)
    consumer.observe(_started(actor=A))
    consumer.observe(_completed(actor=A, delivery_id="shared"))
    consumer.observe(_started(actor=B))
    consumer.observe(_completed(actor=B, delivery_id="shared"))
    assert [post[0] for post in port.posts] == [A, B]
    port.posts.clear()

    # One actor's 257th distinct id evicts the oldest (FIFO, cap 256).
    for index in range(257):
        port.default = (f"id-{index}",)
        consumer.observe(_started(actor=A, seq=1000 + index * 2))
        consumer.observe(
            _completed(
                actor=A, delivery_id=f"id-{index}", seq=1001 + index * 2
            )
        )
    assert len(port.posts) == 257

    # id-0 fell out of the reminder set, so it is reminded again.
    port.default = ("id-0",)
    consumer.observe(_started(actor=A, seq=9000))
    consumer.observe(_completed(actor=A, delivery_id="id-0", seq=9001))
    assert port.posts[-1][1] == ("id-0",)
    assert len(port.posts) == 258


def test_reply_reminder_events_are_the_design_names() -> None:
    assert REPLY_REMINDER_EVENTS == ("turn.started", "turn.completed", "turn.failed")


def test_a_stale_completed_replay_does_not_clear_the_next_turn_start() -> None:
    """Review suggestion: replaying an old completed event after the next
    turn.started must neither erase the new start nor move last_end back."""

    port = _Port()
    port.default = ("d1",)
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100, seq=1))
    old = _completed(delivery_id="d1", ended_at_ms=200, seq=2)
    consumer.observe(old)
    consumer.observe(_started(started_at_ms=300, seq=3))

    consumer.observe(old)  # replay of the already-handled event

    port.queries.clear()
    consumer.observe(_completed(delivery_id="", ended_at_ms=400, seq=4))
    assert port.queries[0]["fetched_since_ms"] == 300
    assert port.queries[0]["since_inclusive"] is True
    assert port.queries[0]["fetched_until_ms"] == 400


def test_a_stale_completed_replay_keeps_last_end_for_the_next_window() -> None:
    port = _Port()
    consumer = ReplyReminderConsumer(port, logger=port.log)

    consumer.observe(_started(started_at_ms=100, seq=1))
    old = _completed(delivery_id="d1", ended_at_ms=200, seq=2)
    consumer.observe(old)
    consumer.observe(_completed(delivery_id="", ended_at_ms=500, seq=3))
    consumer.observe(old)  # stale replay must not pull last_end back to 200

    port.queries.clear()
    consumer.observe(_completed(delivery_id="", ended_at_ms=900, seq=4))
    assert port.queries[0]["fetched_since_ms"] == 500
    assert port.queries[0]["since_inclusive"] is False
