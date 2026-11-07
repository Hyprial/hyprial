from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.runtime import DaemonEventBridge


SOURCE_ROOT = Path(__file__).resolve().parents[4] / 'src/hyprial'
APPLICATION = SOURCE_ROOT / 'daemon/impl/application'


def _application_source() -> str:
    paths = sorted(APPLICATION.rglob('*.py'))
    assert paths and (APPLICATION / '__init__.py') in paths
    return '\n'.join(path.read_text(encoding='utf-8') for path in paths)


def _tree() -> ast.Module:
    return ast.parse(_application_source())


def test_application_does_not_import_raw_domain_owners() -> None:
    forbidden = {
        "InboxService",
        "AgentRegistry",
        "AgentLiveness",
    }
    imported = {
        alias.name
        for node in ast.walk(_tree())
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint(forbidden)


def test_application_has_no_legacy_session_runtime_maps() -> None:
    forbidden = {
        "_channel_current_epochs",
        "_interactive_heartbeats",
        "_channel_observations",
    }
    attributes = {
        node.attr for node in ast.walk(_tree()) if isinstance(node, ast.Attribute)
    }
    assert attributes.isdisjoint(forbidden)


def test_application_has_no_legacy_workflow_or_sync_adapter_calls() -> None:
    source = _application_source()
    assert "def _workflow_deliver(" not in source
    forbidden = {"start", "remove", "reload_gateways", "reconcile", "status"}
    violations = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == "_adapters"
            and node.func.attr in forbidden
        ):
            violations.append((node.lineno, node.func.attr))
    assert violations == []


def test_workflow_service_has_no_legacy_delivery_or_ack_fallbacks() -> None:
    service = SOURCE_ROOT / 'daemon/impl/pac/workflows/runtime'
    paths = sorted(service.rglob('*.py'))
    assert paths and (service / '__init__.py') in paths
    source = '\n'.join(path.read_text(encoding='utf-8') for path in paths)
    assert "_LegacyCallableDeliveryIo" not in source
    assert "_LegacyAcknowledgeIo" not in source
    assert "_LegacyInboxProjection" not in source


def test_inbox_coordinator_receives_event_sink_not_bound_function() -> None:
    calls = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DeliveryCustodyCoordinator"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) >= 3
    sink = calls[0].args[2]
    assert isinstance(sink, ast.Name)
    assert sink.id == "shared_inbox_events"


def _assert_runtime_inbox_composition(tree: ast.Module) -> None:
    def unique_class(name: str) -> ast.ClassDef:
        found = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
        assert len(found) == 1
        return found[0]

    def method(owner: ast.ClassDef, name: str) -> ast.FunctionDef:
        found = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == name]
        assert len(found) == 1
        return found[0]

    application = unique_class('DaemonApplication')
    assert sum(isinstance(base, ast.Name) and base.id == '_WiringTransportMixin'
               for base in application.bases) == 1
    assert any(isinstance(node, ast.ImportFrom)
               and node.module == 'hyprial.daemon.impl.application.wiring.transport'
               and any(alias.name == '_WiringTransportMixin' for alias in node.names)
               for node in tree.body)
    wiring = unique_class('_WiringTransportMixin')
    startup = method(wiring, '_start_runtime')
    plane = method(wiring, '_startup_compose_delivery_plane')
    bridge_method = method(wiring, '_startup_compose_runtime_bridge')
    graph_bindings = [node for node in ast.walk(startup) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'graph' for target in node.targets)]
    assert len(graph_bindings) == 1
    assert isinstance(graph_bindings[0].value, ast.Call)
    assert ast.unparse(graph_bindings[0].value.func) == 'self._startup_compose_delivery_plane'
    forwards = [node for node in ast.walk(startup) if isinstance(node, ast.Call)
                and ast.unparse(node.func) == 'self._startup_compose_runtime_bridge']
    assert len(forwards) == 1 and ast.unparse(forwards[0].args[0]) == 'graph'
    bindings = [
        node for node in ast.walk(plane)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "inbox"
                for target in node.targets)
    ]
    assert len(bindings) == 1
    value = bindings[0].value
    assert isinstance(value, ast.Call)
    assert isinstance(value.func, ast.Name)
    assert value.func.id == "DeliveryCustodyFacade"
    assert isinstance(value.args[0], ast.Name)
    assert value.args[0].id == "inbox_coordinator"
    graphs = [node for node in ast.walk(plane) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == '_StartupGraph']
    assert len(graphs) == 1
    graph_arguments = {item.arg: item.value for item in graphs[0].keywords}
    assert isinstance(graph_arguments.get('inbox'), ast.Name)
    assert graph_arguments['inbox'].id == 'inbox'
    bridges = [
        node for node in ast.walk(bridge_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DaemonEventBridge"
    ]
    assert len(bridges) == 1
    arguments = {item.arg: item.value for item in bridges[0].keywords}
    assert isinstance(arguments.get("inbox"), ast.Attribute)
    assert ast.unparse(arguments['inbox']) == 'graph.inbox'
    assert isinstance(arguments.get("actor_mode"), ast.Constant)
    assert arguments["actor_mode"].value is True
    retained = [node for node in ast.walk(bridge_method) if isinstance(node, ast.Assign)
                and any(ast.unparse(target) == 'self._inbox' for target in node.targets)]
    # Normal adoption and the failure-path retain_runtime_composition closure
    # both retain the same facade on this one application state host.
    assert len(retained) == 2 and all(ast.unparse(node.value) == 'graph.inbox' for node in retained)


def test_event_bridge_is_composed_with_the_inbox_facade_in_actor_mode() -> None:
    _assert_runtime_inbox_composition(_tree())


@pytest.mark.parametrize("replacement", ("raw_coordinator", "legacy_mode", 'graph_raw_coordinator', 'state_host_bypass'))
def test_runtime_inbox_composition_guard_rejects_owner_bypass(replacement: str) -> None:
    tree = _tree()
    bridge = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DaemonEventBridge"
    )
    for keyword in bridge.keywords:
        if replacement == "raw_coordinator" and keyword.arg == "inbox":
            keyword.value = ast.Name(id="inbox_coordinator", ctx=ast.Load())
        elif replacement == "legacy_mode" and keyword.arg == "actor_mode":
            keyword.value = ast.Constant(value=False)
    if replacement == 'graph_raw_coordinator':
        graph = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == '_StartupGraph')
        next(item for item in graph.keywords if item.arg == 'inbox').value = ast.Name(id='inbox_coordinator', ctx=ast.Load())
    elif replacement == 'state_host_bypass':
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(ast.unparse(target) == 'self._inbox' for target in node.targets):
                node.value = ast.Attribute(value=ast.Name(id='graph', ctx=ast.Load()), attr='inbox_coordinator', ctx=ast.Load())
    with pytest.raises(AssertionError):
        _assert_runtime_inbox_composition(tree)


def test_legacy_daemon_runtime_and_supervisor_names_are_gone() -> None:
    paths = tuple((SOURCE_ROOT / 'daemon/impl/runtime').rglob('*.py')) + tuple(
        (SOURCE_ROOT / 'daemon/impl/adapters/lark/runtime').rglob('*.py')) + (
        SOURCE_ROOT / 'daemon/impl/processes/supervisor.py',
        SOURCE_ROOT / 'daemon/impl/adapters/lark/worker/process.py',
    )
    assert paths and all(path.is_file() for path in paths)
    sources = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for forbidden in (
        "DaemonRuntime",
        "reconcile_once",
        "run_until",
        "AdapterSupervisor",
        "HarnessSupervisor",
    ):
        assert forbidden not in sources


def test_managed_lifecycle_paths_never_write_harness_state_directly() -> None:
    methods = (
        DaemonApplication._transfer_quiesce,
        DaemonApplication._transfer_receive,
        DaemonApplication._transfer_undo_receive,
        DaemonApplication._transfer_resume,
        DaemonApplication._transfer_complete,
        DaemonApplication._stop_agent_runtime,
    )
    source = "\n".join(inspect.getsource(method) for method in methods)
    for forbidden in (
        "_harnesses.start(",
        "_harnesses.remove(",
        "desired_state.upsert_harness(",
        "desired_state.remove_harness(",
        "_release_agent_binding(",
    ):
        assert forbidden not in source
    assert "LifecycleOperation." in source


def test_event_bridge_cannot_restore_harness_processes() -> None:
    source = inspect.getsource(DaemonEventBridge.start)
    assert ".restore(" not in source
    assert "harness_selector" not in inspect.signature(DaemonEventBridge).parameters


def test_application_has_no_global_ipc_lock_or_direct_service_tick() -> None:
    source = _application_source()
    assert "_ipc_request_lock" not in source
    assert "_service_linearized_ipc_method" not in source
    assert "._workflow_service.tick(" not in source
    assert "._routine_service.tick(" not in source
    assert "._runtime.poll(" not in source
