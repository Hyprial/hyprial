"""Channel poll/heartbeat/daemon loops and parent watch."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any
from pathlib import Path

import anyio

from hyprial.kernel import (
    CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
)

from hyprial.daemon.impl.inbox_watch import ListInbox, Ok, Quiet, Rejected, Sleep, Unreachable, Wait
from hyprial.daemon.impl.mcp.api  import (
    SESSION_SUPERSEDED_CODE,
    DaemonDisconnected,
    DaemonRequestRejected,
)
from hyprial.daemon.impl.mcp.channel.adapter import ClaudeChannelAdapter
from hyprial.daemon.impl.mcp.channel.ownership import (
    _DAEMON_CONTACT_ERRORS,
    _OWNER_IDENTITY_MISMATCH_GRACE_SECONDS,
    _OwnerProcessStatus,
    _owner_process_status,
    _read_process_identity,
)

_logger = logging.getLogger(__name__)

def signal_channel_recovery(path: Path) -> None:
    """Atomically coalesce one Claude lifecycle recovery pulse on disk."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)

def _consume_channel_recovery(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True

def _pending_turn_signals(
    path: Path | None, *, limit: int = 1000
) -> tuple[Path, ...]:
    if path is None:
        return ()
    try:
        entries = sorted(item for item in path.iterdir() if item.is_file())
    except FileNotFoundError:
        return ()
    return tuple(entries[:limit])

async def _run_channel_poll_loop(
    adapter: ClaudeChannelAdapter,
    *,
    poll_interval: float,
    recovery_signal: Path | None = None,
    turn_signal_dir: Path | None = None,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> None:
    """Drive the inbox-watch poll lane; never crash the child.

    What to do next -- register, refresh after a daemon restart, list, hold
    on ``message.pending.wait``, sleep, or go quiet -- is decided by the shared
    core (``hyprial.daemon.impl.inbox_watch``, contract/inbox-watch); this
    loop performs it and turns each list answer into wakes.  The rules that
    used to live here are now golden cases there, among them: a refused
    refresh never stops polling (G9, the 0.5.0 silence), a supersede verdict
    goes quiet without re-registering (G10, the delivery-flap steal), and a
    restart seen first by the heartbeat still re-wakes the backlog (G8).

    Going quiet ends this task only.  Whether the child exits is decided by
    parent-CC liveness (``_watch_parent``), never by the daemon's verdict.
    """

    watch = adapter.watch
    # The loop owns the cadence: base interval for lists and backoff.
    watch.poll_interval_ms = max(1, int(poll_interval * 1000))
    failures = 0
    while True:
        if _consume_channel_recovery(recovery_signal):
            # SessionStart (startup/resume/clear/compact) or a failed model
            # turn: re-notify the backlog; reply/ack remains mandatory.
            watch.request_rewake("recovery")
        action = watch.poll_next(_now_ms())
        if isinstance(action, Quiet):
            _logger.info(
                "harness channel for actor %s was superseded by a newer "
                "session; going quiet (no re-register)",
                adapter.actor,
            )
            return
        if isinstance(action, Sleep):
            await anyio.sleep(action.ms / 1000)
            continue
        outcome = await _perform_settled(
            adapter, action, lambda: watch.poll_done(action, Unreachable(), _now_ms())
        )
        change = watch.poll_done(action, outcome, _now_ms())
        if isinstance(outcome, Ok):
            if not isinstance(action, Wait):
                failures = 0
            if watch.quiet:
                # Another lane (or a tool) learned SUPERSEDED while this list
                # was in flight: nothing more is applied for this session.
                continue
            if isinstance(action, ListInbox):
                try:
                    await adapter.apply(change, dict(outcome.response))
                except _DAEMON_CONTACT_ERRORS as error:
                    # A response that cannot be applied at all (messages not
                    # a list) must not kill the poll task -- and with it the
                    # heartbeat and the child (review 841 on #1152).  A
                    # re-wake the core handed over with it is owed again.
                    if change is not None and change.rewake_reason is not None:
                        # Owed again, at the ordinary pace: re-listing at
                        # once would spin on a daemon that keeps answering
                        # the same unusable list (review 848).
                        watch.request_rewake(change.rewake_reason, now=False)
                    failures += 1
                    _report_poll_failure(error, failures, on_error)
                await _forward_turn_signals(adapter, turn_signal_dir)
            continue
        if isinstance(action, Wait):
            # The doorbell only decides when the next list runs.
            _logger.debug(
                "harness channel wait for actor %s failed (%s); polling",
                adapter.actor,
                outcome,
            )
            continue
        failures += 1
        _report_poll_failure(_outcome_error(action, outcome), failures, on_error)


async def _forward_turn_signals(
    adapter: ClaudeChannelAdapter, turn_signal_dir: Path | None
) -> None:
    turn_signals = _pending_turn_signals(turn_signal_dir)
    if not turn_signals:
        return
    try:
        await adapter.report_turns(tuple(signal.name for signal in turn_signals))
    except DaemonRequestRejected as error:
        if error.code == SESSION_SUPERSEDED_CODE:
            # Same fence, same verdict: both lanes go quiet.
            adapter.watch.observe_rejection(error.code)
            return
        # Turn counts are telemetry: a rejected report (an older daemon
        # without session.turn.ended, a runtime mismatch) must not mark
        # delivery as failed.  The pulses stay on disk and are retried.
        _logger.debug(
            "turn report for actor %s rejected: %s",
            getattr(adapter, "actor", "?"),
            error,
        )
    except _DAEMON_CONTACT_ERRORS as error:
        _logger.debug("turn report for actor %s failed: %s", adapter.actor, error)
    else:
        for signal in turn_signals:
            signal.unlink(missing_ok=True)


async def _run_channel_heartbeat_loop(
    adapter: ClaudeChannelAdapter,
    *,
    heartbeat_interval: float = CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> None:
    """Drive the heartbeat lane: liveness independent of polling and stdio.

    It may also perform an owed refresh while the poll lane holds a wait, so
    liveness never waits out a doorbell hold; the core keeps that refresh
    single-flight across the two lanes.
    """

    if heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be positive")
    watch = adapter.watch
    watch.heartbeat_interval_ms = max(1, int(heartbeat_interval * 1000))
    failures = 0
    while True:
        action = watch.heartbeat_next(_now_ms())
        if isinstance(action, Quiet):
            # Whichever request got the verdict has already reported it.
            return
        if isinstance(action, Sleep):
            await anyio.sleep(action.ms / 1000)
            continue
        outcome = await _perform_settled(
            adapter, action, lambda: watch.heartbeat_done(action, Unreachable(), _now_ms())
        )
        watch.heartbeat_done(action, outcome, _now_ms())
        if isinstance(outcome, Ok):
            failures = 0
            continue
        failures += 1
        _report_poll_failure(_outcome_error(action, outcome), failures, on_error)


async def _perform_settled(
    adapter: ClaudeChannelAdapter, action: Any, abandon: Callable[[], object]
) -> Any:
    """Perform one request; if the task is cancelled mid-request, settle it
    as unreachable first, so the core never keeps an orphaned request."""

    try:
        return await adapter.perform(action)
    except BaseException:
        abandon()
        raise


def _outcome_error(action: object, outcome: object) -> BaseException:
    op = getattr(action, "op", "request")
    if isinstance(outcome, Rejected):
        return DaemonRequestRejected(outcome.code, f"{op} refused")
    return DaemonDisconnected(f"{op}: daemon unreachable")


def _now_ms() -> int:
    return int(time.monotonic() * 1000)

async def _run_channel_daemon_loops(
    adapter: ClaudeChannelAdapter,
    *,
    poll_interval: float,
    recovery_signal: Path | None = None,
    turn_signal_dir: Path | None = None,
    heartbeat_interval: float = CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> None:
    """Run polling and heartbeat as sibling tasks with independent backpressure."""

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,
                poll_interval=poll_interval,
                recovery_signal=recovery_signal,
                turn_signal_dir=turn_signal_dir,
                on_error=on_error,
            )
        )
        tasks.start_soon(
            lambda: _run_channel_heartbeat_loop(
                adapter,
                heartbeat_interval=heartbeat_interval,
                on_error=on_error,
            )
        )

def _report_poll_failure(
    error: BaseException,
    failures: int,
    on_error: Callable[[BaseException, int], None] | None,
) -> None:
    if on_error is not None:
        on_error(error, failures)
    else:
        _logger.warning(
            "harness channel daemon contact failed (attempt %d); the stdio "
            "child stays up and refreshes its fenced lease on reconnect: %s",
            failures,
            error,
        )

async def _watch_parent(
    *,
    getppid: Callable[[], int],
    poll_interval: float,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    signal_process: Callable[[int, int], None] = os.kill,
    read_identity: Callable[[int], str | None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Return ``True`` once the parent Claude Code process dies (orphaning).

    The stdio child does *not* reliably see EOF when Claude Code exits: a real
    pipe's write end can be held open by an inherited fd (a sibling MCP child of
    the same Claude Code session), and empirically the orphaned child then
    lingers forever -- exactly the accumulation this reaper exists to prevent.
    Managed launches pass the long-lived launcher PID plus its process-birth
    marker.  That owner exists for exactly the Claude wait and remains
    observable even if this child starts only after it has been reparented; the
    marker fences PID reuse.  A direct ``hyprial mcp claude-channel`` launch remains
    compatible by falling back to ``os.getppid()`` drift.  Both paths key on
    process liveness rather than registration recency, so supersede alone never
    tears down a child whose owner is still alive.
    """

    if (owner_pid is None) != (owner_identity is None):
        raise ValueError("channel owner pid and identity must be provided together")
    resolved_read_identity = (
        _read_process_identity if read_identity is None else read_identity
    )
    if owner_pid is not None and owner_identity is not None:
        mismatch_since: float | None = None
        while True:
            status = _owner_process_status(
                owner_pid,
                owner_identity,
                signal_process=signal_process,
                read_identity=resolved_read_identity,
            )
            if status is _OwnerProcessStatus.PID_MISSING:
                _logger.warning(
                    "harness channel owner fence failed reason=%s pid=%d; "
                    "shutting down the orphaned stdio child",
                    status,
                    owner_pid,
                )
                return True
            if status is _OwnerProcessStatus.IDENTITY_MISMATCH:
                now = monotonic()
                if mismatch_since is None:
                    mismatch_since = now
                mismatch_age = max(0.0, now - mismatch_since)
                if mismatch_age >= _OWNER_IDENTITY_MISMATCH_GRACE_SECONDS:
                    _logger.warning(
                        "harness channel owner fence failed reason=%s pid=%d "
                        "durationMs=%d; shutting down the orphaned stdio child",
                        status,
                        owner_pid,
                        int(mismatch_age * 1000),
                    )
                    return True
            else:
                # A matching identity or an unreadable/permission-denied probe
                # breaks the continuous mismatch window. UNKNOWN is fail-safe:
                # an observation gap must never advance a possibly-live owner
                # toward reaping.
                mismatch_since = None
            await anyio.sleep(poll_interval)

    original_ppid = getppid()
    if original_ppid <= 1:
        _logger.info(
            "harness channel started after its parent process had already "
            "exited (ppid %d); shutting down the orphaned stdio child",
            original_ppid,
        )
        return True
    while True:
        await anyio.sleep(poll_interval)
        if getppid() != original_ppid:
            _logger.info(
                "harness channel parent process exited (ppid %d -> %d); shutting "
                "down the orphaned stdio child",
                original_ppid,
                getppid(),
            )
            return True
