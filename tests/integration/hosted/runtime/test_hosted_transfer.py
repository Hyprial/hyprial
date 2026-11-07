"""Hosted receive through the production lifecycle/Agent/Harness composition.

Only external process and network I/O use existing protocol-shaped doubles;
no inline lifecycle executor, invented projections, Docker, or credentials.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from tests.identity.agents.registry.entity.support import StubPresence
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import _LocalDelivery, _Transport
from tests.integration.transfer.lifecycle import test_transfer_daemon as _transfer_daemon_tests
from tests.integration.transfer.lifecycle.test_transfer_daemon import _StubLauncher, _container_spec, _fake_container_ops
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import shutil as _cli_shutil
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def hosted_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The imported container spec carries the transfer tests' WORKSPACE cwd;
    # the launch contract rejects a cwd that does not exist on this host, so
    # point it at a real directory for the duration of each test.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(_transfer_daemon_tests, "WORKSPACE", str(work))
    app = DaemonApplication(
        state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
        owner="target-owner", node_id="target-node",
    **_deep_module_daemon_dependencies())
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._inbox = inbox
    app._presence = StubPresence()
    launcher = _StubLauncher()
    events = CorrelatedDomainEvents()
    harnesses = HarnessPortClient(
        HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state),
        events,
    )
    app._harnesses = harnesses
    app._start_lifecycle_manager(
        transport=_Transport(), inbox=inbox,
        local_delivery=_LocalDelivery(), harnesses=harnesses,
    )
    calls = _fake_container_ops(monkeypatch)
    try:
        yield app, launcher, calls
    finally:
        assert app._lifecycle_manager.drain(3.0)
        for port in app._lifecycle_domain_ports:
            assert port.drain(3.0)
        app._route_registration.close_registrations()
        assert app._route_registration.drain(3.0)
        app._lifecycle_router.close()
        harnesses.stop(3.0)
        app.agents.close()
        inbox.close()


def receive(app: DaemonApplication, *, pins: list[str] | None = None):
    # Real HarnessLaunchSpec.to_json from the pre-existing container tests.
    return app.handle("transfer.receive", {
        "spec": _container_spec(), "pins": pins or [],
        "strictTimeoutSeconds": 1, "credentials": False,
    })


def test_receive_inserts_hosted_row_before_lifecycle_bind(hosted_app, monkeypatch):
    app, launcher, calls = hosted_app
    original = app._run_lifecycle_operation
    observed = []

    def inspect_then_run(operation, **kwargs):
        record = app.agents.require(operation.target.actor)
        observed.append(record)
        assert record.uri == "agent:amyws:target-node:wk"
        assert record.owner == "amyws"
        assert record.machine == "target-node"
        assert record.hosted_by == "transfer-receive"
        return original(operation, **kwargs)

    monkeypatch.setattr(app, "_run_lifecycle_operation", inspect_then_run)
    result = receive(app)
    assert result["ok"] is True
    assert len(observed) == 1
    record = app.agents.require(result["actor"])
    assert app.agents.uri_for("wk") == result["actor"]
    assert app.agents.local_actor(result["actor"]) == "wk"
    assert app.agents.local_actor("agent:unrelated:target-node:wk") is None
    assert record.hosted_by == "transfer-receive"
    # An ordinary update through the typed Agent command must not erase hosting.
    app.agents.record_session("wk", harness="claude", session_id="session-1")
    assert app.agents.require(result["actor"]).hosted_by == "transfer-receive"
    assert launcher.started == [("claude", "wk")]
    assert calls["prepare"][0]["name"] == "wk"
    assert calls["prune"] == []


@pytest.mark.parametrize("method", ["agent.create", "lifecycle.start"])
def test_public_daemon_creation_rejects_foreign_uri(hosted_app, method):
    app, launcher, _ = hosted_app
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(method, {
            "name": "agent:amyws:target-node:wk", "provider": "claude",
            "headless": True,
        })
    assert failure.value.code == "INVALID_AGENT_NAME"
    assert app.agents.list() == ()
    assert app.desired_state.load().harnesses == ()
    assert launcher.started == []


def test_start_pinned_owner_is_not_an_authority_bypass(hosted_app):
    app, launcher, _ = hosted_app
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("lifecycle.start", _container_spec())
    assert failure.value.code == "INVALID_ARGUMENT"
    assert app.agents.list() == ()
    assert launcher.started == []


@pytest.mark.parametrize("command", ["create", "start"])
def test_cli_create_and_start_reject_foreign_uri(hosted_app, monkeypatch, command):
    app, launcher, _ = hosted_app

    def request(method, params=None, **kwargs):
        try:
            return app.handle(method, params or {})
        except DaemonRequestError as error:
            raise _cli_get_services().CliError(error.code, str(error)) from error

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    # Only binary discovery is replaced; no provider process may be launched.
    monkeypatch.setattr(_cli_shutil, "which", lambda _name: "/bin/false")
    args = (["agent", "create"] if command == "create"
            else ["agent", "start", "claude", "--headless"])
    result = CliRunner().invoke(_cli_root_app, [
        *args, "--name", "agent:amyws:target-node:wk", "--json",
    ])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "INVALID_AGENT_NAME"
    assert app.agents.list() == ()
    assert launcher.started == []


@pytest.mark.parametrize("failure_mode", ["wrong-ref", "not-ready", "launcher-error"])
def test_failed_receive_removes_hosted_row_and_pins(hosted_app, monkeypatch, failure_mode):
    app, launcher, calls = hosted_app
    if failure_mode == "wrong-ref":
        launcher.ref_override = "cold-start-ref"
    elif failure_mode == "not-ready":
        launcher.not_ready = True
    else:
        def fail_start(spec):
            raise RuntimeError("controlled launch failure")
        monkeypatch.setattr(launcher, "start", fail_start)
    with pytest.raises(DaemonRequestError) as failure:
        receive(app, pins=["test-adapter"])
    if failure_mode != "launcher-error":
        assert failure.value.code == "STRICT_RESUME_FAILED"
        assert launcher.processes[("claude", "wk")].stopped
    else:
        assert "controlled launch failure" in str(failure.value)
    assert app.agents.get("wk") is None
    assert app.agents.local_actor("agent:amyws:target-node:wk") is None
    assert app.agents.pins() == {}
    assert app.desired_state.load().harnesses == ()
    assert calls["prune"][0]["name"] == "wk"


def test_failed_receive_preserves_incumbent_pin(hosted_app):
    app, _, _ = hosted_app
    original = app.agents.create("incumbent")
    app.agents.pin("held-adapter", "incumbent")
    with pytest.raises(DaemonRequestError) as failure:
        receive(app, pins=["held-adapter"])
    assert failure.value.code == "TRANSFER_PIN_CONFLICT"
    assert app.agents.get("wk") is None
    assert app.agents.pins() == {"held-adapter": original.uri}
    assert app.desired_state.load().harnesses == ()


@pytest.mark.parametrize("target", ["wk", "agent:amyws:target-node:wk"])
def test_destroy_removes_hosted_entity_and_cascades_pins(hosted_app, target):
    app, launcher, _ = hosted_app
    receive(app, pins=["test-adapter"])
    result = app.handle("agent.destroy", {"name": target})
    assert result["destroyed"] is True
    assert result["actor"] == "agent:amyws:target-node:wk"
    assert result["unpinnedAdapters"] == ["test-adapter"]
    assert app.agents.get("wk") is None
    assert app.agents.local_actor(result["actor"]) is None
    assert app.agents.pins() == {}
    assert app.desired_state.load().harnesses == ()
    assert launcher.processes[("claude", "wk")].stopped


def test_destroy_unrelated_uri_does_not_delete_hosted_actor(hosted_app):
    app, launcher, _ = hosted_app
    receive(app, pins=["test-adapter"])
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.destroy", {"name": "agent:unrelated:target-node:wk"})
    assert failure.value.code == "AGENT_NOT_FOUND"
    assert app.agents.require("wk").hosted_by == "transfer-receive"
    assert app.agents.pins() == {"test-adapter": "agent:amyws:target-node:wk"}
    assert launcher.processes[("claude", "wk")].running


def test_conflicting_receive_does_not_remove_incumbent_hosted_row(hosted_app):
    app, launcher, calls = hosted_app
    receive(app, pins=["test-adapter"])
    original = app.agents.require("wk")
    with pytest.raises(DaemonRequestError) as failure:
        receive(app)
    assert failure.value.code == "TRANSFER_CONFLICT"
    assert app.agents.require("wk") == original
    assert app.agents.pins() == {"test-adapter": original.uri}
    assert launcher.started == [("claude", "wk")]
    assert calls["prune"] == []


@pytest.mark.parametrize("all_workers", [False, True])
def test_down_only_deactivates_hosted_entity(hosted_app, all_workers):
    app, launcher, _ = hosted_app
    receive(app, pins=["test-adapter"])
    result = app.handle("down", {"all": True} if all_workers else {"target": "wk"})
    assert result["ok"] is True
    agent = app.agents.require("agent:amyws:target-node:wk")
    assert agent.hosted_by == "transfer-receive"
    assert app.agents.uri_for("wk") == agent.uri
    assert app.agents.pins() == {"test-adapter": agent.uri}
    assert app.desired_state.load().harnesses == ()
    assert launcher.processes[("claude", "wk")].stopped
    assert app._agent_session_domains.agent.read_binding(agent.uri) is None


# R7 retired the top-level `ps` view; its agent rows are the `agent list` view below,
# which is the same reader (`_agent_status_json`) that carried `hosted`/`hostedBy` before.
@pytest.mark.parametrize("command", [("message", "targets"), ("agent", "list")])
@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_hosted_visibility_in_every_cli_view(hosted_app, monkeypatch, command, json_output):
    app, _, _ = hosted_app
    receive(app)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params=None, **kw: app.handle(method, params or {}))
    result = CliRunner().invoke(_cli_root_app, [*command, *(["--json"] if json_output else [])])
    assert result.exit_code == 0, result.output
    if json_output:
        payload = json.loads(result.stdout)
        rows = payload["targets" if command == ("message", "targets") else "agents"]
        key = "targetUri" if command == ("message", "targets") else "uri"
        matching = [row for row in rows if row[key] == "agent:amyws:target-node:wk"]
        assert len(matching) == 1
        assert matching[0]["hosted"] is True
        assert matching[0]["hostedBy"] == "transfer-receive"
        assert not any(row[key] == "agent:target-owner:target-node:wk" for row in rows)
    else:
        assert "hosted" in result.stdout
        assert "true" in result.stdout.lower()
        assert "hostedBy" in result.stdout
        assert "transfer-receive" in result.stdout


def test_direct_agent_create_command_also_rejects_foreign_uri(hosted_app):
    from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted
    from hyprial.daemon.impl.composition import DomainCommandError

    app, _, _ = hosted_app
    with pytest.raises(DomainCommandError) as failure:
        app._agent_session_domains.call_agent(CreateAgentCommand(
            "foreign-create", "agent:amyws:target-node:wk", reuse_existing=True,
        ), AgentMutationCompleted)
    assert failure.value.code == "INVALID_AGENT_NAME"
    assert app.agents.list() == ()
