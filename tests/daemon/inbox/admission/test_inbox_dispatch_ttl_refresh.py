"""Inbox hold TTL must follow accepted work while its worker stays live.

The defect: ``expires_at_ms`` was stamped once in ``InboxService.receive``
and never touched again. Once #273 raised the turn wall-clock cap past the
default hold TTL (and #279 retired the cap entirely), a turn
legitimately running longer than the TTL got its inbox row pruned mid-flight;
when the turn finished, ``DaemonEventBridge._complete_harness_results`` looked
the row up by id, found nothing (``original is None``), and silently
discarded the completed result — no reply, no ack, no log at that call site.

Two fast tests pin the original #276 wiring:
  - dispatch acceptance refreshes the hold (once — enqueue's own dedup means
    a blind re-dispatch on a later tick must not re-trigger it)
  - worker progress activity refreshes the hold

The acceptance tests use one deterministic clock shared by the inbox, the
runtime, and the fake worker. They cover a delivery queued behind a long turn,
refresh throttling, worker removal, dispatch refusal, and the original
progress-driven refresh behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    HoldPolicy,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.tracking.progress import PHASE_TOOL_CALL, ProgressEvent
from tests.support.links.gate import permissive_admission_slot

OWNER = "op"
NODE = "node-ttl"
WORKER = f"agent:{OWNER}:{NODE}:worker"
SENDER = f"agent:{OWNER}:{NODE}:requester"


class _Transport:
    def close(self) -> None:
        return None


def _delivery_message(message_id: str = "delivery-1") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender=SENDER,
        recipient=WORKER,
        payload=b"{}",
        # "reply" intent takes the direct-ack branch in
        # _complete_harness_results, isolating the exact `original is None`
        # defect line from the separate reply-submission path.
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


class _SpyInbox:
    """Minimal InboxPort-shaped fake that records refresh_hold calls."""

    def __init__(self, pending: tuple[InboxMessage, ...]) -> None:
        self._pending = list(pending)
        self.refreshed: list[str] = []

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        return tuple(m for m in self._pending if m.recipient == recipient)

    def retry_due(self, *, now_ms: int | None = None) -> list[object]:
        return []

    def prune_inbox(self, *, now_ms: int | None = None) -> tuple[object, ...]:
        return ()

    def refresh_hold(self, message_id: str, *, now_ms: int | None = None) -> bool:
        self.refreshed.append(message_id)
        return True

    def submit_progress_event(self, event: object, *, recipient: str) -> bool:
        return True

    def close(self) -> None:
        return None


class _OneShotProcess:
    """A fake StreamingHarnessProcess: dedups enqueue like the real one
    (StreamingHarnessProcess._known), so a re-dispatch attempt on a later
    tick genuinely returns False the way it does in production.

    Deliberately has no ``drain_progress`` — ``ManagedHarnessRuntime`` treats it
    as a duck-typed optional seam (``getattr(process, "drain_progress",
    None)``) and skips processes without one, which is exactly what the
    dispatch-only wiring test needs to isolate that refresh path from the
    progress-activity one.
    """

    running = True
    pid = None

    def __init__(self) -> None:
        self.enqueued: list[HarnessDelivery] = []
        self._known: set[str] = set()

    def stop(self) -> None:
        self.running = False

    def enqueue(self, delivery: HarnessDelivery) -> bool:
        identities = delivery.message_ids or (delivery.delivery_id,)
        if any(identity in self._known for identity in identities):
            return False
        self._known.update(identities)
        self.enqueued.append(delivery)
        return True

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return ()

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        return False


class _ProgressEmittingProcess(_OneShotProcess):
    """Adds one mid-flight progress event, sent once per delivery."""

    def __init__(self) -> None:
        super().__init__()
        self._progress_sent = False

    def drain_progress(self) -> tuple[ProgressEvent, ...]:
        if self._progress_sent or not self.enqueued:
            return ()
        self._progress_sent = True
        delivery = self.enqueued[-1]
        return tuple(
            ProgressEvent(
                delivery_id=member,
                conversation_id=delivery.conversation_id,
                actor=WORKER,
                harness="claude",
                phase=PHASE_TOOL_CALL,
                seq=index,
                emitted_at_ms=1,
                summary="still working",
                tool_call_id="call-1",
                tool_name="slow_tool",
            )
            for index, member in enumerate(
                delivery.message_ids or (delivery.delivery_id,), start=1
            )
        )


class _Launcher:
    def __init__(self, process: object) -> None:
        self._process = process

    def start(self, spec: HarnessLaunchSpec) -> object:
        return self._process


def _wired_runtime(
    tmp_path: Path, *, inbox: object, process: object, clock: _Clock | None = None
) -> DaemonEventBridge:
    harnesses = ManagedHarnessRuntime(_Launcher(process))  # type: ignore[arg-type]
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,
        harness_actor_uri=lambda name: WORKER,
        clock_ms=None if clock is None else clock.ms,
        hold_ttl_ms=getattr(inbox, "durable_ttl_ms", DEFAULT_HOLD_TTL_MS),
    )
    runtime.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="claude", name="worker", headless=True)
    )
    harnesses.restore(runtime.desired_state.load().harnesses)
    return runtime


# --- wiring (fast, mutation-verified) -----------------------------------------


def test_dispatch_acceptance_refreshes_hold_exactly_once(tmp_path: Path) -> None:
    process = _OneShotProcess()
    inbox = _SpyInbox((_delivery_message(),))
    runtime = _wired_runtime(tmp_path, inbox=inbox, process=process)
    runtime.start()
    try:
        runtime.on_timer()
        assert len(process.enqueued) == 1
        assert inbox.refreshed == ["delivery-1"]

        # enqueue's own dedup makes dispatch() return False the second time;
        # a blind per-tick re-dispatch attempt must not double-refresh.
        runtime.on_timer()
        assert inbox.refreshed == ["delivery-1"]
    finally:
        runtime.stop()


def test_progress_activity_refreshes_hold(tmp_path: Path, clock: _Clock) -> None:
    process = _ProgressEmittingProcess()
    inbox = _SpyInbox((_delivery_message(),))
    runtime = _wired_runtime(tmp_path, inbox=inbox, process=process, clock=clock)
    runtime.start()
    try:
        runtime.on_timer()  # dispatch: refresh #1
        assert inbox.refreshed == ["delivery-1"]

        clock.advance(DEFAULT_HOLD_TTL_MS / 2_000)
        runtime.on_timer()  # drains the one progress event: refresh #2
        assert inbox.refreshed == ["delivery-1", "delivery-1"]
    finally:
        runtime.stop()


# --- acceptance scenarios (one deterministic clock) ---------------------------


class _Clock:
    """One clock for the whole scenario: inbox stamps/prune, runtime, worker."""

    def __init__(self) -> None:
        self.now_ms = 1_000_000

    def ms(self) -> int:
        return self.now_ms

    def seconds(self) -> float:
        return self.now_ms / 1000

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    shared = _Clock()
    # InboxService stamps expires_at_ms on receive, refreshes holds, and prunes
    # through this one seam; the daemon calls refresh_hold / prune_inbox
    # without now_ms, so patching it is what puts them on the shared clock.
    monkeypatch.setattr(InboxService, "_now_ms", staticmethod(shared.ms))
    return shared


class _TimedTurnProcess:
    """A worker whose turn takes clock seconds to finish, emitting one
    progress event partway through — models a genuinely long, active turn
    without needing a real subprocess/asyncio client."""

    running = True
    pid = None

    def __init__(
        self,
        *,
        complete_after_s: float | None,
        progress_at_s: float | None,
        clock: _Clock,
    ) -> None:
        self._clock = clock
        self._complete_after_s = complete_after_s
        self._progress_at_s = progress_at_s
        self._started_at: dict[str, float] = {}
        self._recipient: dict[str, str] = {}
        self._conversation: dict[str, str] = {}
        self._message_ids: dict[str, tuple[str, ...]] = {}
        self._progress_sent: set[str] = set()

    def stop(self) -> None:
        self.running = False

    def enqueue(self, delivery: HarnessDelivery) -> bool:
        if delivery.delivery_id in self._started_at:
            return False
        self._started_at[delivery.delivery_id] = self._clock.seconds()
        self._recipient[delivery.delivery_id] = delivery.recipient
        self._conversation[delivery.delivery_id] = delivery.conversation_id
        self._message_ids[delivery.delivery_id] = delivery.message_ids
        return True

    def drain_results(self) -> tuple[HarnessResult, ...]:
        if self._complete_after_s is None:
            return ()
        now = self._clock.seconds()
        results = []
        for delivery_id, started in list(self._started_at.items()):
            if now - started >= self._complete_after_s:
                results.append(
                    HarnessResult(
                        delivery_id=delivery_id,
                        recipient=self._recipient[delivery_id],
                        status=HarnessResultStatus.COMPLETED,
                        output="authoritative answer",
                        message_ids=self._message_ids[delivery_id],
                    )
                )
                del self._started_at[delivery_id]
        return tuple(results)

    def drain_progress(self) -> tuple[ProgressEvent, ...]:
        if self._progress_at_s is None:
            return ()
        now = self._clock.seconds()
        events = []
        for delivery_id, started in list(self._started_at.items()):
            if (
                now - started >= self._progress_at_s
                and delivery_id not in self._progress_sent
            ):
                self._progress_sent.add(delivery_id)
                events.extend(
                    ProgressEvent(
                        delivery_id=member,
                        conversation_id=self._conversation[delivery_id],
                        actor=WORKER,
                        harness="claude",
                        phase=PHASE_TOOL_CALL,
                        seq=index,
                        emitted_at_ms=self._clock.ms(),
                        summary="still working",
                        tool_call_id="call-1",
                        tool_name="slow_tool",
                    )
                    for index, member in enumerate(
                        self._message_ids[delivery_id] or (delivery_id,), start=1
                    )
                )
        return tuple(events)

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        return False


class _QueuedBehindBusyTurnProcess(_OneShotProcess):
    """Keep accepted deliveries queued until the already-running turn ends."""

    def __init__(self) -> None:
        super().__init__()
        self._busy = True

    def finish_busy_turn(self) -> None:
        self._busy = False

    def drain_results(self) -> tuple[HarnessResult, ...]:
        if self._busy:
            return ()
        results = tuple(
            HarnessResult(
                delivery_id=delivery.delivery_id,
                recipient=delivery.recipient,
                status=HarnessResultStatus.COMPLETED,
                output="authoritative answer",
                message_ids=delivery.message_ids,
            )
            for delivery in self.enqueued
        )
        self.enqueued.clear()
        return results


def test_live_busy_worker_keeps_an_accepted_queued_delivery_alive(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        link_admission=permissive_admission_slot(),
    )
    process = _QueuedBehindBusyTurnProcess()
    runtime = _wired_runtime(state_dir, inbox=inbox, process=process, clock=clock)
    original = _delivery_message("delivery-queued-behind-busy-turn")
    assert inbox.receive(original).accepted
    refresh_times: list[int] = []
    refresh_hold = inbox.refresh_hold

    def counted_refresh(message_id: str, *, now_ms: int | None = None) -> bool:
        refresh_times.append(clock.ms() if now_ms is None else now_ms)
        return refresh_hold(message_id, now_ms=now_ms)

    monkeypatch.setattr(inbox, "refresh_hold", counted_refresh)

    runtime.start()
    try:
        runtime.on_timer()  # accepted into the live worker's queue at t0
        assert len(process.enqueued) == 1

        pruned = []
        for _step in range(35):
            clock.advance(DEFAULT_HOLD_TTL_MS / 30_000)
            summary = runtime.on_timer()  # the earlier turn is still running
            pruned.extend(summary.inbox_pruned_items)

        assert pruned == []
        assert inbox.pending_count(WORKER) == 1
        assert refresh_times == [
            1_000_000,
            1_000_000 + DEFAULT_HOLD_TTL_MS // 2,
            1_000_000 + DEFAULT_HOLD_TTL_MS,
        ]

        process.finish_busy_turn()
        summary = runtime.on_timer()

        assert summary.harness_results == 1
        assert inbox.is_acknowledged(original.message_id)
    finally:
        runtime.stop()
        inbox.close()


def test_activity_refresh_survives_a_turn_longer_than_the_ttl(
    tmp_path: Path, clock: _Clock
) -> None:
    """The review's acceptance combination verbatim: TTL 5s, turn 8s.

    Pre-fix this reproduces the exact bug: the row is pruned mid-turn (well
    before the 8s turn finishes), so the eventual completed result finds
    ``original is None`` and is silently dropped — never acked.
    """

    state_dir = tmp_path / "state"
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        hold_policy=HoldPolicy(ttl_ms=5_000),
        link_admission=permissive_admission_slot(),
    )
    process = _TimedTurnProcess(complete_after_s=8.0, progress_at_s=2.5, clock=clock)
    runtime = _wired_runtime(state_dir, inbox=inbox, process=process, clock=clock)
    original = _delivery_message("delivery-ttl-e2e")
    accepted = inbox.receive(original)
    assert accepted.accepted

    runtime.start()
    try:
        runtime.on_timer()  # dispatch: refresh #1 (~t=0)
        assert process._started_at

        clock.advance(3.0)  # the mid-turn progress event fires at 2.5s
        summary = runtime.on_timer()  # drains it: refresh #2 (t=3s)
        assert summary.harness_progress == 1

        clock.advance(3.0)  # t=6s: past the ORIGINAL 5s deadline
        summary = runtime.on_timer()
        # Refreshed at 3s -> new deadline 8s: must NOT be pruned yet.
        assert summary.inbox_pruned_items == ()
        assert inbox.pending_count(WORKER) == 1

        # The turn finishes at t=8s from dispatch -- before the refreshed
        # deadline, so the row is still there to settle against.
        clock.advance(2.0)
        summary = runtime.on_timer()

        assert summary.harness_results == 1
        assert inbox.is_acknowledged("delivery-ttl-e2e")
    finally:
        runtime.stop()
        inbox.close()


def test_a_dead_worker_still_lets_the_row_expire_and_prune(
    tmp_path: Path, clock: _Clock
) -> None:
    """A stopped worker gets no lease beyond its final hold refresh."""

    state_dir = tmp_path / "state"
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        hold_policy=HoldPolicy(ttl_ms=DEFAULT_HOLD_TTL_MS),
        link_admission=permissive_admission_slot(),
    )
    # No completion or progress: the worker is explicitly removed below.
    process = _TimedTurnProcess(complete_after_s=None, progress_at_s=None, clock=clock)
    runtime = _wired_runtime(state_dir, inbox=inbox, process=process, clock=clock)
    original = _delivery_message("delivery-dead-worker")
    accepted = inbox.receive(original)
    assert accepted.accepted

    runtime.start()
    try:
        runtime.on_timer()  # dispatch: one refresh, then silence
        assert process._started_at

        clock.advance(5 * 60)
        runtime.desired_state.remove_harness("claude", "worker")
        assert runtime.harnesses.remove("claude", "worker")
        assert not runtime.harnesses.streaming_actors()
        runtime.on_timer()  # drops the lease when the worker is no longer live

        clock.advance(DEFAULT_HOLD_TTL_MS / 1000 - 5 * 60)
        summary = runtime.on_timer()

        assert [item.message_id for item in summary.inbox_pruned_items] == [
            "delivery-dead-worker"
        ]
        assert summary.inbox_pruned_items[0].reason == "TTL_EXPIRED"
        assert inbox.pending_count(WORKER) == 0
        assert not inbox.is_acknowledged("delivery-dead-worker")
    finally:
        runtime.stop()
        inbox.close()


def test_a_delivery_the_live_worker_never_accepted_expires_unchanged(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        link_admission=permissive_admission_slot(),
    )
    original = _delivery_message("delivery-refused")
    assert inbox.receive(original).accepted
    process = _OneShotProcess()
    process._known.add(original.message_id)
    runtime = _wired_runtime(state_dir, inbox=inbox, process=process, clock=clock)
    refreshes: list[str] = []
    refresh_hold = inbox.refresh_hold

    def counted_refresh(message_id: str, *, now_ms: int | None = None) -> bool:
        refreshes.append(message_id)
        return refresh_hold(message_id, now_ms=now_ms)

    monkeypatch.setattr(inbox, "refresh_hold", counted_refresh)

    runtime.start()
    try:
        runtime.on_timer()
        assert process.enqueued == []
        assert refreshes == []

        clock.advance(DEFAULT_HOLD_TTL_MS / 1000)
        summary = runtime.on_timer()

        assert [item.message_id for item in summary.inbox_pruned_items] == [
            original.message_id
        ]
        assert refreshes == []
    finally:
        runtime.stop()
        inbox.close()
