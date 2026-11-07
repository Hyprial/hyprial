"""Name admission uses local durable records, including stopped adapters."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.actors.agents_admin.adapters import _AdapterAdminMixin
from hyprial.daemon.impl.application.actors.agents_admin.registry_ops import _AgentRegistryOpsMixin
from hyprial.daemon.impl.application.actors.lifecycle_ops import _LifecycleOpsMixin
from hyprial.daemon.impl.bootstrap.adapter_registration import add_lark_gateway, ensure_lark_gateway_addable
from hyprial.daemon.impl.desired_state import DesiredState
from hyprial.identity import AgentRegistry
from hyprial.kernel import DaemonRequestError, PersistentConfigError


def gateway(home: Path, name: str = "sprite", *, force: bool = False):
    return add_lark_gateway(hyprial_home=home, name=name, app_id="test-app",
                            app_secret="test-secret", routes=(), allow_no_routes=True,
                            force=force)


@pytest.mark.parametrize("force", [False, True])
def test_adapter_create_and_onboard_refuse_local_agent(tmp_path, force):
    registry = AgentRegistry(tmp_path / "state" / "agents.sqlite3", owner="op", machine="node")
    registry.create("sprite")
    registry.close()
    with pytest.raises(PersistentConfigError, match="sprite-feishu"):
        ensure_lark_gateway_addable(hyprial_home=tmp_path, name="sprite", force=force)
    with pytest.raises(PersistentConfigError, match="sprite-feishu"):
        gateway(tmp_path, force=force)
    assert not (tmp_path / "channels.json").exists()
    assert not (tmp_path / "secrets").exists()
    gateway(tmp_path, "sprite-feishu")


def test_adapter_admission_refuses_unreadable_registry(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "agents.sqlite3").write_text("not a database")
    with pytest.raises(PersistentConfigError, match="cannot check local agent names"):
        gateway(tmp_path)
    assert not (tmp_path / "channels.json").exists()


def test_adapter_admission_uses_explicit_state_root(tmp_path):
    state = tmp_path / "external-state"
    registry = AgentRegistry(state / "agents.sqlite3", owner="op", machine="node")
    registry.create("sprite")
    registry.close()
    with pytest.raises(PersistentConfigError, match="sprite-feishu"):
        add_lark_gateway(hyprial_home=tmp_path / "home", state_dir=state,
                         name="sprite", app_id="test", app_secret="secret",
                         routes=(), allow_no_routes=True)
    assert not (tmp_path / "home" / "state").exists()


def test_doctor_warns_without_changing_legacy_collision(tmp_path, monkeypatch):
    from hyprial.shell.impl.cli.commands.doctor.checks import names

    gateway(tmp_path)
    registry = AgentRegistry(tmp_path / "state" / "agents.sqlite3", owner="op", machine="node")
    registry.create("sprite")  # legacy collision: bypass explicit daemon admission
    registry.close()
    channels = (tmp_path / "channels.json").read_bytes()
    database = (tmp_path / "state" / "agents.sqlite3").read_bytes()
    from hyprial.kernel import ipc_errors

    def unavailable(*_args, **_kwargs):
        raise ipc_errors.DaemonUnavailableError("daemon is not running")

    monkeypatch.setattr(names, "get_services", lambda: SimpleNamespace(
        _hyprial_home=lambda: tmp_path, _state_dir=lambda: tmp_path / "state",
        runtime=SimpleNamespace(request=unavailable)))
    check = names.name_collision_check()
    assert check["status"] == "warn"
    assert "sprite" in check["detail"] and "-feishu" in check["detail"]
    assert (tmp_path / "channels.json").read_bytes() == channels
    assert (tmp_path / "state" / "agents.sqlite3").read_bytes() == database


def test_adapter_start_refuses_agent_before_start(tmp_path):
    app = SimpleNamespace(
        hyprial_home=tmp_path, agents=SimpleNamespace(get=lambda _: object()),
        desired_state=SimpleNamespace(load=DesiredState), _lark_client=object(),
    )
    with pytest.raises(DaemonRequestError, match="sprite-feishu") as error:
        _AdapterAdminMixin._ipc_adapter_start(app, {"name": "sprite"})
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("method", ["agent.create", "agent.start.preflight", "agent.host-invite", "lifecycle.start"])
def test_agent_create_start_refuse_configured_stopped_adapter(tmp_path, method):
    gateway(tmp_path)
    app = SimpleNamespace(
        hyprial_home=tmp_path,
        agents=SimpleNamespace(native_actor=lambda n: n),
        desired_state=SimpleNamespace(load=DesiredState),
    )
    params = {"name": "sprite", "provider": "pi", "owner": "visitor", "headless": True}
    handler = (_LifecycleOpsMixin._ipc_lifecycle_start if method == "lifecycle.start"
               else _AgentRegistryOpsMixin._handle_agent)
    with pytest.raises(DaemonRequestError, match="sprite-feishu") as error:
        handler(app, method, params)
    assert error.value.code == "INVALID_ARGUMENT"
