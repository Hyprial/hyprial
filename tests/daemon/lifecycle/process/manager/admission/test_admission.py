from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
import pytest
from hyprial.identity.impl.agents.actor.ports import BindAgentCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleOperationConflict, LifecyclePorts, LifecycleProcessManager, LifecycleSpec, LifecycleState
from hyprial.daemon.impl.route_registration import RouteRegistrationIo

from ..support import (
    TERMINAL_WAIT_SECONDS,
    _CompletionPorts,
    _RouteTransport,
    _StaleThenCurrentPorts,
    _TEST_MANAGERS,
    _close_test_managers as _close_test_managers,
    _drain_test_managers,
    _manager,
    _spec,
)


def test_manager_factory_registers_real_manager_for_common_cleanup(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    manager = _manager(tmp_path / "manager.sqlite3", _CompletionPorts(router), router)
    try:
        _drain_test_managers()
        assert manager._closed is True
        assert not manager._thread.is_alive()
    finally:
        manager.drain(1.0)


def test_manager_journal_uses_shared_state_persistence_authority(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.daemon.impl.state_db import StateDatabase
    from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority

    state_db = StateDatabase(tmp_path / "saga.sqlite3")
    desired = DesiredStateStore(tmp_path / "desired.json", state_db)
    authority = StatePersistenceAuthority(desired)
    router = CorrelationEventRouter()
    manager = _manager(
        state_db.path, _CompletionPorts(router), router,
        journal_store=authority.journal,
    )
    try:
        assert manager.state_db is state_db
        assert manager.submit(LifecycleOperation.create("shared-writer", _spec())) is PortAdmission.ACCEPTED
        assert manager.wait("shared-writer", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert manager.drain(1.0)
    finally:
        manager.drain(1.0)
        _TEST_MANAGERS.remove(manager)
        assert authority.close()


def test_operation_id_is_idempotent_and_digest_fenced(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    operation = LifecycleOperation.create("same-id", _spec())
    manager.submit(operation)
    assert manager.wait("same-id", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    calls = len(ports.calls)
    assert manager.submit(operation) is PortAdmission.ACCEPTED
    assert len(ports.calls) == calls
    with pytest.raises(LifecycleOperationConflict):
        manager.submit(LifecycleOperation.create("same-id", _spec("different")))
    assert manager.drain(1)


def test_headless_bind_carries_harness_session_ref(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    spec = _spec()
    spec = LifecycleSpec(
        spec.agent_name,
        spec.actor,
        HarnessLaunchProjection(
            "codex", "worker", True, session_ref="thread-1"
        ),
        spec.route,
        None,
    )
    assert manager.submit(LifecycleOperation.create("headless", spec)) is PortAdmission.ACCEPTED
    assert manager.wait("headless", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    bind = next(item for item in ports.calls if isinstance(item, BindAgentCommand))
    assert bind.session_id == "thread-1"
    assert manager.drain(1)


def test_overloaded_domain_admission_retries_without_counting_it_as_completion(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, overloads=3)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    manager.submit(LifecycleOperation.create("overload", _spec()))
    assert manager.wait("overload", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert ports.attempts == len(ports.calls) + 3
    assert manager.drain(1)


def test_old_lifecycle_database_is_migrated_forward_for_error_code(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE lifecycle_operations("
            "operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, "
            "request_json TEXT NOT NULL, state TEXT NOT NULL, error TEXT, "
            "updated_at REAL NOT NULL)"
        )
    router = CorrelationEventRouter()
    manager = _manager(path, _CompletionPorts(router), router)
    with sqlite3.connect(path) as database:
        columns = {
            str(row[1])
            for row in database.execute("PRAGMA table_info(lifecycle_operations)")
        }
    assert "error_code" in columns
    manager.submit(LifecycleOperation.create("migrated", _spec()))
    assert manager.wait("migrated", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert manager.drain(1)


def test_manager_and_real_route_io_share_committed_completion_router(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    domains = _CompletionPorts(router)
    transport = _RouteTransport()
    route = RouteRegistrationIo(
        transport,  # type: ignore[arg-type]
        lambda _route: lambda _key: b"ok",
        router,
    )
    manager = LifecycleProcessManager(
        tmp_path / "saga.sqlite3",
        LifecyclePorts(domains, domains, domains, route),
        router,
        completion_timeout=0.5,
    )
    try:
        operation = LifecycleOperation.create("route-integration", _spec())
        manager.submit(operation)
        assert manager.wait("route-integration", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert transport.liveliness_calls == 1
        assert transport.queryable_calls == 1
        assert manager.submit(operation) is PortAdmission.ACCEPTED
        assert transport.liveliness_calls == 1
        assert manager.drain(1)
        assert route.drain(1)
    finally:
        # Built directly, so the autouse factory cleanup never sees this
        # manager.  Without this, a failure above (e.g. a TimeoutError under
        # CI load) leaves the manager thread and its saga.sqlite3/-wal/-shm
        # handles open, and _fd_hygiene reports them against every later
        # test on the worker -- one timeout became 18 teardown ERRORs across
        # 16 unrelated files in task 15293.  Manager first: its in-flight
        # step still depends on the route port.  Both drains are idempotent.
        manager.drain(5.0)
        route.drain(5.0)


def test_non_route_waiter_rejects_old_generation_before_current_completion(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _StaleThenCurrentPorts(router)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    manager.submit(LifecycleOperation.create("generation-fence", _spec()))
    assert manager.wait("generation-fence", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert ports.sent_stale
    assert manager.drain(1)


def test_business_core_has_no_pykka_or_daemon_application_dependency() -> None:
    root = Path(__file__).resolve().parents[6]
    route_files = tuple(
        root / "src/hyprial/daemon/impl/route_registration" / name
        for name in ("__init__.py", "client.py", "commands.py", "io.py", "receipts.py")
    )
    manager_files = tuple(
        root / "src/hyprial/daemon/impl/lifecycle_manager" / name
        for name in ("__init__.py", "process.py", "steps.py", "store.py", "vocabulary.py")
    )
    files = (
        root / "src/hyprial/daemon/impl/correlation/correlation.py",
        *route_files,
        *manager_files,
    )
    imported: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not any(name == "pykka" or name.startswith("pykka.") for name in imported)
    assert "hyprial.daemon.impl.application" not in imported
    manager_source = "\n".join(path.read_text() for path in manager_files)
    assert "would_change" not in manager_source
    assert "created_by_operation = 1" in manager_source
    receipt_source = (root / "src/hyprial/daemon/impl/lifecycle/lifecycle_receipts.py").read_text()
    for domain in ("Agent", "Session", "Harness", "Route"):
        assert f"class {domain}LifecycleMutationPort" in receipt_source
    assert "expected_resource_token" in receipt_source
