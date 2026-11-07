# Deflake (2026-11-03, workflow wf-ff332cef #implDF): _SESSION_EVENT_BUDGET
# was a hand-coded 2.0s guess and test_confirmation_cannot_overwrite_a_
# concurrent_session_takeover went red in CI (run 45309) when the register
# event did not settle within it under shard load (3-CPU cgroup quota).
# Same class the CI workflow declared the HYPRIAL_TEST_WAIT_SECONDS knob for
# (2026-10-09 ruling: deadlines only bound the waits; assertions check state,
# not speed). The budget still waits on the real condition (the correlation's
# settlement event); every caller in this tree waits for an event that IS
# expected to arrive, so widening only adds tolerance, never vacuity.
from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable

from tests.support.runtime.waiting_support import Budget
from pathlib import Path

import hyprial.daemon.impl.session_actor as session_module
from hyprial.identity.impl.agents import AgentActor, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import (
    AgentCommand,
    AgentMutationCompleted,
    BindAgentCommand,
)
from hyprial.kernel.impl.contracts.channels.channel import CHANNEL_PROTOCOL_VERSION
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.desired_state import (
    DesiredStateStore,
    )
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.daemon.impl.operations.session_ports import (
    RegisterSessionCommand,
    SessionMutationCompleted,
)

OWNER = "op"
NODE = "node-1"
ACTOR = f"agent:{OWNER}:{NODE}:foo"
LEASE = "lease-token-that-is-long-enough-0001"


class _AgentEventRelay:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.session: SessionActor | None = None

    def publish(self, event: object) -> None:
        self.events.append(event)
        if self.session is not None:
            self.session.accept_agent_event(event)  # type: ignore[arg-type]








class _ScriptedAgentCommands:
    def __init__(self, admissions: tuple[PortAdmission, ...]) -> None:
        self.admissions = deque(admissions)
        self.default = PortAdmission.ACCEPTED
        self.commands: list[AgentCommand] = []
        self.session: SessionActor | None = None
        self.pending_events: list[AgentMutationCompleted] = []

    def submit(self, command: AgentCommand) -> PortAdmission:
        self.commands.append(command)
        admission = self.admissions.popleft() if self.admissions else self.default
        if admission is PortAdmission.ACCEPTED:
            operation = "bind" if isinstance(command, BindAgentCommand) else "release"
            event = AgentMutationCompleted(
                correlation_id=command.correlation_id,
                generation=1,
                version=len(self.commands),
                operation=operation,
                changed=True,
            )
            if self.session is None:
                self.pending_events.append(event)
            else:
                self.session.accept_agent_event(event)
        return admission

    def attach(self, session: SessionActor) -> None:
        self.session = session
        for event in self.pending_events:
            session.accept_agent_event(event)
        self.pending_events.clear()


class _FaultOnceDesiredStateStore(DesiredStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._fault_lock = threading.Lock()
        self._fail_next = False

    def fail_next_load(self) -> None:
        with self._fault_lock:
            self._fail_next = True

    def load(self):  # type: ignore[no-untyped-def]
        with self._fault_lock:
            if self._fail_next:
                self._fail_next = False
                raise RuntimeError("injected desired-state read fault")
        return super().load()


class _SlowSessionEffectStore(DesiredStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def record_session_agent_effects(self, effects):  # type: ignore[no-untyped-def]
        self.entered.set()
        assert self.release.wait(_ORCHESTRATION_SLACK_SECONDS)
        return super().record_session_agent_effects(effects)


class _BlockingAcceptedAgentCommands:
    def __init__(self) -> None:
        self.commands: list[AgentCommand] = []
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.session: SessionActor | None = None

    def submit(self, command: AgentCommand) -> PortAdmission:
        self.commands.append(command)
        if len(self.commands) == 1:
            self.first_entered.set()
            assert self.release_first.wait(_ORCHESTRATION_SLACK_SECONDS)
        assert self.session is not None
        self.session.accept_agent_event(
            AgentMutationCompleted(
                correlation_id=command.correlation_id,
                generation=1,
                version=len(self.commands),
                operation=(
                    "bind" if isinstance(command, BindAgentCommand) else "release"
                ),
                changed=True,
            )
        )
        return PortAdmission.ACCEPTED


class _BlockingFailureSession(SessionActor):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.failure_entered = threading.Event()
        self.release_failure = threading.Event()
        self.blocked_failure: session_module._AgentEffectUnavailable | None = None
        super().__init__(*args, **kwargs)

    def _deliver_internal(self, command: object) -> bool:
        if isinstance(command, session_module._AgentEffectUnavailable):
            self.blocked_failure = command
            self.failure_entered.set()
            assert self.release_failure.wait(_ORCHESTRATION_SLACK_SECONDS)
        return super()._deliver_internal(command)


class _ClosingThenBlockingAcceptedCommands:
    def __init__(self) -> None:
        self.commands: list[AgentCommand] = []
        self.second_entered = threading.Event()
        self.release_second = threading.Event()
        self.session: SessionActor | None = None

    def submit(self, command: AgentCommand) -> PortAdmission:
        self.commands.append(command)
        if len(self.commands) == 1:
            return PortAdmission.CLOSING
        if len(self.commands) == 2:
            self.second_entered.set()
            assert self.release_second.wait(_ORCHESTRATION_SLACK_SECONDS)
        assert self.session is not None
        self.session.accept_agent_event(
            AgentMutationCompleted(
                correlation_id=command.correlation_id,
                generation=1,
                version=len(self.commands),
                operation=(
                    "bind" if isinstance(command, BindAgentCommand) else "release"
                ),
                changed=True,
            )
        )
        return PortAdmission.ACCEPTED


def _wired_session(
    tmp_path: Path,
    events: list[object],
    *,
    daemon_epoch: str = "epoch-1",
    clock: Callable[[], float] | None = None,
) -> tuple[SessionActor, AgentActor]:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    if registry.get("foo") is None:
        registry.create("foo")
    relay = _AgentEventRelay()
    agent = AgentActor(registry, event_sink=relay)
    session = SessionActor(
        DesiredStateStore(tmp_path / "desired.json"),
        daemon_epoch=daemon_epoch,
        event_sink=events.append,
        agent_commands=agent,
        clock=clock,
    )
    relay.session = session
    return session, agent




_SESSION_EVENT_BUDGET = Budget(
    seconds=float(os.environ.get("HYPRIAL_TEST_WAIT_SECONDS", "5.0")),
    origin="HYPRIAL_TEST_WAIT_SECONDS (CI-declared shard wait budget; was a 2.0s guess)",
)

#: ⛔ NOT a product promise and deliberately NOT a ``Budget`` -- orchestration
#: slack.  The ``Event.wait(...)`` barriers below are the test's OWN
#: choreography: it deliberately blocks the worker to stage a race, and the
#: deadline only guards that the test does not HANG, not that some operation
#: completes in time.  If you enlarge this number you lose nothing but a
#: slower test -- which is exactly what makes it slack (a real deadline loses
#: something when enlarged; this loses nothing).  There is no product
#: constant that says "a thread should reach a breakpoint within N seconds",
#: so ``Budget.from_product`` has nothing to point at, and folding it into
#: ``_SESSION_EVENT_BUDGET`` would make test choreography read as a product
#: promise.  To retire it: if a real deadline is ever needed here, it is not
#: this constant.
_ORCHESTRATION_SLACK_SECONDS = 2.0

def _fail_next_ownership_read(actor: SessionActor) -> None:
    """Crash one Session generation at its keyed ownership seam."""

    projection = actor._session_projection  # noqa: SLF001 - fault injection seam
    real_read = projection.ownership_candidates
    pending = True

    def fail_once(actor_uri: str, session_ref: str):  # type: ignore[no-untyped-def]
        nonlocal pending
        if pending:
            pending = False
            raise RuntimeError("injected session projection read fault")
        return real_read(actor_uri, session_ref)

    projection.ownership_candidates = fail_once  # type: ignore[method-assign]


def _wait_until(
    predicate: Callable[[], bool], timeout: float | None = None
) -> None:
    deadline = time.monotonic() + (
        _SESSION_EVENT_BUDGET.seconds if timeout is None else timeout
    )
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _registered(events: list[object], correlation_id: str) -> bool:
    return any(
        isinstance(event, SessionMutationCompleted)
        and event.correlation_id == correlation_id
        for event in events
    )


def _register(correlation_id: str, session_ref: str) -> RegisterSessionCommand:
    return RegisterSessionCommand(
        correlation_id=correlation_id,
        actor=ACTOR,
        cwd="/work",
        command=("claude",),
        source="claude-channel",
        session_ref=session_ref,
        channel_confirmed=True,
        channel_protocol_version=CHANNEL_PROTOCOL_VERSION,
        channel_lease_token=LEASE,
    )


















































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'ACTOR',
    'LEASE',
    'NODE',
    'OWNER',
    '_AgentEventRelay',
    '_BlockingAcceptedAgentCommands',
    '_BlockingFailureSession',
    '_ClosingThenBlockingAcceptedCommands',
    '_FaultOnceDesiredStateStore',
    '_ORCHESTRATION_SLACK_SECONDS',
    '_SESSION_EVENT_BUDGET',
    '_ScriptedAgentCommands',
    '_SlowSessionEffectStore',
    '_register',
    '_registered',
    '_wait_until',
    '_wired_session',
]
