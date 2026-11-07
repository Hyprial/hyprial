"""``top.snapshot`` / ``hyprial agent top`` contract tests.

Two regression sources are pinned here:

- the verdict classifier (stuck/spin/timeout/backlog/idle/ok) against
  hand-built evidence rows, one case per incident shape seen in production;
- generation splitting of worker-log turn statistics, so a restarted
  worker's history never leaks into the current process's counters.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.operations.top import (
    InteractiveTurnStats,
    TopThresholds,
    WorkerTurnStats,
    build_top_snapshot,
    classify_actor,
    read_worker_turn_stats,
)
from hyprial.shell.impl.cli.commands.daemon.top import _render_top, _top_state_label
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

NOW_MS = 1_800_000_000_000  # fixed reference instant for every test


def _ts(ms: int) -> str:
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _stats(
    *,
    pid: int | None = 111,
    started_ms: int | None = NOW_MS - 3_600_000,
    turns: tuple[tuple[int, str], ...] = (),
    open_turn_ms: int | None = None,
    last_ended_ms: int | None = None,
    stalled_ms: int | None = None,
) -> WorkerTurnStats:
    durations = tuple(duration for duration, _ in turns)
    outcomes = tuple(outcome for _, outcome in turns)
    if last_ended_ms is None and turns:
        last_ended_ms = NOW_MS - 60_000
    return WorkerTurnStats(
        generation_pid=pid,
        process_started_at_ms=started_ms,
        turn_count=len(turns),
        last_turn_ended_at_ms=last_ended_ms,
        recent_durations_ms=durations[-5:],
        recent_outcomes=outcomes[-5:],
        open_turn_started_at_ms=open_turn_ms,
        open_turn_stalled_at_ms=stalled_ms,
    )


def _classify(
    *,
    running: bool = True,
    runtime: str = "pi",
    pending: int = 0,
    stats: WorkerTurnStats | None,
    thresholds: TopThresholds = TopThresholds(),
) -> tuple[str, dict[str, object]]:
    return classify_actor(
        running=running,
        runtime=runtime,
        pending_count=pending,
        now_ms=NOW_MS,
        stats=stats,
        thresholds=thresholds,
    )


# ---------------------------------------------------------------------------
# Verdict classifier: one case per production incident shape.
# ---------------------------------------------------------------------------


def test_stopped_verdict() -> None:
    state, _ = _classify(running=False, stats=None)
    assert state == "stopped"


def test_stuck_verdict_for_unclosed_turn_beyond_threshold() -> None:
    state, detail = _classify(
        stats=_stats(open_turn_ms=NOW_MS - 14 * 60_000),
    )
    assert state == "stuck"
    assert detail["openMs"] == 14 * 60_000


def test_open_turn_inside_budget_is_busy_not_stuck() -> None:
    state, detail = _classify(
        pending=2,
        stats=_stats(open_turn_ms=NOW_MS - 2 * 60_000),
    )
    assert state == "ok"
    assert detail == {"busy": True}


def test_stall_reporting_runtime_is_never_stuck_by_age() -> None:
    # #277: codex reports its own quiet turns, so an open turn of ANY age
    # without an unresolved stalled event is healthy work -- the retired
    # cap-plus-margin rule must not resurface as an age heuristic.
    state, detail = _classify(
        runtime="codex", stats=_stats(open_turn_ms=NOW_MS - 5 * 3_600_000)
    )
    assert state == "ok"
    assert detail == {"busy": True}


def test_unresolved_stall_report_becomes_the_stalled_verdict() -> None:
    state, detail = _classify(
        runtime="codex",
        stats=_stats(
            open_turn_ms=NOW_MS - 40 * 60_000,
            stalled_ms=NOW_MS - 20 * 60_000,
        ),
    )
    assert state == "stalled"
    assert detail["openMs"] == 40 * 60_000
    assert detail["stalledForMs"] == 20 * 60_000


def test_spin_verdict_for_fast_failed_turns() -> None:
    # The incident shape: ~40 consecutive 4-8s turns, all failed.
    state, detail = _classify(
        stats=_stats(
            turns=(
                (6_000, "failed"),
                (5_000, "failed"),
                (7_000, "failed"),
                (8_000, "completed"),
                (4_000, "failed"),
            )
        ),
    )
    assert state == "spin"
    assert detail["failures"] == 4
    assert detail["window"] == 5


def test_fast_successful_turns_are_not_spin() -> None:
    state, _ = _classify(
        stats=_stats(
            turns=(
                (6_000, "completed"),
                (5_000, "completed"),
                (7_000, "completed"),
                (8_000, "failed"),
                (4_000, "completed"),
            )
        ),
    )
    assert state == "ok"


def test_fast_interrupted_turns_are_not_counted_as_failures() -> None:
    state, _ = _classify(
        stats=_stats(turns=tuple((5_000, "interrupted") for _ in range(5))),
    )
    assert state == "ok"


def test_spin_needs_a_full_window() -> None:
    state, _ = _classify(
        stats=_stats(turns=((5_000, "failed"), (6_000, "failed"))),
    )
    assert state == "ok"


def test_cap_hugging_failures_no_longer_produce_a_timeout_verdict() -> None:
    # The retired classifier called this "timeout"; with no cap there is
    # nothing to hug and the shape reads as recent (failed) work.
    state, _ = _classify(
        runtime="codex",
        stats=_stats(
            turns=(
                (3_600_500, "failed"),
                (3_600_200, "failed"),
                (3_600_100, "failed"),
            )
        ),
    )
    assert state == "ok"


def test_resumed_stall_reads_as_busy_again() -> None:
    # stalled_ms is None once worker.turn.resumed cleared the report.
    state, detail = _classify(
        runtime="codex",
        stats=_stats(open_turn_ms=NOW_MS - 40 * 60_000, stalled_ms=None),
    )
    assert state == "ok"
    assert detail == {"busy": True}


def test_backlog_verdict_for_pending_messages_while_idle() -> None:
    state, detail = _classify(
        pending=2,
        stats=_stats(
            turns=((300_000, "completed"),),
            last_ended_ms=NOW_MS - 12 * 60_000,
        ),
    )
    assert state == "backlog"
    assert detail["pending"] == 2
    assert detail["idleMs"] == 12 * 60_000


def test_recently_active_actor_with_pending_is_not_backlog() -> None:
    state, _ = _classify(
        pending=1,
        stats=_stats(
            turns=((300_000, "completed"),),
            last_ended_ms=NOW_MS - 60_000,
        ),
    )
    assert state == "ok"


def test_idle_verdict_for_actor_that_never_turned() -> None:
    state, _ = _classify(stats=_stats())
    assert state == "idle"


def test_never_turned_actor_inside_quiet_window_is_ok() -> None:
    state, _ = _classify(stats=_stats(started_ms=NOW_MS - 29 * 60_000))
    assert state == "ok"


def test_idle_verdict_after_long_quiet_period() -> None:
    state, _ = _classify(
        stats=_stats(
            turns=((264_000, "completed"),),
            last_ended_ms=NOW_MS - 980 * 60_000,
        ),
    )
    assert state == "idle"


def test_quiet_window_boundary_is_not_past_the_threshold() -> None:
    state, _ = _classify(
        stats=_stats(
            turns=((264_000, "completed"),),
            last_ended_ms=NOW_MS - 30 * 60_000,
        ),
    )
    assert state == "ok"


def test_idle_state_is_presented_as_no_recent_output() -> None:
    assert _top_state_label({"state": "idle"}, NOW_MS) == "no recent output"


def test_ok_verdict_for_recently_active_actor() -> None:
    state, _ = _classify(
        stats=_stats(
            turns=((363_000, "completed"),),
            last_ended_ms=NOW_MS - 11 * 60_000,
        ),
    )
    assert state == "ok"


# ---------------------------------------------------------------------------
# Worker-log parsing and generation splitting.
# ---------------------------------------------------------------------------


def _worker_line(ms: int, event: str, **fields: object) -> str:
    record = {
        "ts": _ts(ms),
        "level": "info",
        "component": "pi-worker",
        "name": "wk",
        "event": event,
        **fields,
    }
    return json.dumps(record, separators=(",", ":"))


def _turn_pair(lines: list[str], message_id: str, start_ms: int, duration_ms: int, outcome: str) -> None:
    lines.append(_worker_line(start_ms, "worker.turn.started", messageId=message_id))
    terminal = {
        "completed": "worker.turn.completed",
        "failed": "worker.turn.failed",
        "interrupted": "worker.turn.interrupted",
    }[outcome]
    lines.append(
        _worker_line(start_ms + duration_ms, terminal, messageId=message_id)
    )


def test_turn_stats_are_split_at_the_latest_generation(tmp_path: Path) -> None:
    """The original prototype bug: a restarted worker had its whole cross-
    restart history counted (14 turns) instead of the current process's 2."""
    log = tmp_path / "pi-wk.jsonl"
    lines: list[str] = []
    old_start = NOW_MS - 8 * 3_600_000
    lines.append(_worker_line(old_start, "worker.started", pid=111))
    for index in range(12):
        _turn_pair(
            lines,
            f"old-{index}",
            old_start + (index + 1) * 60_000,
            30_000,
            "completed",
        )
    # An unclosed turn from the DEAD generation must not leak either.
    lines.append(
        _worker_line(old_start + 900_000, "worker.turn.started", messageId="old-open")
    )
    new_start = NOW_MS - 3_600_000
    lines.append(_worker_line(new_start, "worker.started", pid=222))
    _turn_pair(lines, "new-1", new_start + 60_000, 300_000, "completed")
    _turn_pair(lines, "new-2", new_start + 600_000, 12_000, "failed")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = read_worker_turn_stats(log)

    assert stats is not None
    assert stats.generation_pid == 222
    assert stats.process_started_at_ms == new_start
    assert stats.turn_count == 2
    assert stats.recent_durations_ms == (300_000, 12_000)
    assert stats.recent_outcomes == ("completed", "failed")
    assert stats.last_turn_ended_at_ms == new_start + 612_000
    assert stats.open_turn_started_at_ms is None


def test_open_turn_in_current_generation_is_reported(tmp_path: Path) -> None:
    log = tmp_path / "pi-wk.jsonl"
    start = NOW_MS - 3_600_000
    lines = [
        _worker_line(start, "worker.started", pid=222),
        _worker_line(start + 100_000, "worker.turn.started", messageId="m1"),
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = read_worker_turn_stats(log)

    assert stats is not None
    assert stats.turn_count == 0
    assert stats.open_turn_started_at_ms == start + 100_000
    assert stats.open_turn_stalled_at_ms is None


def test_interrupted_terminal_event_closes_turn_with_distinct_outcome(
    tmp_path: Path,
) -> None:
    log = tmp_path / "codex-wk.jsonl"
    start = NOW_MS - 60_000
    lines = [_worker_line(start, "worker.started", pid=222)]
    _turn_pair(lines, "m1", start + 1_000, 5_000, "interrupted")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = read_worker_turn_stats(log)

    assert stats is not None
    assert stats.turn_count == 1
    assert stats.recent_outcomes == ("interrupted",)
    assert stats.open_turn_started_at_ms is None


def test_stall_events_track_and_clear_the_open_turn(tmp_path: Path) -> None:
    log = tmp_path / "codex-wk.jsonl"
    start = NOW_MS - 3_600_000
    base = [
        _worker_line(start, "worker.started", pid=222),
        _worker_line(start + 100_000, "worker.turn.started", messageId="m1"),
        _worker_line(
            start + 1_100_000,
            "worker.turn.stalled",
            messageId="m1",
            quietSeconds=900.0,
        ),
    ]
    log.write_text("\n".join(base) + "\n", encoding="utf-8")
    stats = read_worker_turn_stats(log)
    assert stats is not None
    assert stats.open_turn_stalled_at_ms == start + 1_100_000

    # Activity resumed: the report is cleared, the turn is just busy again.
    cleared = base + [
        _worker_line(
            start + 1_200_000, "worker.turn.resumed", messageId="m1"
        )
    ]
    log.write_text("\n".join(cleared) + "\n", encoding="utf-8")
    stats = read_worker_turn_stats(log)
    assert stats is not None
    assert stats.open_turn_started_at_ms == start + 100_000
    assert stats.open_turn_stalled_at_ms is None


def test_missing_generation_boundary_distrusts_statistics(tmp_path: Path) -> None:
    log = tmp_path / "pi-wk.jsonl"
    lines: list[str] = []
    _turn_pair(lines, "m1", NOW_MS - 120_000, 30_000, "completed")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_worker_turn_stats(log) is None


def test_window_expands_until_the_boundary_is_found(tmp_path: Path) -> None:
    log = tmp_path / "pi-wk.jsonl"
    lines = [_worker_line(NOW_MS - 3_600_000, "worker.started", pid=222)]
    # Pad with enough turn history that a tiny initial tail misses the
    # boundary and the reader must widen the window to find it.
    for index in range(40):
        _turn_pair(
            lines,
            f"pad-{index}",
            NOW_MS - 3_000_000 + index * 60_000,
            30_000,
            "completed",
        )
    _turn_pair(lines, "last", NOW_MS - 120_000, 30_000, "completed")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = read_worker_turn_stats(log, tail_bytes=256, max_bytes=1_000_000)

    assert stats is not None
    assert stats.generation_pid == 222
    assert stats.turn_count == 41


def test_window_cap_distrusts_statistics(tmp_path: Path) -> None:
    log = tmp_path / "pi-wk.jsonl"
    lines = [_worker_line(NOW_MS - 3_600_000, "worker.started", pid=222)]
    for index in range(40):
        _turn_pair(
            lines,
            f"pad-{index}",
            NOW_MS - 3_000_000 + index * 60_000,
            30_000,
            "completed",
        )
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_worker_turn_stats(log, tail_bytes=256, max_bytes=512) is None


def test_missing_log_file_yields_no_statistics(tmp_path: Path) -> None:
    assert read_worker_turn_stats(tmp_path / "nope.jsonl") is None


# ---------------------------------------------------------------------------
# Snapshot assembly and the daemon IPC handler.
# ---------------------------------------------------------------------------


class _FakeHarnesses:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def status(self) -> tuple[dict[str, object], ...]:
        return self._rows


class _FakePresence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def _application(
    tmp_path: Path, connectors: tuple[dict[str, object], ...]
) -> DaemonApplication:
    state_dir = tmp_path / "state"
    (state_dir / "logs" / "workers").mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _FakeHarnesses(connectors)  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    return app


def _message(message_id: str, recipient: str, created_ms: int) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conv",
        sender="agent:op:node-1:sender",
        recipient=recipient,
        payload=b"{}",
        intent="normal",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_ms,
    )


def test_top_snapshot_handler_contract(tmp_path: Path) -> None:
    connectors = (
        {
            "id": "pi:wk-stuck",
            "runtime": "pi",
            "name": "wk-stuck",
            "running": True,
            "pid": 222,
        },
        {
            "id": "codex:wk-dead",
            "runtime": "codex",
            "name": "wk-dead",
            "running": False,
            "pid": None,
        },
    )
    app = _application(tmp_path, connectors)
    assert app._inbox is not None
    now_ms = int(time.time() * 1000)

    # A live stuck worker: one open turn 14 minutes old, one queued message.
    stuck_log = app.state_dir / "logs" / "workers" / "pi-wk-stuck.jsonl"
    start = now_ms - 3_600_000
    stuck_log.write_text(
        "\n".join(
            [
                _worker_line(start, "worker.started", pid=222),
                _worker_line(
                    now_ms - 14 * 60_000, "worker.turn.started", messageId="m-open"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    app._inbox.receive(
        _message("m-1", "agent:op:node-1:wk-stuck", now_ms - 20 * 60_000),
        now_ms=now_ms - 20 * 60_000,
    )
    # A stranded key: same owner, same actor name, another node.  It must be
    # reported under "stranded" and never joined into the live actor's row.
    app.agents.create("wk-stuck")
    app._inbox.receive(
        _message("m-2", "agent:op:old-node:wk-stuck", now_ms - 60_000),
        now_ms=now_ms - 60_000,
    )

    result = app.handle("top.snapshot", {})

    assert set(result) >= {"daemon", "nowMs", "actors", "stranded"}
    rows = {row["name"]: row for row in result["actors"]}
    stuck = rows["wk-stuck"]
    assert stuck["actor"] == "agent:op:node-1:wk-stuck"
    assert stuck["runtime"] == "pi"
    assert stuck["pid"] == 222
    assert stuck["running"] is True
    assert stuck["state"] == "stuck"
    assert stuck["openTurnStartedAtMs"] == now_ms - 14 * 60_000
    # Queue depth joins on the full URI: the old-node message must not count.
    assert stuck["pendingCount"] == 1
    assert stuck["oldestPendingAgeMs"] == pytest.approx(20 * 60_000, abs=5_000)
    dead = rows["wk-dead"]
    assert dead["state"] == "stopped"
    assert dead["pid"] is None
    assert result["stranded"] == [
        {
            "recipient": "agent:op:old-node:wk-stuck",
            "currentRecipient": "agent:op:node-1:wk-stuck",
            "pending": 1,
        }
    ]


def test_snapshot_distrusts_stats_from_a_different_pid(tmp_path: Path) -> None:
    workers = tmp_path / "logs" / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    (workers / "pi-wk.jsonl").write_text(
        "\n".join(
            [
                _worker_line(NOW_MS - 3_600_000, "worker.started", pid=222),
                _worker_line(
                    NOW_MS - 3_000_000, "worker.turn.started", messageId="m1"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_top_snapshot(
        state_dir=tmp_path,
        owner="op",
        node_id="node-1",
        epoch="e",
        daemon_pid=1,
        connectors=(
            {
                "id": "pi:wk",
                "runtime": "pi",
                "name": "wk",
                "running": True,
                "pid": 999,
            },
        ),
        pending={},
        stranded=[],
        now_ms=NOW_MS,
    )

    row = snapshot["actors"][0]
    # The log's generation (pid 222) is not the supervised child (pid 999):
    # its unclosed turn must not brand the live process as stuck.
    assert row["openTurnStartedAtMs"] is None
    assert row["turnCount"] is None
    assert row["state"] == "ok"


def test_claude_interactive_row_uses_fenced_daemon_turn_stats(tmp_path: Path) -> None:
    actor = "agent:op:node-1:claude-worker"
    snapshot = build_top_snapshot(
        state_dir=tmp_path,
        owner="op",
        node_id="node-1",
        epoch="e",
        daemon_pid=1,
        connectors=(),
        actor_statuses=(
            {
                "actor": actor,
                "name": "claude-worker",
                "runtime": "claude_interactive",
                "processState": "not_applicable",
                "status": "online",
                "sources": ["interactive"],
            },
        ),
        interactive_turn_stats={
            actor: InteractiveTurnStats(
                session_ref="session-1",
                counting_since_ms=NOW_MS - 600_000,
                turn_count=3,
                last_turn_ended_at_ms=NOW_MS - 65_000,
            )
        },
        pending={},
        stranded=[],
        now_ms=NOW_MS,
    )

    row = snapshot["actors"][0]
    assert row["turnCount"] == 3
    assert row["lastTurnEndedAtMs"] == NOW_MS - 65_000
    assert row["turnCountSinceMs"] == NOW_MS - 600_000
    assert "turnStatsUnavailableReason" not in row
    assert row["recentTurnDurationsMs"] is None
    assert row["recentTurnOutcomes"] is None

    rendered = _render_top(snapshot, wide=False, sampler=lambda: {})
    table_row = next(line for line in rendered.splitlines() if "claude-worker" in line)
    assert " 3 " in table_row
    assert " 1m " in table_row


def test_claude_interactive_row_explains_unavailable_stats(tmp_path: Path) -> None:
    snapshot = build_top_snapshot(
        state_dir=tmp_path,
        owner="op",
        node_id="node-1",
        epoch="e",
        daemon_pid=1,
        connectors=(),
        actor_statuses=(
            {
                "actor": "agent:op:node-1:claude-worker",
                "name": "claude-worker",
                "runtime": "claude_interactive",
                "processState": "not_applicable",
                "status": "online",
                "sources": ["interactive"],
            },
        ),
        pending={},
        stranded=[],
        now_ms=NOW_MS,
    )

    row = snapshot["actors"][0]
    assert row["turnCount"] is None
    assert row["lastTurnEndedAtMs"] is None
    assert row["turnStatsUnavailableReason"] == "no-reporting-session-this-generation"


def test_interactive_turn_stats_do_not_change_pi_row(tmp_path: Path) -> None:
    actor_status = {
        "actor": "agent:op:node-1:pi-worker",
        "name": "pi-worker",
        "runtime": "pi",
        "processState": "stopped",
        "status": "offline",
        "running": False,
        "sources": ["worker"],
    }
    common = dict(
        state_dir=tmp_path,
        owner="op",
        node_id="node-1",
        epoch="e",
        daemon_pid=1,
        connectors=(),
        actor_statuses=(actor_status,),
        pending={},
        stranded=[],
        now_ms=NOW_MS,
    )

    before = build_top_snapshot(**common)
    after = build_top_snapshot(
        **common,
        interactive_turn_stats={
            "agent:op:node-1:someone-else": InteractiveTurnStats(
                session_ref="session-1",
                counting_since_ms=NOW_MS,
                turn_count=1,
                last_turn_ended_at_ms=NOW_MS,
            )
        },
    )

    assert after["actors"][0] == before["actors"][0]


# ---------------------------------------------------------------------------
# Inbox per-recipient statistics.
# ---------------------------------------------------------------------------


def test_pending_recipient_stats_report_depth_and_oldest(tmp_path: Path) -> None:
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    inbox.receive(_message("a1", "agent:op:node:a", 1000), now_ms=1000)
    inbox.receive(_message("a2", "agent:op:node:a", 2000), now_ms=2000)
    inbox.receive(_message("b1", "agent:op:node:b", 5000), now_ms=5000)
    inbox.ack("agent:op:node:b", "b1")

    stats = {
        recipient: (count, oldest)
        for recipient, count, oldest in inbox.pending_recipient_stats()
    }

    assert stats == {"agent:op:node:a": (2, 1000)}
