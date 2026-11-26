"""Real Cell CAS and binding persistence at each interrupted coordination boundary."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.cells.model import CellBinding, CellMember, thaw_json
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.desired_state.bindings.models import Configuration
from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.kernel import DaemonRequestError
from tests.daemon.cells.runtime.cell_runtime_support import org_fs, runtime


@pytest.fixture
def setup(tmp_path):
    from hyprial.daemon.impl.application.model_binding.configure.coordinator import Coordinator

    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(CellSpec(
        "agent", CellBinding.subject("a.fixture"),
        members=(CellMember("member", "harness"),),
        params={"cell.members": {"member": {"kind": "claude", "allow": ["Read"]}}},
    ))
    agent = SimpleNamespace(agent_id="a.fixture", cell_id=cell.cell_id, actor="worker")
    store = DesiredStateStore(tmp_path / "desired.json")
    app = SimpleNamespace(
        desired_state=store, _cell_runtime=cells,
        agents=SimpleNamespace(get=lambda _name: agent),
    )
    config = Configuration("connection", "hy4-flash", "pi", "headless", "a" * 64, "ref", 1)
    authorized = []

    def build():
        return Coordinator(app, agent, "member", authorize=lambda c: authorized.append(c))

    yield app, agent, config, build, authorized
    cells.close()


def test_finalize_failure_keeps_blocker_and_restart_recovers_once(setup, monkeypatch):
    app, agent, config, build, authorized = setup
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    original = ModelBindingStore.finalize
    monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError, match="state-unreadable"):
        build().configure("op", 0, 1, config)
    assert app._cell_runtime.lookup(agent.cell_id).revision == 2
    assert app.desired_state.model_binding_store.status(agent.agent_id, "member").pending is not None
    app.desired_state = DesiredStateStore(app.desired_state.legacy_path)
    monkeypatch.setattr(ModelBindingStore, "finalize", original)
    result = build().configure("op", 0, 1, config)
    assert result.phase == "finalized" and result.result_revision == 1
    authorized.clear()
    assert build().configure("op", 0, 1, config) == result
    assert authorized == []
    assert app._cell_runtime.lookup(agent.cell_id).revision == 2


def test_cancel_first_configuration_preserves_unrelated_updates(setup, monkeypatch):
    app, agent, config, build, _ = setup
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError):
        build().configure("op", 0, 1, config)
    current = app._cell_runtime.lookup(agent.cell_id)
    params = thaw_json(current.params)
    params["unrelated"] = "preserve"
    params["cell.members"]["member"]["allow"] = ["Read", "Glob"]
    app._cell_runtime.update(agent.cell_id, params=params, expect_revision=current.revision)
    result = build().cancel("op", 0)
    assert result.phase == "cancelled"
    cell = app._cell_runtime.lookup(agent.cell_id)
    assert thaw_json(cell.params) == {
        "cell.members": {"member": {"kind": "claude", "allow": ["Read", "Glob"]}},
        "unrelated": "preserve",
    }
    assert app.desired_state.model_binding_store.status(agent.agent_id, "member").binding is None
    assert build().configure("op", 0, 1, config) == result
    assert build().cancel("op", 0) == result


def test_unknown_cell_outcome_remains_cancelling_and_retry_cannot_finalize(setup, monkeypatch):
    app, agent, config, build, _ = setup
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError):
        build().configure("op", 0, 1, config)
    original = app._cell_runtime.lookup
    monkeypatch.setattr(app._cell_runtime, "lookup", lambda _: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError, match="cell-outcome-unknown"):
        build().cancel("op", 0)
    operation = app.desired_state.model_binding_store.status(agent.agent_id, "member").pending
    assert operation.coordination.phase == "cancelling"
    with pytest.raises(DaemonRequestError, match="operation-in-progress"):
        build().configure("op", 0, 1, config)
    monkeypatch.setattr(app._cell_runtime, "lookup", original)
    assert build().cancel("op", 0).phase == "cancelled"


def test_cell_conflict_never_overwrites_changed_member(setup, monkeypatch):
    app, agent, config, build, _ = setup
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError):
        build().configure("op", 0, 1, config)
    app._cell_runtime.update(agent.cell_id, params={
        "cell.members": {"member": {"kind": "pi", "model": "another-choice"}},
    }, expect_revision=2)
    with pytest.raises(DaemonRequestError, match="cell-intent-mismatch"):
        build().cancel("op", 0)
    assert app._cell_runtime.lookup(agent.cell_id).revision == 3
    assert app.desired_state.model_binding_store.status(agent.agent_id, "member").pending


def test_stale_revision_and_other_pending_refuse_before_secret_or_cell(setup):
    app, agent, config, build, authorized = setup
    with pytest.raises(DaemonRequestError, match="revision-mismatch"):
        build().configure("op", 1, 1, config)
    assert authorized == []
    assert app._cell_runtime.lookup(agent.cell_id).revision == 1
    assert not app.desired_state.model_binding_store.status(agent.agent_id, "member").operations
    result = build().configure("op", 0, 1, config)
    with pytest.raises(DaemonRequestError, match="operation-digest-mismatch"):
        build().configure("op", 0, 2, config)
    with pytest.raises(DaemonRequestError, match="operation-digest-mismatch"):
        build().configure("op", 0, 1, replace(config, model="different"))
    with pytest.raises(DaemonRequestError, match="operation-already-completed"):
        build().cancel("op", 0)
    assert result.result_revision == 1


def test_recreated_name_between_read_and_write_cannot_mutate_new_cell(setup, monkeypatch):
    app, agent, config, build, _ = setup
    from hyprial.daemon.impl.application.actors.cell_admin import service

    other = app._cell_runtime.ensure(CellSpec(
        "agent", CellBinding.subject("a.recreated"),
        members=(CellMember("member", "harness"),),
        params={"cell.members": {"member": {"kind": "claude"}}},
    ))
    recreated = SimpleNamespace(agent_id="a.recreated", actor="worker", cell_id=other.cell_id)
    monkeypatch.setattr(service, "_resolve_agent", lambda *_: recreated)
    with pytest.raises(DaemonRequestError) as error:
        build().configure("op", 0, 1, config)
    assert error.value.code == "MODEL_CONFIGURATION_CONFLICT"
    assert error.value.data == {"phase": "bind", "reason": "agent-incarnation-changed"}
    assert app._cell_runtime.lookup(other.cell_id) == other
    assert app.desired_state.model_binding_store.status(agent.agent_id, "member").pending


def test_compensation_write_before_crash_can_finish_without_second_cell_write(setup, monkeypatch):
    app, agent, config, build, _ = setup
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError):
        build().configure("op", 0, 1, config)
    checkpoint = ModelBindingStore.checkpoint

    def crash(self, *args, **kwargs):
        if args[-1]["phase"] == "compensated":
            raise OSError("crash before recording compensated")
        return checkpoint(self, *args, **kwargs)

    monkeypatch.setattr(ModelBindingStore, "checkpoint", crash)
    with pytest.raises(DaemonRequestError, match="state-unreadable"):
        build().cancel("op", 0)
    assert app._cell_runtime.lookup(agent.cell_id).revision == 3
    app.desired_state = DesiredStateStore(app.desired_state.legacy_path)
    monkeypatch.setattr(ModelBindingStore, "checkpoint", checkpoint)
    assert build().cancel("op", 0).phase == "cancelled"
    assert app._cell_runtime.lookup(agent.cell_id).revision == 3


def test_same_cell_configure_and_cancel_serialize_without_global_storage_lock(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    app, agent, config, build, _ = setup
    entered, release, cancel_entered = Event(), Event(), Event()
    update = app._cell_runtime.update

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return update(*args, **kwargs)

    monkeypatch.setattr(app._cell_runtime, "update", blocked)

    def cancel():
        cancel_entered.set()
        return build().cancel("op", 0)

    with ThreadPoolExecutor(max_workers=3) as executor:
        configuring = executor.submit(build().configure, "op", 0, 1, config)
        try:
            assert entered.wait(2)
            cancelling = executor.submit(cancel)
            assert cancel_entered.wait(2)
            independent = executor.submit(app.desired_state.save,
                                         replace(app.desired_state.load(), as_mailbox=True))
            independent.result(timeout=2)
        finally:
            release.set()
        assert configuring.result(timeout=2).phase == "finalized"
        with pytest.raises(DaemonRequestError, match="operation-already-completed"):
            cancelling.result(timeout=2)
    assert app.desired_state.model_binding_store.status(agent.agent_id, "member").pending is None


def test_invalid_operation_limit_refuses_new_but_allows_existing_recovery(setup, monkeypatch):
    app, agent, config, build, _ = setup
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    with pytest.raises(DaemonRequestError, match="operation-limit-setting"):
        build().configure("new", 0, 1, config, operation_limit=None)
    assert app._cell_runtime.lookup(agent.cell_id).revision == 1
    finalize = ModelBindingStore.finalize
    monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DaemonRequestError):
        build().configure("op", 0, 1, config)
    monkeypatch.setattr(ModelBindingStore, "finalize", finalize)
    result = build().configure("op", 0, 1, config, operation_limit=None)
    assert result.phase == "finalized"
    assert build().configure("op", 0, 1, config, operation_limit=None) == result
