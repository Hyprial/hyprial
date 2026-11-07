'U0c ④: an interrupted saga is compensated on restart, never resumed.\n\nAllen\'s ruling (spec-pr332-fixes, 2026-09-03, lines 7-9): a saga that did\nnot finish in the same daemon generation starts OVER after a restart --\nstartup first compensates the half-products (half-built agent/session,\nhalf-pulled processes), then desired-state is re-run.  Consequences pinned\nhere:\n\n* the process manager never resumes forward across a generation: journal\n  effect status feeds compensation only ("which resources did this\n  operation touch"), it is not a resume cursor;\n* no lifecycle receipt crosses a generation boundary -- the durable\n  receipts (desired-state ``lifecycle_receipts`` for the harness/session\n  domains, the agents registry\'s own ``lifecycle_receipts`` table) are\n  expired at startup, incomplete harness admissions rolled back;\n* the journal itself is never pruned (the U0b tripwire\n  ``tests/daemon/lifecycle/journal/test_lifecycle_journal_retention_gate.py`` stays green untouched\n  -- compensation deletes receipts/resources, not journal rows).\n'

from __future__ import annotations

import ast
import sqlite3
import inspect
import textwrap
import time
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
)
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecyclePorts,
    LifecycleProcessManager,
    LifecycleSpec,
    LifecycleState,
)
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
    MutationProvenance,
)
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports import RouteSpec


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


# ---------------------------------------------------------------------------
# Saga layer: restart compensates instead of resuming (manager-level ④)
# ---------------------------------------------------------------------------

#: Budget for waiting on a lifecycle operation's terminal state -- the same
#: ruler the application uses (operation deadline + wait margin).  ``wait``
#: only polls the durable result and never changes the saga, so this is not a
#: latency claim; literal 2/3/5s budgets went red under CI load (dev task
#: 15834, #600 task 15852, #605 task 15843: "lifecycle operation still running").
TERMINAL_WAIT_SECONDS = LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS



def _settles(predicate, timeout: float = 5.0, step: float = 0.01) -> bool:  # type: ignore[no-untyped-def]
    """Bounded poll on the assertion's own object (card 2389ace2, family A).

    The fault hooks below raise mid-saga to simulate a daemon death; the tests
    used to give that crash 100-200 ms of wall clock (``first.wait(key, 0.2)``)
    before asserting the post-crash state, and under a full unit run the saga
    had not reached the crash yet.  Wait for the asserted state itself.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def test_restart_compensates_an_interrupted_saga_instead_of_resuming_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saga.sqlite3"
    receipts: dict[str, MutationProvenance] = {}
    current_tokens: dict[str, str] = {}
    router1 = CorrelationEventRouter()
    ports1 = _CompletionPorts(router1, receipts=receipts, current_tokens=current_tokens)

    crash_reached = Event()

    def daemon_death_mid_saga(_operation_id: str, effect: str) -> None:
        if effect == "agent.bind":
            crash_reached.set()
            raise RuntimeError("simulated daemon death mid-saga")

    with ExitStack() as cleanup:
        first = _manager(path, ports1, router1, fault_after_effect=daemon_death_mid_saga)
        cleanup.callback(first._thread.join, 5)
        cleanup.callback(first.drain, 5)
        first.submit(LifecycleOperation.create("restart-compensate", _spec()))
        assert crash_reached.wait(TERMINAL_WAIT_SECONDS), "saga did not reach the injected crash"
        first._thread.join(5)
        assert not first._thread.is_alive(), "crashed generation did not exit"
        with pytest.raises(TimeoutError):
            first.wait("restart-compensate", 0.0)
        assert [type(item).__name__ for item in ports1.calls] == [
            "CreateAgentCommand",
            "EnsureRouteCommand",
            "BindAgentCommand",
        ]

        # A new process-manager generation on the same journal.  U0c: it must
        # compensate the interrupted saga (clear the half-built world), not
        # finish it forward.
        router2 = CorrelationEventRouter()
        ports2 = _CompletionPorts(router2, receipts=receipts, current_tokens=current_tokens)
        second = _manager(path, ports2, router2)
        cleanup.callback(second.drain, 5)
        result = second.wait("restart-compensate", TERMINAL_WAIT_SECONDS)
        assert result.state is LifecycleState.COMPENSATED
        assert result.completed_effects == (
            "agent.create",
            "route.persona.ensure",
            "agent.bind",
        )
        assert result.compensated_effects == (
            "agent.create",
            "route.persona.ensure",
            "agent.bind",
        )
        # The reverse plan ran in the NEW generation, in reverse order.
        assert [type(item).__name__ for item in ports2.calls] == [
            "ReleaseAgentCommand",
            "DropRouteCommand",
            "DestroyAgentCommand",
        ]
        # The restart cause is visible on the compensated result.
        assert result.error_code == "SAGA_INTERRUPTED_BY_RESTART"
        assert "generation" in (result.error or "")

        # "Start over" means a fresh operation starts from step zero and
        # completes; desired-state re-run authority is not the journal.
        assert (
            second.submit(
                LifecycleOperation.create("restart-compensate-fresh", _spec())
            )
            is PortAdmission.ACCEPTED
        )
        fresh = second.wait("restart-compensate-fresh", TERMINAL_WAIT_SECONDS)
        assert fresh.state is LifecycleState.COMPLETED
        assert len(fresh.completed_effects) == 6

        # The journal is evidence, not a cursor: every row survives (no prune).
        with closing(sqlite3.connect(path)) as db:
            effects = db.execute(
                "SELECT COUNT(*) FROM lifecycle_effects "
                "WHERE operation_id = 'restart-compensate'"
            ).fetchone()[0]
            operations = db.execute(
                "SELECT COUNT(*) FROM lifecycle_operations "
                "WHERE operation_id = 'restart-compensate'"
            ).fetchone()[0]
        assert effects == 6  # 3 forward + 3 compensation, none deleted
        assert operations == 1
        assert second.drain(1)


# ---------------------------------------------------------------------------
# Composition layer: restart clears the durable half-products (daemon ④)
# ---------------------------------------------------------------------------


def test_interrupted_saga_restart_clears_half_products_end_to_end(
    tmp_path: Path,
    own_daemon_application,  # type: ignore[no-untyped-def]
) -> None:
    """A create saga killed between the harness admission and its settlement.

    Generation 1 really runs a full create saga to COMPLETED, then the
    durable state is rewound to the post-kill shape: journal effects after
    ``agent.bind`` removed (never settled), the operation back to RUNNING,
    the desired-state harness row removed (it only lands on success), and
    the ensure's admission fence + incomplete receipt re-written exactly as
    the mid-start admission leaves them.  Generation 2 then boots on those
    databases and must compensate, leaving no half-product behind.
    """

    state_dir = tmp_path / "state"
    operation_id = "kill-mid-ensure"
    target = LifecycleSpec(
        agent_name="worker",
        actor="agent:owner:node-1:worker",
        harness=HarnessLaunchProjection("pi", "worker", True),
        route=RouteSpec(
            "agent:owner:node-1:worker",
            "live/worker",
            "inbox/worker",
        ),
    )

    app1 = own_daemon_application(
        DaemonApplication(
            state_dir=state_dir,
            socket_path=tmp_path / "daemon.sock",
            node_id="node-1",
            owner="owner",
        **_app_injections(),
        )
    )
    events1 = CorrelatedDomainEvents()
    actor1 = HarnessRuntimeActor(
        _Launcher(), event_sink=events1, desired_state=app1.desired_state
    )
    harnesses1 = HarnessPortClient(actor1, events1)
    app1._start_lifecycle_manager(
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=_Inbox(),  # type: ignore[arg-type]
        local_delivery=_LocalDelivery(),  # type: ignore[arg-type]
        harnesses=harnesses1,  # type: ignore[arg-type]
    )
    assert app1._lifecycle_manager is not None
    assert (
        app1._lifecycle_manager.submit(LifecycleOperation.create(operation_id, target))
        is PortAdmission.ACCEPTED
    )
    assert (
        app1._lifecycle_manager.wait(operation_id, TERMINAL_WAIT_SECONDS).state
        is LifecycleState.COMPLETED
    )
    assert app1.agents.get("worker") is not None
    assert app1.desired_state.load().harnesses[0].name == "worker"

    # Generation 1 has completed. Drain its public lifecycle owner before
    # rewinding the durable journal, so no worker or recovery loop can read
    # the simulated post-kill state while generation 2 is constructed.
    manager1 = app1._lifecycle_manager
    assert manager1.drain(5.0)
    app1._lifecycle_manager = None  # the explicit drain retired this owner

    # --- rewind the durable state to the post-kill shape ---------------
    # The harness.ensure step WAS admitted (its fence + incomplete receipt
    # exist) but never settled: the journal keeps the effect row in
    # 'dispatched' state and the later steps never ran.  The desired-state
    # row only lands on start success, so it is removed; the fence is set
    # back to the pre-ensure inactive state so the admission re-activates
    # it with a fresh token and changed=True -- the durable shape a kill
    # between admission and settlement leaves.
    state_db = state_dir / "lifecycle-operations.sqlite3"
    projection = app1._state_persistence.projection()
    app1._state_persistence.settled_desired.compare_and_save(
        replace(projection.desired, harnesses=()),
        expected_version=projection.version,
    )
    with sqlite3.connect(state_db) as db:
        db.execute(
            "UPDATE lifecycle_operations SET state = 'running', error = NULL, "
            "error_code = NULL WHERE operation_id = ?",
            (operation_id,),
        )
        db.execute(
            "UPDATE lifecycle_effects SET status = 'dispatched' "
            "WHERE operation_id = ? AND ordinal = 3",
            (operation_id,),
        )
        db.execute(
            "DELETE FROM lifecycle_effects WHERE operation_id = ? AND ordinal >= 4",
            (operation_id,),
        )
        db.execute(
            "UPDATE lifecycle_resources SET active = 0 "
            "WHERE resource_key = 'harness:pi:worker'"
        )
    admission = LifecycleMutationRequest(
        f"lifecycle:{operation_id}:forward:3",
        f"{operation_id}:forward:3",
        operation_id,
        None,
        EnsureHarnessCommand(
            f"lifecycle:{operation_id}:forward:3", target.harness
        ),
    )
    provenance, _replayed = app1.desired_state.apply_harness_lifecycle(admission)
    assert provenance.changed is True  # the admission really (re)activated
    assert app1.desired_state.load().harnesses == ()
    assert len(app1.desired_state.load().lifecycle_receipts) == 1
    receipt = app1.desired_state.load().lifecycle_receipts[0]
    assert receipt.completed is False
    assert receipt.domain == "harness"

    # --- generation 2 boots on the same databases ----------------------
    app2 = own_daemon_application(
        DaemonApplication(
            state_dir=state_dir,
            socket_path=tmp_path / "daemon.sock",
            node_id="node-1",
            owner="owner",
        **_app_injections(),
        )
    )
    # The daemon's real order: expire the previous generation's receipts
    # BEFORE the harness actor is constructed (its restore deferral set is
    # seeded from incomplete receipts) and before the lifecycle manager
    # boots (its recovery must not resume).
    app2._expire_previous_generation_lifecycle_state()
    assert app2.desired_state.incomplete_harness_lifecycle_resources() == ()

    events2 = CorrelatedDomainEvents()
    actor2 = HarnessRuntimeActor(
        _Launcher(), event_sink=events2, desired_state=app2.desired_state
    )
    harnesses2 = HarnessPortClient(actor2, events2)
    app2._start_lifecycle_manager(
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=_Inbox(),  # type: ignore[arg-type]
        local_delivery=_LocalDelivery(),  # type: ignore[arg-type]
        harnesses=harnesses2,  # type: ignore[arg-type]
    )
    manager2 = app2._lifecycle_manager
    assert manager2 is not None
    result = manager2.wait(operation_id, TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.compensated_effects == (
        "agent.create",
        "route.persona.ensure",
        "agent.bind",
        "harness.ensure",
    )

    # No half-products: the half-built agent row is destroyed, no desired
    # harness row exists, and no lifecycle receipt survived the generation.
    assert app2.agents.get("worker") is None
    state2 = app2.desired_state.load()
    assert state2.harnesses == ()
    assert state2.lifecycle_receipts == ()
    fence = next(
        (
            resource
            for resource in state2.lifecycle_resources
            if resource.resource_key == "harness:pi:worker"
        ),
        None,
    )
    assert fence is None or fence.active is False

    # The journal keeps every row: the U0b rescue evidence is intact.
    with sqlite3.connect(state_db) as db:
        effects = db.execute(
            "SELECT COUNT(*) FROM lifecycle_effects WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()[0]
    assert effects == 8  # 3 settled + 1 backfilled forward + 4 compensation

    # Desired-state is the only re-run authority: a fresh create for the
    # same target starts from zero and completes.
    assert (
        manager2.submit(LifecycleOperation.create("rerun-after-restart", target))
        is PortAdmission.ACCEPTED
    )
    assert manager2.wait("rerun-after-restart", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert app2.agents.get("worker") is not None
    assert app2.desired_state.load().harnesses[0].name == "worker"


# ---------------------------------------------------------------------------
# Minimal composition doubles (same shapes as the final-composition suite)
# ---------------------------------------------------------------------------


class _Registration:
    def close(self, **_kwargs: object) -> None:
        return None


class _Transport:
    def declare_liveliness(self, _key: str) -> _Registration:
        return _Registration()

    def subscribe(self, _key: str, _callback: object) -> _Registration:
        return _Registration()

    def declare_queryable(self, _key: str, _handler: object) -> _Registration:
        return _Registration()


class _LocalDelivery:
    def register_actor(self, _actor: str) -> _Registration:
        return _Registration()


class _Inbox:
    def receive(self, _message: object) -> None:
        return None

    def has_received(self, _message_id: str) -> bool:
        return False

    def has_fetched(self, _message_id: str) -> bool:
        return False


class _Process:
    def __init__(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class _Launcher:
    def start(self, _spec: object) -> _Process:
        return _Process()


def test_restart_also_compensates_an_interrupted_remove_saga(
    tmp_path: Path,
) -> None:
    """The restart rule is uniform across saga kinds (a flagged choice).

    An interrupted REMOVE/DEACTIVATE (a ``hyprial stop`` killed mid-way) is
    compensated like every other interrupted saga: the partial removal is
    undone, not finished.  The durable consequence -- the interrupted stop
    behaves like a FAILED stop (the world is restored and the user re-runs
    it) -- is identical to the same-generation failure semantics, and it
    is what keeps "the journal crosses generations as compensation input
    only" true for removes as well: nothing resumes forward, ever.
    """

    path = tmp_path / "saga.sqlite3"
    receipts: dict[str, MutationProvenance] = {}
    current_tokens: dict[str, str] = {}
    router1 = CorrelationEventRouter()
    ports1 = _CompletionPorts(router1, receipts=receipts, current_tokens=current_tokens)

    crash_reached = Event()

    def death_mid_removal(_operation_id: str, effect: str) -> None:
        if effect == "bind.release":
            crash_reached.set()
            raise RuntimeError("simulated daemon death mid-removal")

    # Same shape as the create-saga test above.  The fourth port call is
    # appended before the manager commits ``bind.release`` to the journal, so
    # waiting on ``len(ports1.calls) >= 4`` let the second generation read the
    # journal one effect early (CI task 11224: compensated_effects lacked
    # ``bind.release``).  The hook runs after ``complete_effect``, so its
    # signal plus the crashed thread's exit is the asserted state itself.
    with ExitStack() as cleanup:
        first = _manager(path, ports1, router1, fault_after_effect=death_mid_removal)
        cleanup.callback(first._thread.join, 5)
        cleanup.callback(first.drain, 5)
        first.submit(LifecycleOperation.remove("interrupted-stop", _spec()))
        assert crash_reached.wait(TERMINAL_WAIT_SECONDS), "removal did not reach the injected crash"
        first._thread.join(5)
        assert not first._thread.is_alive(), "crashed generation did not exit"
        with pytest.raises(TimeoutError):
            first.wait("interrupted-stop", 0.0)
        # The remove plan runs create-in-reverse: four steps tore down before
        # the crash (managed route, session, harness, binding).
        assert [type(item).__name__ for item in ports1.calls] == [
            "DropRouteCommand",
            "UnregisterSessionCommand",
            "RemoveHarnessCommand",
            "ReleaseAgentCommand",
        ]

        router2 = CorrelationEventRouter()
        ports2 = _CompletionPorts(router2, receipts=receipts, current_tokens=current_tokens)
        second = _manager(path, ports2, router2)
        cleanup.callback(second.drain, 5)
        result = second.wait("interrupted-stop", TERMINAL_WAIT_SECONDS)
        # Not resumed to COMPLETED: the half-removal is undone instead.
        assert result.state is LifecycleState.COMPENSATED
        assert result.error_code == "SAGA_INTERRUPTED_BY_RESTART"
        assert result.compensated_effects == (
            "managed.ensure.drop",
            "register.unregister",
            "ensure.remove",
            "bind.release",
        )
        # The reverse-of-remove plan re-created what the partial stop had torn
        # down, in reverse order.
        assert [type(item).__name__ for item in ports2.calls] == [
            "BindAgentCommand",
            "EnsureHarnessCommand",
            "RegisterSessionCommand",
            "EnsureRouteCommand",
        ]
        assert second.drain(1)


def test_daemon_start_runtime_sweeps_before_lifecycle_consumers() -> None:
    """The wiring order the sweep depends on is pinned, not implicit.

    ``_start_runtime`` must expire the previous generation's receipts
    BEFORE the harness actor is constructed (its restore-deferral set is
    seeded from incomplete receipts) and before the lifecycle manager is
    constructed (its recovery must not resume).  A source-order gate, in
    the repo's established style.
    """

    native_root = Path(__file__).resolve().parents[4] / 'src/hyprial'
    if not Path(DaemonApplication._start_runtime.__code__.co_filename).is_relative_to(native_root):
        raise RuntimeError("Startup reader resolved outside the actual product tree")
    entry = ast.parse(textwrap.dedent(inspect.getsource(DaemonApplication._start_runtime)))
    phases = [node.func.attr for node in ast.walk(entry) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
              and node.func.value.id == "self" and node.func.attr.startswith("_startup_")]
    expected = ["_startup_prepare_state", "_startup_open_transport", "_startup_compose_delivery_plane",
                "_startup_compose_runtime_bridge", "_startup_adopt_domain_services", "_startup_finalize_runtime"]
    if phases != expected:
        raise AssertionError(f"Native startup phase execution order changed: {phases}")
    bridge = inspect.getsource(DaemonApplication._startup_compose_runtime_bridge)
    if "self._start_lifecycle_manager(" not in bridge:
        raise AssertionError("Lifecycle consumer no longer called by the runtime bridge phase")
    source = "\n".join(inspect.getsource(getattr(DaemonApplication, name)) for name in phases)
    source += inspect.getsource(DaemonApplication._start_lifecycle_manager)
    sweep = source.index("self._expire_previous_generation_lifecycle_state()")
    actor = source.index("harness_actor = HarnessRuntimeActor(")
    manager = source.index("manager = LifecycleCoordinator(")
    assert sweep < actor < manager


# ---------------------------------------------------------------------------
# Saga-layer doubles (same shapes as the process-manager suite)
# ---------------------------------------------------------------------------


class _CompletionPorts:
    """Fake four-domain port with the receipt ledger semantics."""

    def __init__(
        self,
        router: CorrelationEventRouter,
        *,
        receipts: dict[str, MutationProvenance] | None = None,
        current_tokens: dict[str, str] | None = None,
        fault_after_effect: object = None,
    ) -> None:
        self.router = router
        self.receipts = receipts if receipts is not None else {}
        self.current_tokens = current_tokens if current_tokens is not None else {}
        self.retired_receipts: dict[str, str] = {}
        self.calls: list[object] = []
        self.generation = 1
        self.version = 0

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        receipt = self.receipts.get(attempt_token)
        if receipt is None:
            retired = self.retired_receipts.get(attempt_token)
            if retired is None:
                return False
            if retired != resource_token:
                raise ValueError("test retired receipt token mismatch")
            return True
        if receipt.resource_token != resource_token:
            raise ValueError("test receipt token mismatch")
        self.receipts.pop(attempt_token, None)
        self.retired_receipts[attempt_token] = resource_token
        return True

    def confirm_receipt_retired(self, attempt_token: str, resource_token: str) -> None:
        retired = self.retired_receipts.get(attempt_token)
        if retired is None:
            return
        if retired != resource_token:
            raise ValueError("test retirement confirmation token mismatch")
        del self.retired_receipts[attempt_token]

    def submit(self, command: object) -> PortAdmission:
        assert isinstance(command, LifecycleMutationRequest)
        self.calls.append(command.payload)
        attempt = command.attempt_token
        payload = command.payload
        resource_key = _resource_key(payload)
        expected = command.expected_resource_token
        provenance = self.receipts.get(attempt)
        changed = True
        if expected is not None:
            current = self.current_tokens.get(resource_key, expected)
            if current != expected:
                changed = False
                provenance = MutationProvenance(False, False, current)
            else:
                provenance = MutationProvenance(True, True, expected)
                self.current_tokens.pop(resource_key, None)
        if provenance is None:
            provenance = MutationProvenance(changed, changed, f"resource:{attempt}")
        if expected is None and provenance.created_by_operation:
            self.current_tokens[resource_key] = provenance.resource_token
        self.receipts[attempt] = provenance
        self.router.publish(_completion(command, provenance))
        return PortAdmission.ACCEPTED

    def reassociate(
        self,
        attempt_token: str,
        *,
        correlation_id: str,
        generation: int,
        version: int,
    ) -> bool:
        return False


def _resource_key(payload: object) -> str:
    from hyprial.identity.impl.agents.actor.ports import (
        BindAgentCommand,
        CreateAgentCommand,
        DestroyAgentCommand,
        ReleaseAgentCommand,
    )
    from hyprial.daemon.impl.operations.session_ports import (
        RegisterSessionCommand,
        UnregisterSessionCommand,
    )

    if isinstance(payload, CreateAgentCommand | DestroyAgentCommand):
        return f"agent:{payload.name}"
    if isinstance(payload, BindAgentCommand | ReleaseAgentCommand):
        return f"binding:{payload.actor}"
    from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, RemoveHarnessCommand

    if isinstance(payload, EnsureHarnessCommand):
        return f"harness:{payload.spec.harness}:{payload.spec.name}"
    if isinstance(payload, RemoveHarnessCommand):
        return f"harness:{payload.harness}:{payload.name}"
    if isinstance(payload, RegisterSessionCommand | UnregisterSessionCommand):
        return f"session:{payload.actor}"
    from hyprial.daemon.impl.network.route_ports import (
        DropRouteCommand,
        EnsureRouteCommand,
    )

    if isinstance(payload, EnsureRouteCommand):
        return f"route:{payload.spec.route_id}"
    if isinstance(payload, DropRouteCommand):
        return f"route:{payload.route_id}"
    raise TypeError(f"unsupported lifecycle test resource: {type(payload).__name__}")


def _completion(command: LifecycleMutationRequest, provenance: MutationProvenance):
    from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted
    from hyprial.daemon.impl.harnesses.runtime.ports import HarnessMutationCompleted, RemoveHarnessCommand
    from hyprial.daemon.impl.network.route_ports import (
        DropRouteCommand,
        EnsureRouteCommand,
        RouteMutationCompleted,
    )
    from hyprial.daemon.impl.operations.session_ports import (
        RegisterSessionCommand,
        SessionMutationCompleted,
        SessionMutationProjection,
        UnregisterSessionCommand,
    )

    payload = command.payload
    if isinstance(payload, EnsureRouteCommand | DropRouteCommand):
        return RouteMutationCompleted(
            payload.correlation_id,
            payload.attempt_token,
            payload.generation,
            payload.version,
            "ensure" if isinstance(payload, EnsureRouteCommand) else "drop",
            payload.spec.route_id
            if isinstance(payload, EnsureRouteCommand)
            else payload.route_id,
            provenance.changed,
            provenance,
        )
    if isinstance(payload, EnsureHarnessCommand | RemoveHarnessCommand):
        base = HarnessMutationCompleted(
            command.correlation_id, 1, 1, "codex:worker", provenance.changed
        )
        return _lifecycle_completed(command, "harness", provenance, base)
    if isinstance(payload, RegisterSessionCommand | UnregisterSessionCommand):
        registered = isinstance(payload, RegisterSessionCommand)
        base = SessionMutationCompleted(
            command.correlation_id,
            1,
            1,
            SessionMutationProjection(
                "agent:owner:node:worker",
                "s1",
                "epoch",
                registered=registered and provenance.changed,
                unregistered=(not registered) and provenance.changed,
            ),
        )
        return _lifecycle_completed(command, "session", provenance, base)
    base = AgentMutationCompleted(
        command.correlation_id, 1, 1, type(payload).__name__, provenance.changed
    )
    return _lifecycle_completed(command, "agent", provenance, base)


def _lifecycle_completed(
    command: LifecycleMutationRequest,
    domain: str,
    provenance: MutationProvenance,
    payload: object,
):
    from hyprial.kernel import LifecycleMutationCompleted

    return LifecycleMutationCompleted(
        command.correlation_id,
        command.attempt_token,
        1,
        1,
        domain,
        provenance,
        payload,
    )


def _spec(name: str = "worker") -> LifecycleSpec:
    from hyprial.daemon.impl.lifecycle_manager import SessionLifecycleSpec

    actor = f"agent:owner:node:{name}"
    return LifecycleSpec(
        agent_name=name,
        actor=actor,
        harness=HarnessLaunchProjection("codex", name, True),
        route=RouteSpec(actor, f"live/{actor}", f"inbox/{actor}"),
        session=SessionLifecycleSpec(
            "/tmp", ("/bin/worker",), "test", f"session-{name}"
        ),
    )


def _manager(
    path: Path,
    ports: _CompletionPorts,
    router: CorrelationEventRouter,
    **kwargs: object,
) -> LifecycleProcessManager:
    return LifecycleProcessManager(
        path,
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.2,
        **kwargs,  # type: ignore[arg-type]
    )
