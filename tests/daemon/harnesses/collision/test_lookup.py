"""All worker-spec readers must distinguish adapters and ambiguous workers."""
from dataclasses import replace
from types import SimpleNamespace

from hyprial.daemon.impl.desired_state import DesiredStateStore
from .fixtures import migrated_state as DesiredState
from hyprial.daemon.impl.harnesses.capabilities import worker_spec
from hyprial.daemon.impl.state_persistence.ports import _Projection
from hyprial.daemon.impl.transfer.admission import resolve_transfer_target
from hyprial.daemon.impl.application.actors.transfer_p2 import _TransferP2Mixin
from hyprial.kernel import HarnessLaunchSpec


def specs():
    return (HarnessLaunchSpec(harness="lark", name="sprite", headless=True),
            HarnessLaunchSpec(harness="pi", name="sprite", headless=True))


def test_worker_lookup_is_unambiguous_and_never_returns_adapter(tmp_path):
    rows = specs()
    store = DesiredStateStore(tmp_path / "desired.json")
    store.save(DesiredState(harnesses=rows))
    projection = _Projection(store.load())
    for reader in (store, projection):
        assert reader.harness_spec("sprite").harness == "pi"
        assert reader.harness_spec("sprite", excluding="pi") is None
    assert worker_spec(rows[:1], "sprite") is None
    assert worker_spec((*rows, replace(rows[1], harness="codex")), "sprite") is None
    store.upsert_harness(replace(rows[1], harness="codex"))
    assert store.harness_spec("sprite") is None
    projection.publish(store.load())
    assert projection.harness_spec("sprite") is None


def test_transfer_resolvers_ignore_adapter():
    desired = DesiredState(harnesses=specs())
    assert resolve_transfer_target(desired, name="sprite").harness == "pi"
    app = SimpleNamespace(desired_state=SimpleNamespace(load=lambda: desired),
                          agents=SimpleNamespace(native_actor=lambda a: a))
    assert _TransferP2Mixin._transfer_desired_spec_for(app, "sprite").harness == "pi"
