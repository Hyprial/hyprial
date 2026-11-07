"""Legacy adapter rows must not acquire agent runtime or inbox custody."""
from types import SimpleNamespace

import pytest

from .fixtures import migrated_state as DesiredState
from hyprial.daemon.impl.harnesses.capabilities import DECLARED_HARNESSES
from hyprial.daemon.impl.runtime.results import _BridgeResultsMixin
from hyprial.identity.impl.agents.migration.actor import config_rebind_quiescence
from hyprial.kernel import HarnessLaunchSpec


def test_shared_worker_vocabulary_matches_capabilities():
    from hyprial.daemon.impl.harnesses.capabilities import _DECLARATIONS
    from hyprial.kernel import DECLARED_HARNESSES as shared

    assert shared is DECLARED_HARNESSES
    assert shared == {name for name, _ in _DECLARATIONS}


@pytest.mark.parametrize("kind", ["lark", *sorted(DECLARED_HARNESSES)])
def test_adapter_row_is_not_agent_runtime_custody(kind):
    agent = SimpleNamespace(actor="sprite", uri="agent:op:node:sprite")
    live = SimpleNamespace(verdict=lambda *_: False, binding=lambda *_: None)
    launches = SimpleNamespace(active_for=lambda *_: False,
                               destroy_reserved=lambda *_: False)

    def facts(specs):
        return config_rebind_quiescence(
            agent,
            desired_state=SimpleNamespace(load=lambda: DesiredState(harnesses=specs)),
            liveness=live, runtime_launches=launches,
        )

    assert facts(())["quiescent"] is True
    proof = facts((HarnessLaunchSpec(harness=kind, name="sprite", headless=True),))
    assert proof["quiescent"] is (kind == "lark"), proof
    assert proof["desired"] is (kind != "lark")


@pytest.mark.parametrize("kind", ["lark", *sorted(DECLARED_HARNESSES)])
def test_adapter_row_does_not_enter_agent_stranded_sweep(kind):
    queried, acked = [], []
    message = SimpleNamespace(message_id="m", intent="event")

    def pending(actor):
        queried.append(actor)
        return (message,)

    def ack(actor, message_id):
        acked.append((actor, message_id))
        return SimpleNamespace(acknowledged=True)

    bridge = SimpleNamespace(
        harnesses=SimpleNamespace(streaming_actors=lambda: ()),
        harness_actor_uri=lambda name: f"agent:op:node:{name}",
        inbox=SimpleNamespace(has_fetched=lambda _: True,
                              harness_failure_settlement=lambda _: None,
                              pending_messages=pending, ack=ack),
        _current_link_gate=lambda: None, _logger=None,
    )
    count = _BridgeResultsMixin._settle_stranded_rows(
        bridge, (HarnessLaunchSpec(harness=kind, name="sprite", headless=True),)
    )
    expected = (0, [], []) if kind == "lark" else (
        1, ["agent:op:node:sprite"], [("agent:op:node:sprite", "m")]
    )
    assert (count, queried, acked) == expected


@pytest.mark.parametrize("ambiguous", [False, True])
def test_status_snapshot_never_uses_adapter_owner(ambiguous):
    from hyprial.daemon.impl.application.messaging.status.snapshots import (
        _WorkerSnapshotMixin,
    )

    specs = [
        HarnessLaunchSpec("lark", "sprite", True, pinned_owner="adapter-owner"),
        HarnessLaunchSpec("pi", "sprite", True, pinned_owner="worker-owner"),
    ]
    if ambiguous:
        specs.append(HarnessLaunchSpec("codex", "sprite", True, pinned_owner="other"))
    app = SimpleNamespace(
        _harnesses=SimpleNamespace(status=lambda: (
            {"name": "sprite", "runtime": "pi", "running": True},
        )),
        desired_state=SimpleNamespace(load=lambda: DesiredState(harnesses=tuple(specs))),
        _canonical_harness_uri=lambda name, spec: (
            f"agent:{spec.pinned_owner if spec else 'registry'}:node:{name}"
        ),
    )
    snapshot = _WorkerSnapshotMixin._build_worker_status_snapshot(app)
    owner = "registry" if ambiguous else "worker-owner"
    assert snapshot.running_by_actor == {f"agent:{owner}:node:sprite": True, "sprite": True}


def test_corrupt_routine_marker_cannot_retire_adapter():
    from hyprial.daemon.impl.application.actors.routines import _RoutinesMixin

    calls = []
    app = SimpleNamespace(
        owner="op", node_id="node",
        _routine_coordinator_marker=lambda *_: "routine-marker",
        desired_state=SimpleNamespace(load=lambda: DesiredState(harnesses=(
            HarnessLaunchSpec("lark", "sprite", True, nickname="routine-marker"),
        ))),
        handle=lambda *args: calls.append(args) or {"removed": True},
    )
    result = _RoutinesMixin._retire_routine_coordinator(
        app, {"name": "routine", "schemaError": "unreadable"}
    )
    assert result is None
    assert calls == []


@pytest.mark.parametrize("persisted", [False, True])
def test_down_without_adapter_client_does_not_deactivate_or_release_agent(persisted):
    from hyprial.daemon.impl.application.actors.lifecycle_ops import _LifecycleOpsMixin

    calls = []
    app = SimpleNamespace(
        _lark_client=None,
        desired_state=SimpleNamespace(load=lambda: DesiredState(harnesses=(
            HarnessLaunchSpec("lark", "sprite", True),
        ) if persisted else ())),
        _lifecycle_spec=lambda spec: calls.append(("spec", spec)) or spec,
        _run_lifecycle_operation=lambda operation: calls.append(("deactivate", operation)),
        _agent_name_for=lambda name: calls.append(("release-lookup", name)) or None,
    )
    result = _LifecycleOpsMixin._ipc_down(app, {"target": "sprite", "provider": "lark"})
    assert calls == []
    assert result == {"ok": True, "removed": []}


def test_pac_adapter_launch_cannot_create_agent_lifecycle():
    from hyprial.daemon.impl.application.actors.lifecycle_ops import _LifecycleOpsMixin
    from hyprial.daemon.impl.pac.actors.coordinator.types import LaunchSpec
    from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
    from hyprial.kernel import DaemonRequestError

    conversions = []
    app = SimpleNamespace(
        _canonical_harness_uri=lambda name, spec: (
            conversions.append(name) or f"agent:op:node:{name}"
        ),
        _run_lifecycle_operation=lambda _: None,
    )
    app._lifecycle_spec = lambda spec: _LifecycleOpsMixin._lifecycle_spec(app, spec)
    runtime = DaemonActorRuntime(app)
    runtime.observe = lambda _: None
    with pytest.raises(DaemonRequestError, match="declared worker harness") as error:
        runtime.start("sprite", LaunchSpec("lark", None, None, None, ()),
                      operation_id="test", identity_marker="pac:test")
    assert error.value.code == "INVALID_ARGUMENT"
    assert conversions == []
