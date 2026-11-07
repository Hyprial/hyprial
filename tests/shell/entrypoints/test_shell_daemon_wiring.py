"""Real upper composition and precise business-to-daemon error translations."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_upper_composition_uses_only_public_lower_faces():
    directory = ROOT / "src/hyprial/shell/impl/daemon_wiring"
    files = list(directory.glob("*.py"))
    assert files and len([path for path in directory.iterdir() if path.is_file()]) <= 7
    for path in files:
        source = path.read_text()
        assert len(source.splitlines()) <= 1000
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                assert not any(node.module.startswith(f"hyprial.{owner}.impl") for owner in ("kernel", "identity", "daemon", "biz")), path


def test_real_composition_symbols_have_canonical_public_bindings():
    from hyprial import biz, daemon
    from hyprial.biz.impl.routine.execution.service import RoutineService
    from hyprial.daemon.impl.application.ports import RoutinePortError, RoutineRuntime

    assert biz.RoutineService is RoutineService
    assert daemon.RoutinePortError is RoutinePortError
    assert daemon.RoutineRuntime is RoutineRuntime


@pytest.mark.parametrize("method,arguments", [
    ("recover", {}), ("list", {}), ("status", {"name": "x"}),
    ("add", {"yaml_text": "bad", "owner": "user:test", "enabled": True}),
    ("remove", {"name": "x", "reservation_id": "reservation"}),
    ("reserve_remove", {"name": "x", "reservation_id": "reservation", "enforce_last": True}),
    ("cancel_remove", {"name": "x", "reservation_id": "reservation"}),
    ("pause", {"name": "x"}), ("resume", {"name": "x", "align_schedule": True}),
    ("set", {"name": "x", "yaml_text": "bad"}), ("address_migrations", {}),
    ("close", {}),
    ("submit_timer", {"observed_at_ms": 123}),
])
def test_service_adapter_preserves_code_data_chain_and_arguments(method, arguments):
    from hyprial.biz import RoutineServiceError
    from hyprial.daemon import RoutinePortError
    from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter

    original = RoutineServiceError("ROUTINE_TEST", "specific detail", (("actor", "x"),))
    calls = []

    def fail(**keywords):
        calls.append(keywords)
        raise original

    adapter = RoutineServiceAdapter(SimpleNamespace(**{method: fail}))
    with pytest.raises(RoutinePortError) as caught:
        getattr(adapter, method)(**arguments)
    assert calls == [arguments]
    assert caught.value.code == original.code
    assert caught.value.data is original.data
    assert str(caught.value) == str(original)
    assert caught.value.__cause__ is original


def test_service_close_does_not_invent_success_and_unrelated_errors_propagate():
    from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter

    assert RoutineServiceAdapter(SimpleNamespace(close=lambda: None)).close() is None
    original = ValueError("not a routine failure")

    def fail():
        raise original

    with pytest.raises(ValueError) as caught:
        RoutineServiceAdapter(SimpleNamespace(list=fail)).list()
    assert caught.value is original


def test_coordinator_timeout_is_canonical_and_close_forwards_timeout():
    from hyprial.biz import RoutineCoordinatorTimeout
    from hyprial.daemon import RoutinePortError, RoutinePortTimeout
    from hyprial.shell.impl.daemon_wiring.routine_coordinator import RoutineCoordinatorAdapter

    original = RoutineCoordinatorTimeout("operation-17")
    calls = []

    def wait(operation_id, *, timeout):
        calls.append((operation_id, timeout))
        raise original

    adapter = RoutineCoordinatorAdapter(SimpleNamespace(wait=wait, close=lambda *, timeout: timeout))
    with pytest.raises(RoutinePortTimeout) as caught:
        adapter.wait("operation-17", timeout=0.02)
    assert calls == [("operation-17", 0.02)]
    assert isinstance(caught.value, RoutinePortError)
    assert caught.value.operation_id == "operation-17"
    assert caught.value.__cause__ is original
    assert adapter.close(0.25) == 0.25


def test_real_usage_factory_never_allocates_collectors_when_disabled_or_isolated(monkeypatch):
    from hyprial.shell.impl.daemon_wiring import usage

    def forbidden():
        raise AssertionError("collector allocated under disabled/isolated startup")

    monkeypatch.setattr(usage, "UsageAuthority", forbidden)
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    monkeypatch.delenv("HYPRIAL_USAGE_DISABLE", raising=False)
    assert usage.usage_factory() is None
    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED")
    monkeypatch.setenv("HYPRIAL_USAGE_DISABLE", "1")
    assert usage.usage_factory() is None


def test_enabled_factory_constructs_real_usage_authority_without_starting_collectors(monkeypatch):
    from hyprial.biz import UsageAuthority
    from hyprial.biz.impl import usage as policy
    from hyprial.shell.impl.daemon_wiring.usage import usage_factory

    def forbidden(*arguments, **keywords):
        raise AssertionError("construction read a credential or opened a remote quota endpoint")

    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED", raising=False)
    monkeypatch.delenv("HYPRIAL_USAGE_DISABLE", raising=False)
    monkeypatch.setattr(policy, "_read_json_file", forbidden)
    monkeypatch.setattr(policy, "_http_get_json", forbidden)
    authority = usage_factory()
    try:
        assert isinstance(authority, UsageAuthority)
        assert not authority._started
        assert authority.snapshots() == ()
    finally:
        assert authority.stop(timeout=2)


def test_routine_schema_errors_are_not_leaked_up_the_daemon_port():
    from hyprial.biz import RoutineSchemaError
    from hyprial.daemon import RoutineSchemaPortError
    from hyprial.shell.impl.daemon_wiring.routines import routine_text_loader

    with pytest.raises(RoutineSchemaPortError) as caught:
        routine_text_loader("not: a routine")
    assert caught.value.code == "ROUTINE_SCHEMA_ERROR"
    assert isinstance(caught.value.__cause__, RoutineSchemaError)


def test_real_quota_evaluator_does_not_commit_before_daemon_settlement(tmp_path):
    from hyprial.biz import QuotaWatchdog
    from hyprial.biz.impl.usage import QuotaWindow, SourceSnapshot
    from hyprial.daemon import QuotaWatchdogDeps
    from hyprial.shell.impl.daemon_wiring.quota import quota_evaluator_factory

    deliveries = []
    snapshots = (SourceSnapshot("claude", True, 0, windows=(QuotaWindow("session", "session", 95, 100, 1000),)),)
    deps = QuotaWatchdogDeps(
        state_dir=tmp_path, deliver=lambda key, text: deliveries.append((key, text)) or True,
        readings=lambda: snapshots, clock_ms=lambda: 0, on_alert=lambda alert: None,
    )
    evaluator = quota_evaluator_factory(deps)
    assert isinstance(evaluator, QuotaWatchdog)
    assert evaluator._readings is deps.readings
    assert evaluator._clock_ms is deps.clock_ms
    alerts = evaluator.observe_readings()
    assert alerts and deliveries
    assert not (tmp_path / "quota-watchdog.json").exists()
    assert json.dumps(evaluator._state)


def test_real_workflow_and_routine_service_factory(tmp_path):
    from hyprial.biz import RoutineService
    from hyprial.daemon import RoutineRuntimeDeps
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from hyprial.shell.impl.daemon_wiring.routines import routine_runtime_factory

    workflow = GraphWorkflowService(
        state_dir=tmp_path, owner="test", machine="node", sender=lambda notice: True,
        admit=lambda **keywords: None, clock_ms=lambda: 0, start_thread=False,
    )
    runtime = None
    try:
        deps = RoutineRuntimeDeps(
            state_dir=tmp_path, hyprial_home=tmp_path / "home", owner="test",
            logger=None, alarm=SimpleNamespace(escalate=lambda **keywords: None),
            deliver_task=lambda **keywords: True, clock_ms=lambda: 0,
            resolve_principal=lambda actor: actor, migrate_address=lambda actor: actor,
            workflow_service=workflow,
        )
        runtime = routine_runtime_factory(deps)
        assert runtime is not None
        assert isinstance(runtime.service.service, RoutineService)
        assert runtime.service.service._pac._workflow is workflow
        assert runtime.coordinator is None
        assert runtime.routine_exists("missing") is False
        assert runtime.service.list() == {"routines": []}
        assert runtime.service.close() is None
    finally:
        if runtime is not None:
            runtime.service.close()
        workflow.close()


def test_allocated_real_service_is_closed_if_coordinator_construction_fails(tmp_path, monkeypatch):
    from hyprial.daemon import RoutineRuntimeDeps
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from hyprial.shell.impl.daemon_wiring import routines

    original = RuntimeError("coordinator construction failed")
    services = []
    constructor = routines.RoutineService

    def service(**options):
        created = constructor(**options)
        services.append(created)
        return created

    def fail(**options):
        assert options["routines"] is services[0]
        raise original

    monkeypatch.setattr(routines, "RoutineService", service)
    monkeypatch.setattr(routines, "RoutineCoordinator", fail)
    workflow = GraphWorkflowService(
        state_dir=tmp_path, owner="test", machine="node", sender=lambda notice: True,
        clock_ms=lambda: 0, start_thread=False,
    )
    try:
        deps = RoutineRuntimeDeps(
            state_dir=tmp_path, hyprial_home=tmp_path / "home", owner="test",
            logger=None, alarm=SimpleNamespace(escalate=lambda **keywords: None),
            deliver_task=lambda **keywords: True, clock_ms=lambda: 0,
            resolve_principal=lambda actor: actor, migrate_address=lambda actor: actor,
            workflow_service=workflow, graph_authority=object(),
        )
        with pytest.raises(RuntimeError) as caught:
            routines.routine_runtime_factory(deps)
        assert caught.value is original
        assert services[0]._projection_closed
        assert not services[0]._effect_thread.is_alive()
        assert not any(worker.is_alive() for worker in services[0]._effect_workers)
    finally:
        for allocated in services:
            allocated.close()
        workflow.close()


def test_real_coordinator_uses_underlying_service_with_real_pac_authority(tmp_path):
    from hyprial.biz import RoutineCoordinator
    from hyprial.daemon import RoutineRuntimeDeps
    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
    from hyprial.daemon.impl.pac.storage.store import default_database_path
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from hyprial.shell.impl.daemon_wiring.routines import routine_runtime_factory

    authority = PacGraphAuthority(default_database_path(tmp_path), SimpleNamespace(send=lambda notice: True))
    workflow = None
    runtime = None
    try:
        workflow = GraphWorkflowService(
            state_dir=tmp_path, owner="test", machine="node", sender=SimpleNamespace(send=lambda notice: True),
            clock_ms=lambda: 0, graph_authority=authority,
        )
        deps = RoutineRuntimeDeps(
            state_dir=tmp_path, hyprial_home=tmp_path / "home", owner="test",
            logger=None, alarm=SimpleNamespace(escalate=lambda **keywords: None),
            deliver_task=lambda **keywords: True, clock_ms=lambda: 0,
            resolve_principal=lambda actor: actor, migrate_address=lambda actor: actor,
            workflow_service=workflow, graph_authority=authority,
            ensure_coordinator=lambda routine: {}, retire_coordinator=lambda routine: {},
            close_graph=lambda graph, actor: authority.close_graph(graph, actor=actor),
        )
        runtime = routine_runtime_factory(deps)
        assert isinstance(runtime.coordinator.coordinator, RoutineCoordinator)
        assert runtime.coordinator.coordinator.routines is runtime.service.service
        assert runtime.coordinator.close(timeout=2)
    finally:
        if runtime is not None:
            if runtime.coordinator is not None:
                runtime.coordinator.close(timeout=2)
            runtime.service.close()
        if workflow is not None:
            workflow.close()
        authority.close(2)


def test_quota_hook_consumer_factory_wraps_the_authority():
    """H4: the shell hands the daemon a biz consumer around its watchdog."""

    from hyprial.biz import QuotaHookConsumer
    from hyprial.shell.impl.daemon_wiring.quota import quota_hook_consumer_factory

    class _Watchdog:
        def observe_readings(self): ...
        def observe_usage_limit_failure(self, actor): ...
        def flush_failures(self): ...

    consumer = quota_hook_consumer_factory(_Watchdog())
    assert isinstance(consumer, QuotaHookConsumer)
