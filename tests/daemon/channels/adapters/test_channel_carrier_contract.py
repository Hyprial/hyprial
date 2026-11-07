"""L1-L4, L6-L7: the claude channel as a full carrier (wp5c.md §4, §6.3).

A real ``DaemonApplication`` with the WP7 reply-reminder consumer wired, a real
``ClaudeChannelAdapter`` behind the real ``StatelessDaemonProxy``, and the real
pulse files the Claude Code hooks write.  The fixtures never preset a
``last_end``: the first turn of a new session must already be covered.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.biz.impl.turn_pulse import signal_turn_ended
from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.mcp import (
    ClaudeChannelAdapter,
    ClaudeChannelDriver,
    StatelessDaemonProxy,
    WakeCoordinator,
)
from hyprial.daemon.impl.mcp.api import DaemonDisconnected, DaemonRequestRejected
from hyprial.daemon.impl.mcp.channel.loops import _forward_turn_signals
from hyprial.kernel import DaemonRequestError
from tests.daemon.harnesses.carrier.turns.test_turn_reports import (
    A_NAME,
    A_URI,
    X_URI,
    _TurnApp,
    _wall_clock,
)

SESSION = "session-1"


@dataclass
class _Store:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class _Channel:
    async def notify(self, method: str, params: dict[str, Any]) -> None:
        return None


class _Factory:
    """A daemon connection factory over a real application; records frames."""

    def __init__(self, app: _TurnApp) -> None:
        self.app = app
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.fail_once: dict[str, int] = {}

    def of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.frames if name == method]

    @asynccontextmanager
    async def connect(self):  # type: ignore[no-untyped-def]
        factory = self

        class Connection:
            async def request(
                self, method: str, params: dict[str, Any], *, request_id: str | None = None
            ) -> dict[str, Any]:
                factory.frames.append((method, dict(params)))
                if factory.fail_once.get(method, 0) > 0:
                    factory.fail_once[method] -= 1
                    raise DaemonDisconnected("scripted transport loss")
                try:
                    return factory.app.app.handle(method, dict(params))
                except DaemonRequestError as error:
                    raise DaemonRequestRejected(error.code, str(error)) from error

        yield Connection()


def _adapter(factory: _Factory, turns: Path) -> ClaudeChannelAdapter:
    session = InteractiveSession(
        actor=A_NAME, cwd="/work", command=("claude",), source="claude-channel"
    )
    return ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),  # type: ignore[arg-type]
        WakeCoordinator(
            _Store(DesiredState(interactive_sessions=(session,))),
            ClaudeChannelDriver(_Channel()),
        ),
        actor=A_NAME,
        session_ref=SESSION,
        cwd="/work",
        command=("claude",),
        turn_signal_dir=turns,
    )


def _seed(app: _TurnApp, message_id: str, intent: str) -> None:
    app.inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id="conv-turns",
            sender=X_URI,
            recipient=A_URI,
            payload=json.dumps({"message": "body"}).encode(),
            intent=intent,
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
            summary="S",
        )
    )


def _pending_ids(app: _TurnApp) -> list[str]:
    listed = app.app.handle(
        "message.pending.list",
        {"actor": A_NAME, "sessionRef": SESSION, "fetched": True},
    )
    return sorted(
        row["messageId"] for row in listed["messages"] if row.get("kind") != "notice"
    )


def _stop(turns: Path) -> None:
    signal_turn_ended(turns)


def _prompt(turns: Path) -> None:
    signal_turn_ended(turns / "start")


def _fixture(tmp_path: Path) -> tuple[_TurnApp, _Factory, ClaudeChannelAdapter, Path, dict[str, int]]:
    clock = _wall_clock()
    app = _TurnApp(tmp_path, clock)
    factory = _Factory(app)
    turns = tmp_path / "session.turns"
    return app, factory, _adapter(factory, turns), turns, clock


@pytest.mark.anyio
async def test_first_turn_of_a_new_session_gets_a_reminder(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        _seed(app, "r1", "request")
        await adapter.start()

        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        read = await adapter.harness_read()
        assert [row["messageId"] for row in read["items"]] == ["r1"]

        clock["now"] += 60_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)

        assert app.recorder.wait_for(lambda: len(app.inbox.system_notices(A_URI)) == 1)
        assert "r1" in app.inbox.system_notices(A_URI)[0].summary
        started = factory.of("session.turn.started")
        ended = factory.of("session.turn.ended")
        assert len(started) == 1 and ended[0]["reportIds"] == [started[0]["reportId"]]
    finally:
        app.close()


@pytest.mark.anyio
async def test_turn_end_settles_by_hint_without_answering(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        _seed(app, "r1", "request")
        _seed(app, "p1", "reply")
        _seed(app, "e1", "event")
        await adapter.start()

        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        await adapter.harness_read()
        clock["now"] += 60_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)

        assert sorted(p["messageId"] for p in factory.of("message.settle_hint")) == [
            "e1",
            "p1",
            "r1",
        ]
        assert sorted(p["messageId"] for p in factory.of("message.ack")) == ["e1", "p1"]
        assert factory.of("message.reply") == []
        assert _pending_ids(app) == ["r1"]
        # Settlement waited for the Stop report to be confirmed.
        order = [name for name, _ in factory.frames]
        assert order.index("session.turn.ended") < order.index("message.settle_hint")
    finally:
        app.close()


@pytest.mark.anyio
async def test_late_start_signal_is_backfilled_by_read(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        _seed(app, "r1", "request")
        await adapter.start()

        await adapter.harness_read()  # no start pulse yet
        order = [
            name
            for name, params in factory.frames
            if name in ("session.turn.started", "message.pending.batch")
        ]
        # The tool read opens the turn before its own consuming batch capture
        # (the earlier batch frame belongs to the startup poll).
        started_at = order.index("session.turn.started")
        assert order[-1] == "message.pending.batch"
        assert started_at == len(order) - 2

        _prompt(turns)  # the late pulse of the same turn
        await _forward_turn_signals(adapter, turns)
        assert len(factory.of("session.turn.started")) == 1
        assert not tuple((turns / "start").iterdir())

        clock["now"] += 60_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)
        assert app.recorder.wait_for(lambda: len(app.inbox.system_notices(A_URI)) == 1)
        assert "r1" in app.inbox.system_notices(A_URI)[0].summary
    finally:
        app.close()


@pytest.mark.anyio
async def test_turn_signals_are_retried_across_child_restart(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        await adapter.start()
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        assert len(factory.of("session.turn.started")) == 1
        report_id = factory.of("session.turn.started")[0]["reportId"]

        clock["now"] += 1_000
        _stop(turns)
        # The proxy retries a lost socket once: fail both attempts.
        factory.fail_once["session.turn.ended"] = 2
        await _forward_turn_signals(adapter, turns)
        assert (turns / "state" / "open-turn").is_file()
        assert len(tuple(turns.iterdir())) == 3  # state/, start/, the Stop pulse

        restarted = _adapter(factory, turns)  # a new channel child
        await _forward_turn_signals(restarted, turns)

        ended = factory.of("session.turn.ended")
        assert [p["reportIds"] for p in ended] == [[report_id]] * 3  # 2 lost + 1 sent
        assert app.recorder.wait_for(lambda: len(app.recorder.of("turn.completed")) == 1)
        time.sleep(0.1)
        assert len(app.recorder.of("turn.completed")) == 1
        assert not (turns / "state" / "open-turn").exists()
        assert [p.name for p in turns.iterdir() if p.is_file()] == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_a_start_over_an_unclosed_turn_reports_it_failed(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        await adapter.start()
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        assert len(factory.of("session.turn.started")) == 1
        first = factory.of("session.turn.started")[0]["reportId"]

        clock["now"] += 1_000
        _prompt(turns)  # the previous turn never reached Stop (StopFailure)
        await _forward_turn_signals(adapter, turns)

        ended = factory.of("session.turn.ended")
        assert ended[0]["reportIds"] == [first] and ended[0]["status"] == "failed"
        assert len(factory.of("session.turn.started")) == 2
        # The current-run refresh is observation; only boundary reports order here.
        order = [
            name
            for name, _ in factory.frames
            if name in {"session.turn.started", "session.turn.ended"}
        ]
        assert order == ["session.turn.started", "session.turn.ended", "session.turn.started"]
        assert factory.of("message.settle_hint") == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_explicit_ack_makes_the_end_of_turn_query_idempotent(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        _seed(app, "e1", "event")
        await adapter.start()
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        await adapter.harness_read()
        await adapter.harness_ack("e1")

        clock["now"] += 60_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)  # must not raise or retry forever
        await _forward_turn_signals(adapter, turns)

        assert len(factory.of("session.turn.started")) == 1
        assert _pending_ids(app) == []
        assert len(factory.of("message.settle_hint")) <= 1
        assert not (turns / "state" / "open-turn").exists()
    finally:
        app.close()


@pytest.mark.anyio
async def test_doorbell_listing_is_not_injection(tmp_path: Path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        _seed(app, "e1", "event")
        await adapter.start()  # the doorbell list enqueues e1 but injects nothing
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        clock["now"] += 60_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)

        assert len(factory.of("session.turn.started")) == 1
        assert factory.of("message.settle_hint") == []
        assert _pending_ids(app) == ["e1"]
    finally:
        app.close()
