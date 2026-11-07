from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

import hyprial.daemon.impl.application.actors.lifecycle_ops as daemon_application
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.composition import (
    AgentIdentityProjectionView,
    CorrelatedDomainEvents,
    HarnessPortClient,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    HarnessMutationCompleted,
    HarnessStatusProjection,
    RemoveHarnessCommand,
    harness_desired_generation,
)
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecycleResult,
    LifecycleSpec,
    LifecycleState,
)
from hyprial.daemon.impl.network.route_ports import RouteSpec
from hyprial.daemon.impl.route_registration import RouteCommandError
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

#: Budget for waiting on a lifecycle operation's terminal state -- the same
#: ruler the application uses (operation deadline + wait margin).  ``wait``
#: only polls the durable result and never changes the saga, so this is not a
#: latency claim; literal 2/3/5s budgets went red under CI load (dev task
#: 15834, #600 task 15852, #605 task 15843: "lifecycle operation still running").
TERMINAL_WAIT_SECONDS = LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS


def _close_application(app: DaemonApplication) -> None:
    app._close()
    app._close_home_lease()


def _isolated_application(
    *,
    state_dir: Path,
    socket_path: Path,
    node_id: str,
    owner: str,
    hyprial_home: Path | None = None,
    network_isolated: bool,
) -> DaemonApplication:
    """Construct a daemon fixture with explicit, effective local-only policy."""

    assert network_isolated is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=socket_path,
        node_id=node_id,
        owner=owner,
        hyprial_home=hyprial_home,
        zenoh_listen=(endpoint,),
        zenoh_connect=(),
    **_deep_module_daemon_dependencies())
    app.network_isolated = network_isolated
    assert app.network_isolated
    assert app.zenoh_listen == (endpoint,)
    assert app.zenoh_connect == ()
    return app



@dataclass
class _Registration:
    closed: bool = False
    close_failures: int = 0
    close_calls: int = 0

    def close(self, **_kwargs: object) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("injected registration close failure")
        self.closed = True


class _Transport:
    def __init__(self, *, liveliness_close_failures: int = 0) -> None:
        self.registrations: list[_Registration] = []
        self.liveliness_close_failures = liveliness_close_failures

    def _registration(self) -> _Registration:
        item = _Registration()
        self.registrations.append(item)
        return item

    def declare_liveliness(self, _key: str) -> _Registration:
        item = _Registration(close_failures=self.liveliness_close_failures)
        self.liveliness_close_failures = 0
        self.registrations.append(item)
        return item

    def subscribe(self, _key: str, _callback: object) -> _Registration:
        return self._registration()

    def declare_queryable(self, _key: str, _handler: object) -> _Registration:
        return self._registration()


class _LocalDelivery:
    def __init__(self) -> None:
        self.registrations: list[_Registration] = []

    def register_actor(self, _actor: str) -> _Registration:
        registration = _Registration()
        self.registrations.append(registration)
        return registration


class _Inbox:
    def receive(self, _message: object) -> None:
        return None

    def has_received(self, _message_id: str) -> bool:
        return False

    def has_fetched(self, _message_id: str) -> bool:
        return False


@dataclass
class _Process:
    running: bool = True
    pid: int | None = None

    def stop(self) -> None:
        self.running = False


class _Launcher:
    def start(self, _spec: HarnessLaunchSpec) -> _Process:
        return _Process()


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.01)


class _Presence:
    def actor_online(self, _actor: str) -> bool:
        return False


class _HarnessActorProjection:
    def __init__(self, harnesses: _HarnessPort) -> None:
        self._harnesses = harnesses

    def read_harnesses(self) -> tuple[HarnessStatusProjection, ...]:
        return tuple(self._harnesses.rows.values())


class _HarnessPort:
    def __init__(self) -> None:
        self.generation = 1
        self.version = 0
        self.rows: dict[str, HarnessStatusProjection] = {}
        self.actor = _HarnessActorProjection(self)

    def call(self, command: object, expected: object) -> HarnessMutationCompleted:
        assert expected is HarnessMutationCompleted
        self.version += 1
        if isinstance(command, EnsureHarnessCommand):
            harness_id = f"{command.spec.harness}:{command.spec.name}"
            changed = harness_id not in self.rows
            self.rows[harness_id] = HarnessStatusProjection(
                self.version,
                harness_id,
                command.spec.harness,
                command.spec.name,
                True,
            )
        else:
            assert isinstance(command, RemoveHarnessCommand)
            harness_id = f"{command.harness}:{command.name}"
            changed = self.rows.pop(harness_id, None) is not None
        return HarnessMutationCompleted(
            command.correlation_id,
            self.generation,
            self.version,
            harness_id,
            changed,
        )


def test_assembled_application_wires_one_state_database_to_both_stores(
    tmp_path: Path, request,
) -> None:
    """The U0a-2 assembly invariant, walked from the assembly point DOWN.

    The construction signature alone proves nothing: every standalone
    DesiredStateStore defaults to its own StateDatabase, and two connections
    make "one transaction across desired state and the journal" silently
    impossible.  What must hold is object identity on the ASSEMBLED
    application: the daemon hands the same StateDatabase instance to both
    stores, so the single-transaction property is a fact about constructed
    objects.  Reachability: application.desired_state and
    application._lifecycle_manager.state_db.
    """

    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    harness_events = CorrelatedDomainEvents()
    harness_actor = HarnessRuntimeActor(
        _Launcher(), event_sink=harness_events, desired_state=app.desired_state
    )
    harnesses = HarnessPortClient(harness_actor, harness_events)
    app._harnesses = harnesses
    app._start_lifecycle_manager(
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=_Inbox(),  # type: ignore[arg-type]
        local_delivery=_LocalDelivery(),  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
    )
    manager = app._lifecycle_manager
    assert manager is not None
    assert manager.state_db is app.desired_state.state_db


def test_agent_home_uses_explicit_hyprial_home_not_state_parent(
    tmp_path: Path, request,
) -> None:
    home = tmp_path / "configured-home"
    state = tmp_path / "separate-state"
    app = _isolated_application(
        state_dir=state,
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        hyprial_home=home,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    try:
        # Drive the real composed registry (not InlineLifecycleManager), so the
        # home wiring under test is exactly the production one the daemon builds.
        registry = app._agent_session_domains._registry
        agent = registry.create("worker")
        receipt = registry.home_receipt("worker")
        assert Path(receipt.path) == home / "agents" / "worker"
        assert receipt.entity_token == agent.entity_token
        assert (home / "agents" / "worker" / "state" / "home-receipt.json").is_file()
        assert not (state.parent / "agents" / "worker").exists()
    finally:
        pass


def test_production_lifecycle_composition_runs_all_four_domain_ports(
    tmp_path: Path, request,
) -> None:
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    transport = _Transport()
    harness_events = CorrelatedDomainEvents()
    harness_actor = HarnessRuntimeActor(
        _Launcher(), event_sink=harness_events, desired_state=app.desired_state
    )
    harnesses = HarnessPortClient(harness_actor, harness_events)
    app._harnesses = harnesses
    local_delivery = _LocalDelivery()
    app._start_lifecycle_manager(
        transport=transport,  # type: ignore[arg-type]
        inbox=_Inbox(),  # type: ignore[arg-type]
        local_delivery=local_delivery,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
    )
    assert app._lifecycle_manager is not None
    operation = LifecycleOperation.create(
        "composition-create",
        LifecycleSpec(
            agent_name="worker",
            actor="agent:owner:node-1:worker",
            harness=HarnessLaunchProjection("pi", "worker", True),
            route=RouteSpec(
                "agent:owner:node-1:worker",
                "live/worker",
                "inbox/worker",
            ),
        ),
    )
    assert app._lifecycle_manager.submit(operation) is PortAdmission.ACCEPTED
    result = app._lifecycle_manager.wait(operation.operation_id, TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPLETED
    assert result.completed_effects == (
        "agent.create",
        "route.persona.ensure",
        "agent.bind",
        "harness.ensure",
        "route.managed.ensure",
    )
    assert app.agents.get("worker") is not None
    assert any(item.harness_id == "pi:worker" for item in harness_actor.read_harnesses())
    assert app.desired_state.load().harnesses[0].name == "worker"
    assert len(local_delivery.registrations) == 1
    assert len(transport.registrations) == 2
    assert app._routes is not None
    assert app._routes.owners(operation.target.actor) == (
        f"managed:{operation.target.actor}",
        f"persona:{operation.target.actor}",
    )

    deactivate = LifecycleOperation.deactivate(
        "composition-deactivate", operation.target
    )
    assert app._lifecycle_manager.submit(deactivate) is PortAdmission.ACCEPTED
    assert app._lifecycle_manager.wait(deactivate.operation_id, TERMINAL_WAIT_SECONDS).state is (
        LifecycleState.COMPLETED
    )
    assert app._routes.owners(operation.target.actor) == (
        f"persona:{operation.target.actor}",
    )
    assert not any(item.closed for item in transport.registrations)
    assert not local_delivery.registrations[0].closed

    remove = LifecycleOperation.remove("composition-remove", operation.target)
    assert app._lifecycle_manager.submit(remove) is PortAdmission.ACCEPTED
    assert app._lifecycle_manager.wait(remove.operation_id, TERMINAL_WAIT_SECONDS).state is (
        LifecycleState.COMPLETED
    )
    assert app._routes.owners(operation.target.actor) == ()
    assert all(item.closed for item in transport.registrations)
    assert local_delivery.registrations[0].closed


def test_explicit_lifecycle_start_retires_same_entity_restore_aba(
    tmp_path: Path, request,
) -> None:
    """A prior A suppression cannot revive after explicit A -> B -> A."""

    class _TrackingLauncher:
        def __init__(self) -> None:
            self.processes: list[_Process] = []
            self.specs: list[HarnessLaunchSpec] = []

        def start(self, spec: HarnessLaunchSpec) -> _Process:
            process = _Process()
            self.processes.append(process)
            self.specs.append(spec)
            return process

    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    launcher = _TrackingLauncher()
    harness_events = CorrelatedDomainEvents()
    harness_actor = HarnessRuntimeActor(
        launcher,
        event_sink=harness_events,
        desired_state=app.desired_state,
        agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
        restart_backoff_seconds=0,
    )
    harnesses = HarnessPortClient(harness_actor, harness_events)
    app._harnesses = harnesses
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    app._start_lifecycle_manager(
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=app._inbox,  # type: ignore[arg-type]
        local_delivery=_LocalDelivery(),  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
    )

    spec_a = HarnessLaunchSpec("codex", "worker", True, args=("--episode-a",))
    spec_b = HarnessLaunchSpec("codex", "worker", True, args=("--episode-b",))
    agent = app.agents.create("worker")
    app.desired_state.upsert_harness(spec_a)
    app.agents.suppress_restore(
        agent.actor,
        desired_generation=harness_desired_generation(spec_a),
        last_active_at_ms=1,
        idle_age_ms=20,
        restore_threshold_ms=10,
    )
    app._publish_restore_eligibility(
        spec=spec_a,
        entity_token=agent.entity_token,
        suppressed=True,
    )
    _wait_for(
        lambda: (
            harness_actor._restore_eligibility.get("worker") is not None
            and harness_actor._restore_eligibility["worker"].suppressed
        )
    )

    result_b = app.handle("lifecycle.start", spec_b.to_json())
    assert app.desired_state.load().harnesses == (spec_b,)
    assert app.agents.require("worker").entity_token == agent.entity_token
    result_a = app.handle("lifecycle.start", spec_a.to_json())
    assert app.desired_state.load().harnesses == (spec_a,)
    assert result_b["changed"] is True
    assert result_a["changed"] is True
    projection = app.agents.projection("worker")
    assert projection is not None
    assert projection.entity_token == agent.entity_token
    assert projection.restore_disposition is None
    _wait_for(
        lambda: (
            harness_actor._restore_eligibility.get("worker") is not None
            and not harness_actor._restore_eligibility["worker"].suppressed
            and harness_actor._restore_eligibility["worker"].desired_generation
            == harness_desired_generation(spec_a)
        )
    )
    cached_after_a = harness_actor._restore_eligibility.get("worker")
    assert cached_after_a is not None
    assert cached_after_a.suppressed is False
    assert cached_after_a.desired_generation == harness_desired_generation(spec_a)
    assert len(launcher.processes) == 2

    launcher.processes[-1].running = False
    harnesses.reconcile()
    _wait_for(lambda: len(launcher.processes) == 3)
    assert launcher.specs[-1] == spec_a


def test_production_route_factory_retains_partial_compound_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request,
) -> None:
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    transport = _Transport(liveliness_close_failures=1)
    local_delivery = _LocalDelivery()
    harness_events = CorrelatedDomainEvents()
    harness_actor = HarnessRuntimeActor(
        _Launcher(), event_sink=harness_events, desired_state=app.desired_state
    )
    harnesses = HarnessPortClient(harness_actor, harness_events)
    app._harnesses = harnesses
    endpoint_calls = 0
    successful_endpoints: list[_Registration] = []

    def endpoint_factory(*_args: object, **_kwargs: object) -> _Registration:
        nonlocal endpoint_calls
        endpoint_calls += 1
        if endpoint_calls == 1:
            raise RuntimeError("injected endpoint construction failure")
        endpoint = _Registration()
        successful_endpoints.append(endpoint)
        return endpoint

    monkeypatch.setattr(daemon_application, "ZenohInboxEndpoint", endpoint_factory)
    app._start_lifecycle_manager(
        transport=transport,  # type: ignore[arg-type]
        inbox=_Inbox(),  # type: ignore[arg-type]
        local_delivery=local_delivery,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
    )
    assert app._routes is not None
    spec = RouteSpec(
        "agent:owner:node-1:partial",
        "live/partial",
        "inbox/partial",
    )

    with pytest.raises(RouteCommandError) as failure:
        app._routes.ensure(spec, owner_lease="session:first")
    assert failure.value.code == "ROUTE_IO_FAILED"
    assert len(local_delivery.registrations) == 1
    assert local_delivery.registrations[0].closed
    assert len(transport.registrations) == 1
    assert transport.registrations[0].close_calls == 2
    assert transport.registrations[0].closed

    replacement = app._routes.ensure(
        spec, owner_lease="session:replacement"
    )
    assert replacement.changed
    assert app._routes.owners(spec.route_id) == ("session:replacement",)
    assert len(local_delivery.registrations) == 2
    assert len(transport.registrations) == 2
    assert len(successful_endpoints) == 1

def test_application_preserves_lifecycle_domain_error_code(
    tmp_path: Path, request,
) -> None:
    class _RejectedManager:
        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPENSATED,
                completed_effects=(),
                compensated_effects=(),
                error="agent.bind failed",
                error_code="AGENT_ALREADY_RUNNING",
            )

    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    app._lifecycle_manager = _RejectedManager()  # type: ignore[assignment]
    operation = LifecycleOperation.create(
        "rejected",
        LifecycleSpec(
            agent_name="worker",
            actor="agent:owner:node-1:worker",
            harness=HarnessLaunchProjection("pi", "worker", True),
            route=RouteSpec("agent:owner:node-1:worker", "live/worker", "inbox/worker"),
        ),
    )
    try:
        app._run_lifecycle_operation(operation)
    except DaemonRequestError as error:
        assert error.code == "AGENT_ALREADY_RUNNING"
    else:
        raise AssertionError("structured lifecycle rejection was not surfaced")
    finally:
        app._lifecycle_manager = None


def _lifecycle_create_operation(operation_id: str = "op") -> LifecycleOperation:
    return LifecycleOperation.create(
        operation_id,
        LifecycleSpec(
            agent_name="worker",
            actor="agent:owner:node-1:worker",
            harness=HarnessLaunchProjection("pi", "worker", True),
            route=RouteSpec("agent:owner:node-1:worker", "live/worker", "inbox/worker"),
        ),
    )


def test_lifecycle_failures_are_named_by_what_happened(
    tmp_path: Path, request,
) -> None:
    """2026-09-14: a dead lifecycle consumer thread surfaced every lifecycle
    timeout as DAEMON_START_FAILED (a `down` reported "start failed").  Each
    failure now carries the code of what actually happened."""

    import pytest

    from hyprial.kernel import ipc_errors

    class _CrashedManager:
        crashed = True

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.CLOSING

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            raise AssertionError("a refused operation is never waited on")

    class _SlowManager:
        crashed = False

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            raise TimeoutError(f"lifecycle operation still running: {operation_id}")

    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    operation = _lifecycle_create_operation()
    try:
        app._lifecycle_manager = None
        with pytest.raises(DaemonRequestError) as absent:
            app._run_lifecycle_operation(operation)
        assert absent.value.code == ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE

        app._lifecycle_manager = _CrashedManager()  # type: ignore[assignment]
        with pytest.raises(DaemonRequestError) as crashed:
            app._run_lifecycle_operation(operation)
        assert crashed.value.code == ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE

        app._lifecycle_manager = _SlowManager()  # type: ignore[assignment]
        with pytest.raises(DaemonRequestError) as unsettled:
            app._run_lifecycle_operation(operation)
        assert unsettled.value.code == ipc_errors.LIFECYCLE_OPERATION_UNSETTLED
    finally:
        app._lifecycle_manager = None


def test_ps_reports_lifecycle_thread_health(tmp_path: Path, request) -> None:
    """The degraded state must be visible to operators: ps carries the
    lifecycle consumer thread's running/crashed/lastError."""

    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    try:
        assert app._lifecycle_status() == {
            "running": False,
            "crashed": False,
            "lastError": None,
        }

        class _CrashedManager:
            crashed = True
            is_running = False
            last_error = "sqlite3.OperationalError: database is locked"

        app._lifecycle_manager = _CrashedManager()  # type: ignore[assignment]
        assert app._lifecycle_status() == {
            "running": False,
            "crashed": True,
            "lastError": "sqlite3.OperationalError: database is locked",
        }
    finally:
        app._lifecycle_manager = None


def test_settled_start_failure_is_reported_to_the_cli() -> None:
    """A launcher handle is not proof of a stable start.

    Streaming harnesses can refuse on the next reconcile tick (Pi P2 trust is
    the production case); the CLI must see the daemon's recorded error instead
    of a successful lifecycle result.
    """

    from hyprial.daemon.impl.application.actors.lifecycle_ops import (
        _visible_start_failure,
    )

    class Harnesses:
        rows: list[dict[str, object]] = [
            {"runtime": "pi", "name": "worker", "running": True}
        ]

        def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
            assert (harness, name) == ("pi", "worker")
            assert timeout == 1.0
            # The real Pi P2 refusal lands after the launcher handle is ready.
            time.sleep(0.15)
            self.rows[0]["error"] = "trust refused"
            return False

        def status(self) -> tuple[dict[str, object], ...]:
            return tuple(self.rows)

    app = object.__new__(DaemonApplication)
    app._harnesses = Harnesses()

    with pytest.raises(DaemonRequestError) as failure:
        app._observe_start_readiness("pi", "worker")

    assert failure.value.code == "HARNESS_START_FAILED"
    assert "trust refused" in str(failure.value)
    assert _visible_start_failure(
        ({"runtime": "pi", "name": "worker", "running": True},), "pi", "worker"
    ) is None


def test_start_readiness_is_not_inferred_from_a_launcher_handle() -> None:
    class Harnesses:
        def __init__(self, ready: bool) -> None:
            self.ready = ready

        def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
            return self.ready

        def status(self) -> tuple[()]:
            return ()

    app = object.__new__(DaemonApplication)
    app._harnesses = Harnesses(True)
    assert app._observe_start_readiness("pi", "worker") is True

    app._harnesses = Harnesses(False)
    assert app._observe_start_readiness("pi", "worker") is False
