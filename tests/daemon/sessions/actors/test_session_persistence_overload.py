"""Completed Agent effects retain bounded custody through storage pressure."""
from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel.impl.actor_runtime import ActorRuntime, ActorState
from hyprial.kernel.impl.actor_runtime.contracts import ActorEventKind
from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.composition import CorrelatedDomainEvents
from hyprial.daemon.impl.desired_state import DesiredStateStore, InteractiveSession
from hyprial.daemon.impl.session_actor import SessionActor, SessionOwnershipError
from hyprial.daemon.impl.operations.session_ports import (
    HeartbeatSessionCommand, RefreshSessionCommand, RegisterSessionCommand,
    SessionLeaseElapsedCommand, SessionMutationCompleted,
)

from .domains.support import _fail_next_ownership_read

ACTOR = "agent:probe:local:session"
LEASE = "isolated-session-lease-token-00000001"


def _wait(predicate):
    # Orchestration slack for explicit progress barriers, not a product latency
    # assertion or sleep followed by an assertion that nothing happened.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("isolated session probe did not reach its barrier")


class _GatedStore(DesiredStateStore):
    def __init__(self, path: Path, block_record: int):
        super().__init__(path)
        self.block_record = block_record
        self.records = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def record_session_agent_effects(self, effects):
        self.records += 1
        if self.records == self.block_record:
            self.entered.set()
            assert self.release.wait(5.0), "test did not release real storage write"
        return super().record_session_agent_effects(effects)


class _Probe:
    def __init__(self, tmp_path: Path, capacity: int, originals: int):
        self.store = _GatedStore(tmp_path / "desired.json", originals + 1)
        self.store.claim_interactive(InteractiveSession(
            actor=ACTOR, cwd=str(tmp_path), command=("claude",),
            source="claude-channel", session_ref="session-ref",
            runtime="claude_interactive", channel_confirmed=True,
            channel_protocol_version=2,
            channel_lease_digest=hashlib.sha256(LEASE.encode()).hexdigest(),
        ))
        self.events = []
        self.runtime_events = []
        self.commands = []
        self.router = CorrelatedDomainEvents()
        self.runtime = ActorRuntime(event_sink=self.runtime_events.append)
        self.actor = SessionActor(
            self.store, daemon_epoch="isolated-epoch", event_sink=self.publish,
            agent_commands=self, mailbox_capacity=capacity, runtime=self.runtime,
        )
        self.capacity = capacity
        self.originals = originals
        self.persistence = self.actor._persistence
        assert self.persistence is not None
        self.lane = self.persistence._effects

    def publish(self, event):
        # A permissive append sink previously hid empty-correlation failures.
        self.router.publish(event)
        self.events.append(event)

    def submit(self, command):
        # This is the fake Agent command port. Agent results are explicitly
        # delivered by the test, never inferred from mailbox admission.
        self.commands.append(command)
        return PortAdmission.ACCEPTED

    def refresh(self, correlation):
        assert self.actor.submit(RefreshSessionCommand(
            correlation, ACTOR, "session-ref", LEASE,
        )) is PortAdmission.ACCEPTED

    def complete(self, command):
        assert self.actor.accept_agent_event(AgentMutationCompleted(
            command.correlation_id, 1, 1, "bind", True,
        ))

    def fill(self, *, supersede=False):
        for index in range(self.originals):
            self.refresh(f"A{index}")
            _wait(lambda: len(self.commands) == index + 1
                  and self.lane.snapshot().outstanding == 0)
        self.refresh("B")
        assert self.store.entered.wait(5.0)
        for index in range(self.capacity - 1):
            if supersede:
                assert self.capacity == 2
                assert self.actor.submit(RegisterSessionCommand(
                    "NEW", ACTOR, "/private/tmp", ("claude",),
                    "claude-channel", "new-ref", channel_protocol_version=2,
                    channel_lease_token=LEASE, manage_agent=False,
                )) is PortAdmission.ACCEPTED
            else:
                self.refresh(f"C{index}")
            _wait(lambda: self.lane.snapshot().outstanding == index + 2)
        for index, command in enumerate(tuple(self.commands)):
            self.complete(command)
            _wait(lambda: len(self.events) + len(getattr(self.actor, "_deferred_persistence", {}))
                  == index + 1)

    def release_and_finish(self, *, supersede=False):
        self.store.release.set()
        expected = self.capacity + self.originals - int(supersede)
        _wait(lambda: len(self.commands) == expected
              and self.lane.snapshot().outstanding == 0)
        for command in self.commands[self.originals:]:
            self.complete(command)
            _wait(lambda: not self.actor.owns_agent_correlation(command.correlation_id))
        _wait(lambda: self.lane.snapshot().outstanding == 0)
        assert len(self.commands) == expected  # No repeated Agent side effects.
        assert self.actor._deferred_persistence == {}
        assert self.actor._effect_custody == {}
        assert self.actor._agent_attempts == {}
        assert self.actor._mutations == {}
        assert self.actor._effect_owners == {}

    def successful_correlations(self):
        return {event.correlation_id for event in self.events
                if isinstance(event, SessionMutationCompleted)}


@contextmanager
def _probe(tmp_path, *, capacity=2, originals=1):
    probe = _Probe(tmp_path, capacity, originals)
    try:
        yield probe
    finally:
        probe.store.release.set()
        assert probe.actor.drain(2.0).complete


@pytest.mark.parametrize("capacity", (2, 128))
def test_completed_agent_result_waits_for_real_persistence_capacity(tmp_path, capacity):
    with _probe(tmp_path, capacity=capacity) as probe:
        probe.fill()
        assert not probe.events  # RED on v2: A0 is terminally rejected here.
        assert len(probe.actor._deferred_persistence) == 1
        assert len(probe.actor._effect_custody) == 1
        assert probe.lane.snapshot().outstanding == capacity
        probe.release_and_finish()
        assert probe.successful_correlations() == {
            "A0", "B", *(f"C{i}" for i in range(capacity - 1)),
        }
        assert len(probe.events) == capacity + 1
        assert probe.store.load().pending_session_agent_effects == ()
        assert probe.actor.generation == 1
        assert probe.actor.read_runtime(ACTOR).alive
        assert not any(event.kind is ActorEventKind.CHILD_FAILED
                       for event in probe.runtime_events)

        # Existing ownership, generation, and lease boundaries remain enforced.
        for command, code in (
            (SessionLeaseElapsedCommand("stale-generation", 0, probe.actor.version, 1000), "STALE_GENERATION"),
            (HeartbeatSessionCommand("stale-session", ACTOR, "other", LEASE), "SESSION_SUPERSEDED"),
            (HeartbeatSessionCommand("bad-lease", ACTOR, "session-ref", "wrong-token"), "INVALID_CHANNEL_LEASE"),
        ):
            assert probe.actor.submit(command) is PortAdmission.ACCEPTED
            _wait(lambda: any(event.correlation_id == command.correlation_id
                              for event in probe.events))
            assert isinstance(probe.events[-1], PortCommandRejected)
            assert probe.events[-1].code == code


def test_closed_persistence_rejects_original_correlations_without_actor_failure(tmp_path):
    with _probe(tmp_path) as probe:
        probe.fill()
        assert not probe.events
        # The real submitted write is still blocked, so drain cannot succeed.
        assert not probe.persistence.close(0.01)
        probe.release_and_finish()
        assert len(probe.events) == 3
        assert {event.correlation_id for event in probe.events} == {"A0", "B", "C0"}
        assert all(isinstance(event, PortCommandRejected)
                   and event.code == "SESSION_PERSISTENCE_OVERLOADED"
                   and "closed" in event.detail for event in probe.events)
        assert not probe.successful_correlations()
        assert len(probe.store.load().pending_session_agent_effects) == 3
        snapshot = probe.runtime.snapshot(probe.actor._handle)
        assert snapshot.state is ActorState.RUNNING and snapshot.generation == 1
        assert probe.actor.command_costs.snapshot()["_AgentEffectResult"]["errors"] == 0
        assert not any(event.kind is ActorEventKind.CHILD_FAILED
                       for event in probe.runtime_events)


def test_deferred_completion_bound_rejects_excess_and_explicit_replay_recovers(tmp_path):
    with _probe(tmp_path, originals=3) as probe:
        probe.fill()
        assert len(probe.actor._deferred_persistence) == probe.capacity
        assert len(probe.actor._effect_custody) == probe.capacity
        assert len(probe.events) == 1
        rejected = probe.events[0]
        assert isinstance(rejected, PortCommandRejected)
        assert rejected.correlation_id == "A2"
        assert rejected.code == "SESSION_PERSISTENCE_OVERLOADED"
        probe.release_and_finish()
        assert probe.successful_correlations() == {"A0", "A1", "B", "C0"}
        remaining = probe.store.load().pending_session_agent_effects
        assert len(remaining) == 1 and remaining[0].correlation_id == "A2"
        events_before_replay = tuple(probe.events)
        previous_commands = len(probe.commands)
        assert probe.actor.reconcile_pending_effects() == 1
        _wait(lambda: len(probe.commands) == previous_commands + 1)
        probe.complete(probe.commands[-1])
        _wait(lambda: not probe.actor.owns_agent_correlation(probe.commands[-1].correlation_id))
        assert probe.store.load().pending_session_agent_effects == ()
        assert probe.actor._effect_custody == {}
        assert tuple(probe.events) == events_before_replay  # No success after rejection.


@pytest.mark.parametrize("supersede", (False, True))
def test_deferred_completion_survives_generation_and_cannot_restore_old_route(tmp_path, supersede):
    with _probe(tmp_path) as probe:
        probe.fill(supersede=supersede)
        assert not probe.events
        _fail_next_ownership_read(probe.actor)
        probe.refresh("crash-probe")
        _wait(lambda: probe.runtime.snapshot(probe.actor._handle).generation == 2)
        assert len(probe.commands) == 1
        probe.release_and_finish(supersede=supersede)
        assert probe.successful_correlations() == {"A0", "B", "NEW" if supersede else "C0"}
        assert len(probe.events) == 3
        assert probe.store.load().pending_session_agent_effects == ()
        assert probe.actor.generation == 2
        if supersede:
            assert probe.actor.read_session(ACTOR).session_ref == "new-ref"
            probe.actor.assert_owner(ACTOR, "new-ref")
            with pytest.raises(SessionOwnershipError):
                probe.actor.assert_owner(ACTOR, "session-ref")
            events = {event.correlation_id: event for event in probe.events}
            assert events["NEW"].version > events["A0"].version
            app = SimpleNamespace(
                _interactive_route_actor_versions={}, _interactive_route_versions={},
            )
            accept = DaemonApplication._accept_interactive_route_observation_locked
            assert accept(app, ACTOR, "new-ref", events["NEW"].version)
            assert not accept(app, ACTOR, "session-ref", events["A0"].version)
