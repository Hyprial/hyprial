"""Per-actor health projection behind ``top.snapshot`` / ``hyprial top``.

The question this module answers is "is this agent actually doing work", not
"is the process alive". Actor membership and liveness arrive from
``hyprial.status``; this module enriches each shared row with top-specific evidence:

- the supervisor's live process table (runtime, running, pid),
- the worker JSONL log written by the daemon-side turn pump, parsed from the
  tail and split at the latest ``worker.started`` so turn statistics always
  describe the CURRENT process generation, never a restarted ancestor,
- the durable inbox, summarized per full canonical recipient URI (same short
  name under another node is a different agent and is reported separately as
  a stranded key).

Classification (stuck/spin/stalled/backlog/idle/ok) is computed here so the
verdict is identical for the CLI, tests, and any other IPC consumer; the CLI
only adds process sampling (``ps``) and rendering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hyprial.log import route_path

from hyprial.uri import canonical_agent_uri, short_actor_name

JsonObject = dict[str, Any]

# Read the worker log tail only: worker logs have no rotation, so a full scan
# would grow unboundedly (the daemon log already reached 18 MiB once).  The
# window doubles until the current generation's ``worker.started`` line is
# found, the file start is reached, or the cap is hit.
WORKER_LOG_TAIL_BYTES = 64 * 1024
WORKER_LOG_MAX_BYTES = 4 * 1024 * 1024

# Turn wall-clock caps are retired (#277: liveness moves to connector-side
# steer probing; no timeout kills a turn).  Runtimes listed here report
# quiet turns themselves (worker.turn.stalled / worker.turn.resumed), so a
# long open turn is judged by those events instead of by age -- a 2h turn
# with activity is healthy work.  Runtimes without a truthful activity
# source keep the age-based "stuck" heuristic below.
STALL_REPORTING_RUNTIMES = frozenset({"codex"})

_TURN_STARTED = "worker.turn.started"
_TURN_STALLED = "worker.turn.stalled"
_TURN_RESUMED = "worker.turn.resumed"
_TURN_TERMINAL_OUTCOMES: dict[str, str] = {
    "worker.turn.completed": "completed",
    "worker.turn.failed": "failed",
    "worker.turn.interrupted": "interrupted",
}
_WORKER_STARTED = "worker.started"

RECENT_TURN_WINDOW = 5


@dataclass(frozen=True, slots=True)
class TopThresholds:
    """Classification thresholds, injectable for tests."""

    # An open turn older than this is stuck -- but only for runtimes
    # without their own stall reporting; a stall-reporting runtime's open
    # turn is judged by worker.turn.stalled/resumed events, never by age.
    stuck_ms: int = 600_000
    # SPIN: the last `spin_window` turns averaged under `spin_mean_ms` and
    # more than half of them failed (the 4-8s failed-turn incident).
    spin_window: int = RECENT_TURN_WINDOW
    spin_mean_ms: int = 15_000
    spin_failure_ratio: float = 0.5
    # BACKLOG: messages are pending while the actor has been idle this long.
    backlog_idle_ms: int = 300_000
    # Past this much quiet time a running, unqueued actor is reported as
    # "idle" (normal standby) instead of "ok".
    idle_ms: int = 1_800_000


DEFAULT_THRESHOLDS = TopThresholds()


@dataclass(frozen=True, slots=True)
class WorkerTurnStats:
    """Turn statistics for exactly one worker process generation."""

    generation_pid: int | None
    process_started_at_ms: int | None
    turn_count: int
    last_turn_ended_at_ms: int | None
    recent_durations_ms: tuple[int, ...]
    recent_outcomes: tuple[str, ...]
    open_turn_started_at_ms: int | None
    # Timestamp of the open turn's last worker.turn.stalled that no
    # worker.turn.resumed (or terminal event) has cleared; None otherwise.
    open_turn_stalled_at_ms: int | None = None


def _timestamp_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The shared Logger always writes UTC millisecond timestamps.
    return int(parsed.timestamp() * 1000)


def _parse_event(line: bytes) -> JsonObject | None:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_generation_window(path: Path, tail_bytes: int, max_bytes: int) -> list[bytes] | None:
    """Return the log lines of the latest worker generation, or None.

    ``None`` means the generation boundary could not be established (no
    ``worker.started`` within the read cap); callers must then distrust any
    turn statistics rather than risk mixing generations.
    """

    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    window = min(tail_bytes, size)
    while True:
        with path.open("rb") as stream:
            stream.seek(size - window)
            data = stream.read()
        if size - window > 0:
            # Drop the leading partial line of a mid-file window.
            _, _, data = data.partition(b"\n")
        lines = [line for line in data.splitlines() if line.strip()]
        boundary = None
        for index in range(len(lines) - 1, -1, -1):
            if b'"worker.started"' not in lines[index]:
                continue
            event = _parse_event(lines[index])
            if event is not None and event.get("event") == _WORKER_STARTED:
                boundary = index
                break
        if boundary is not None:
            return lines[boundary:]
        if window >= size:
            # The whole file is in memory and records no generation at all.
            return None
        if window >= max_bytes:
            return None
        window = min(window * 2, size, max_bytes)


def read_worker_turn_stats(
    path: Path,
    *,
    tail_bytes: int = WORKER_LOG_TAIL_BYTES,
    max_bytes: int = WORKER_LOG_MAX_BYTES,
) -> WorkerTurnStats | None:
    """Summarize the latest worker generation from its JSONL log tail.

    Turn events are paired by ``messageId``.  Anything before the last
    ``worker.started`` belongs to a previous (dead) process and is ignored,
    which is what keeps cross-restart history out of the counters.
    """

    lines = _read_generation_window(Path(path), tail_bytes, max_bytes)
    if lines is None:
        return None
    generation_pid: int | None = None
    process_started_at_ms: int | None = None
    open_turns: dict[str, int] = {}
    stalled_at: dict[str, int] = {}
    durations: list[int] = []
    outcomes: list[str] = []
    last_ended_at_ms: int | None = None
    for line in lines:
        event = _parse_event(line)
        if event is None:
            continue
        kind = event.get("event")
        if kind == _WORKER_STARTED:
            pid = event.get("pid")
            generation_pid = pid if isinstance(pid, int) else None
            process_started_at_ms = _timestamp_ms(event.get("ts"))
            continue
        message_id = event.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            continue
        at_ms = _timestamp_ms(event.get("ts"))
        if at_ms is None:
            continue
        if kind == _TURN_STARTED:
            open_turns[message_id] = at_ms
            continue
        if kind == _TURN_STALLED:
            stalled_at[message_id] = at_ms
            continue
        if kind == _TURN_RESUMED:
            stalled_at.pop(message_id, None)
            continue
        outcome = _TURN_TERMINAL_OUTCOMES.get(str(kind))
        if outcome is None:
            continue
        stalled_at.pop(message_id, None)
        started_at_ms = open_turns.pop(message_id, None)
        if started_at_ms is None:
            # A terminal event whose start fell outside the window (or a
            # previous generation) cannot yield a trustworthy duration.
            continue
        duration = at_ms - started_at_ms
        if duration < 0:
            continue
        durations.append(duration)
        outcomes.append(outcome)
        last_ended_at_ms = at_ms
    open_turn_id = max(open_turns, key=open_turns.__getitem__, default=None)
    return WorkerTurnStats(
        generation_pid=generation_pid,
        process_started_at_ms=process_started_at_ms,
        turn_count=len(durations),
        last_turn_ended_at_ms=last_ended_at_ms,
        recent_durations_ms=tuple(durations[-RECENT_TURN_WINDOW:]),
        recent_outcomes=tuple(outcomes[-RECENT_TURN_WINDOW:]),
        open_turn_started_at_ms=max(open_turns.values(), default=None),
        open_turn_stalled_at_ms=(
            stalled_at.get(open_turn_id) if open_turn_id is not None else None
        ),
    )


def classify_actor(
    *,
    running: bool,
    runtime: str,
    pending_count: int,
    now_ms: int,
    stats: WorkerTurnStats | None,
    thresholds: TopThresholds = DEFAULT_THRESHOLDS,
) -> tuple[str, JsonObject]:
    """Reduce one actor's evidence to a single operator-facing verdict."""

    if not running:
        return "stopped", {}
    open_started_at_ms = stats.open_turn_started_at_ms if stats is not None else None
    if open_started_at_ms is not None:
        open_ms = now_ms - open_started_at_ms
        stalled_at_ms = (
            stats.open_turn_stalled_at_ms if stats is not None else None
        )
        if stalled_at_ms is not None:
            # The worker itself reported the turn quiet and no activity has
            # cleared it -- the operator-facing signal that replaced the
            # retired wall-clock cap (#277).
            return "stalled", {
                "openMs": open_ms,
                "stalledForMs": max(now_ms - stalled_at_ms, 0),
            }
        if (
            runtime not in STALL_REPORTING_RUNTIMES
            and open_ms >= thresholds.stuck_ms
        ):
            return "stuck", {"openMs": open_ms}
        # A live turn is healthy work whatever its age, whatever the queue.
        return "ok", {"busy": True}
    durations = stats.recent_durations_ms if stats is not None else ()
    outcomes = stats.recent_outcomes if stats is not None else ()
    if len(durations) >= thresholds.spin_window:
        window_durations = durations[-thresholds.spin_window :]
        window_outcomes = outcomes[-thresholds.spin_window :]
        mean_ms = sum(window_durations) / len(window_durations)
        failures = sum(1 for outcome in window_outcomes if outcome == "failed")
        if (
            mean_ms < thresholds.spin_mean_ms
            and failures / len(window_outcomes) > thresholds.spin_failure_ratio
        ):
            return "spin", {
                "meanMs": int(mean_ms),
                "failures": failures,
                "window": len(window_outcomes),
            }
    last_activity_ms: int | None = None
    if stats is not None:
        last_activity_ms = stats.last_turn_ended_at_ms or stats.process_started_at_ms
    idle_for_ms = now_ms - last_activity_ms if last_activity_ms is not None else None
    if pending_count > 0:
        if idle_for_ms is not None and idle_for_ms >= thresholds.backlog_idle_ms:
            return "backlog", {"idleMs": idle_for_ms, "pending": pending_count}
        return "ok", {}
    if stats is not None and (
        stats.turn_count == 0
        or (idle_for_ms is not None and idle_for_ms >= thresholds.idle_ms)
    ):
        return "idle", {}
    return "ok", {}


def build_actor_row(
    *,
    state_dir: Path,
    owner: str,
    node_id: str,
    status: JsonObject,
    pending: tuple[int, int | None],
    now_ms: int,
    thresholds: TopThresholds = DEFAULT_THRESHOLDS,
) -> JsonObject:
    """Enrich one shared actor-status row for ``top.snapshot``."""

    raw_actor = status.get("actor")
    actor = (
        raw_actor
        if isinstance(raw_actor, str)
        else canonical_agent_uri(owner, node_id, str(status.get("name")))
    )
    raw_name = status.get("name")
    name = raw_name if isinstance(raw_name, str) else short_actor_name(actor)
    raw_runtime = status.get("runtime")
    runtime = raw_runtime if isinstance(raw_runtime, str) else None
    process_state = status.get("processState")
    if process_state not in {"running", "stopped", "not_applicable"}:
        process_state = "running" if status.get("running") is True else "stopped"
    running = (
        status.get("running") is True
        if process_state != "not_applicable"
        else None
    )
    pid = status.get("pid")
    pid = pid if isinstance(pid, int) else None
    stats: WorkerTurnStats | None = None
    if process_state != "not_applicable" and runtime is not None:
        log_path = route_path(
            state_dir,
            route="worker",
            component=f"{runtime}-worker",
            name=name,
            runtime=runtime,
        )
        stats = read_worker_turn_stats(log_path)
    if (
        stats is not None
        and pid is not None
        and stats.generation_pid is not None
        and stats.generation_pid != pid
    ):
        # The log's latest generation is not the supervised child (startup
        # race or a launch that never logged).  Showing the dead process's
        # turns as this process's health is exactly the failure mode this
        # command exists to kill, so drop the statistics instead.
        stats = None
    pending_count, oldest_pending_at_ms = pending
    if process_state == "not_applicable":
        state = str(status.get("status", "offline"))
        detail: JsonObject = {}
    else:
        state, detail = classify_actor(
            running=running is True,
            runtime=runtime or "",
            pending_count=pending_count,
            now_ms=now_ms,
            stats=stats,
            thresholds=thresholds,
        )
    row: JsonObject = {
        "actor": actor,
        "name": name,
        "runtime": runtime,
        "status": status.get("status", "online" if running else "offline"),
        "processState": process_state,
        "sources": list(status.get("sources", [])),
        "running": running,
        "pid": pid,
        "processStartedAtMs": (
            stats.process_started_at_ms if stats is not None else None
        ),
        "turnCount": stats.turn_count if stats is not None else None,
        "lastTurnEndedAtMs": (
            stats.last_turn_ended_at_ms if stats is not None else None
        ),
        "recentTurnDurationsMs": (
            list(stats.recent_durations_ms) if stats is not None else None
        ),
        "recentTurnOutcomes": (
            list(stats.recent_outcomes) if stats is not None else None
        ),
        "recentFailureRatio": (
            (
                sum(1 for outcome in stats.recent_outcomes if outcome == "failed")
                / len(stats.recent_outcomes)
            )
            if stats is not None and stats.recent_outcomes
            else None
        ),
        "openTurnStartedAtMs": (
            stats.open_turn_started_at_ms if stats is not None else None
        ),
        "pendingCount": pending_count,
        "oldestPendingAgeMs": (
            (now_ms - oldest_pending_at_ms)
            if oldest_pending_at_ms is not None
            else None
        ),
        "state": state,
    }
    if detail:
        row["stateDetail"] = detail
    error = status.get("error")
    if isinstance(error, str) and error:
        row["error"] = error
    return row


def build_top_snapshot(
    *,
    state_dir: Path,
    owner: str,
    node_id: str,
    epoch: str,
    daemon_pid: int,
    connectors: tuple[JsonObject, ...] | list[JsonObject],
    pending: dict[str, tuple[int, int | None]],
    stranded: list[JsonObject],
    now_ms: int,
    thresholds: TopThresholds = DEFAULT_THRESHOLDS,
    actor_statuses: tuple[JsonObject, ...] | list[JsonObject] | None = None,
    dispatch_without_pac_count: int = 0,
    dispatch_conversation_count: int = 0,
) -> JsonObject:
    """The full ``top.snapshot`` IPC result."""

    statuses = actor_statuses if actor_statuses is not None else connectors
    actors = [
        build_actor_row(
            state_dir=state_dir,
            owner=owner,
            node_id=node_id,
            status=status,
            # Queue depth joins on the full canonical URI: same short name
            # under another node is a different agent (a stranded key).
            pending=pending.get(
                (
                    status["actor"]
                    if isinstance(status.get("actor"), str)
                    else canonical_agent_uri(
                        owner, node_id, str(status.get("name"))
                    )
                ),
                (0, None),
            ),
            now_ms=now_ms,
            thresholds=thresholds,
        )
        for status in statuses
    ]
    return {
        "daemon": {
            "pid": daemon_pid,
            "nodeId": node_id,
            "owner": owner,
            "epoch": epoch,
            "dispatchWithoutPacCount": dispatch_without_pac_count,
            "dispatchConversationCount": dispatch_conversation_count,
        },
        "nowMs": now_ms,
        "actors": actors,
        "stranded": list(stranded),
    }
