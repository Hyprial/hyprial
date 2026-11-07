"""Early sidecar progress and tailnet → Zenoh → OrgFS startup order."""
import ast
import inspect
import textwrap

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import DaemonStartupPhase


def test_delivery_plane_slow_operations_are_individually_named_startup_phases():
    tree = ast.parse('\n'.join(textwrap.dedent(inspect.getsource(method)) for method in (
        DaemonApplication._startup_open_transport, DaemonApplication._startup_compose_delivery_plane)))
    wrapped = {}
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == '_startup_step' and len(call.args) >= 2):
            continue
        phase = call.args[1]
        assert isinstance(phase, ast.Attribute) and isinstance(phase.value, ast.Name)
        assert phase.value.id == 'DaemonStartupPhase'
        operation = call.args[0]
        if isinstance(operation, ast.Attribute):
            wrapped[operation.attr] = phase.attr
        for nested in ast.walk(operation):
            if isinstance(nested, ast.Call):
                if isinstance(nested.func, ast.Attribute):
                    wrapped[nested.func.attr] = phase.attr
                elif isinstance(nested.func, ast.Name):
                    wrapped[nested.func.id] = phase.attr
    expected = {
        'bind_transport': 'ACTOR_RUNTIME_ORGFS_TRANSPORT',
        '_start_org_identity_bindings_safely': 'ACTOR_RUNTIME_IDENTITY_BINDINGS',
        'start_casdoor_membership_sync': 'ACTOR_RUNTIME_CASDOOR_SYNC',
        'start_before_transport': 'ACTOR_RUNTIME_FORWARDING_RECONFIGURE',
        '_open_transport': 'ACTOR_RUNTIME_TRANSPORT',
    }
    assert {name: wrapped.get(name) for name in expected} == expected
    assert {DaemonStartupPhase(value).name for value in (
        'actor-runtime-orgfs-transport', 'actor-runtime-identity-bindings',
        'actor-runtime-casdoor-sync', 'actor-runtime-forwarding-reconfigure',
        'actor-runtime-transport')} == set(expected.values())
    calls = {node.func.id: node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    bind = next(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'bind_transport')
    assert calls['start_before_transport'] < calls['_open_transport'] < bind
    runtime = ast.parse(textwrap.dedent(inspect.getsource(DaemonApplication._start_runtime)))
    stages = {node.func.attr: node.lineno for node in ast.walk(runtime)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert stages['_startup_open_transport'] < stages['_startup_compose_delivery_plane']
