"""Doctor name-collision diagnostics: online request first, B06 offline only."""

from __future__ import annotations

import json

import pytest

from hyprial.kernel import daemon_request, ipc_errors
from hyprial.shell.impl.cli.commands.common.support import CliError
from hyprial.shell.impl.cli.commands.doctor.checks import names
from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
)


class _Runtime:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def request(self, method, params=None, **kwargs):
        del params, kwargs
        self.calls.append(method)
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _services(runtime, probe=None):
    def _daemon_probe(timeout=None):
        del timeout
        if isinstance(probe, Exception):
            raise probe
        if probe is None:
            raise AssertionError("an unexpected phase probe")
        return probe

    return type(
        "Services",
        (),
        {"runtime": runtime, "CliError": CliError, "_daemon_probe": staticmethod(_daemon_probe)},
    )()


def test_agent_name_uses_the_canonical_uri_parser() -> None:
    assert names._agent_name({"actor": "agent:o:n:ops"}) == "ops"
    assert names._agent_name({"actor": "agent:o:n:ops:extra"}) is None
    assert names._agent_name({"actor": "o:n:ops"}) is None


def test_online_name_collision_uses_agent_and_adapter_requests(monkeypatch, tmp_path):
    runtime = _Runtime(
        {
            "agent.list": {"agents": [{"name": "ops", "actor": "agent:o:n:ops"}]},
            "adapter.list": {"adapters": [{"name": "ops"}]},
        }
    )
    services = _services(runtime)
    monkeypatch.setattr(names, "get_services", lambda: services)
    monkeypatch.setattr(
        "hyprial.identity.AgentRegistry.local_names",
        lambda _path: (_ for _ in ()).throw(AssertionError("online read must not touch sqlite")),
    )

    check = names.name_collision_check()

    assert check is not None and check["status"] == "warn"
    assert "ops" in str(check["detail"])
    assert runtime.calls == ["agent.list", "adapter.list"]


def test_offline_name_collision_is_b06_only_after_daemon_unavailable(monkeypatch, tmp_path):
    (tmp_path / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "ops",
                        "appId": "test",
                        "credentialRef": "lark-ops",
                        "routes": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = _Runtime(
        {
            "agent.list": ipc_errors.DaemonUnavailableError("daemon is not running"),
            "adapter.list": {"adapters": []},
        }
    )
    services = _services(runtime)
    services._hyprial_home = lambda: tmp_path
    services._state_dir = lambda: tmp_path / "state"
    monkeypatch.setattr(names, "get_services", lambda: services)
    monkeypatch.setattr("hyprial.identity.AgentRegistry.local_names", lambda _path: {"ops"})

    check = names.name_collision_check()

    assert check is not None and check["status"] == "warn"
    assert "ops" in str(check["detail"])
    assert runtime.calls == ["agent.list"]


def test_offline_fallback_is_triggered_by_the_real_kernel_error(monkeypatch, tmp_path):
    missing_socket = tmp_path / "missing-daemon.sock"

    class RealTransport:
        def request(self, method, params=None, **kwargs):
            del params, kwargs
            return daemon_request(method, {}, socket_path=missing_socket, timeout=0.2)

    services = _services(RealTransport())
    services._hyprial_home = lambda: tmp_path
    services._state_dir = lambda: tmp_path / "state"
    monkeypatch.setattr(names, "get_services", lambda: services)
    monkeypatch.setattr("hyprial.identity.AgentRegistry.local_names", lambda _path: set())

    assert names.name_collision_check() is None


@pytest.mark.parametrize("code", ["INVALID_RESPONSE", "DAEMON_ERROR", "INTERNAL_ERROR", "SOMETHING_ELSE"])
def test_other_request_error_is_not_silently_treated_as_offline(monkeypatch, code):
    runtime = _Runtime(
        {
            "agent.list": CliError(code, "unexpected"),
            "adapter.list": {"adapters": []},
        }
    )
    monkeypatch.setattr(names, "get_services", lambda: _services(runtime))
    monkeypatch.setattr(
        names,
        "_offline_names",
        lambda _services: (_ for _ in ()).throw(AssertionError("an error must not trigger the offline read")),
    )

    with pytest.raises(CliError) as caught:
        names.name_collision_check()
    assert caught.value.code == code


def test_a_serving_daemons_malformed_listing_still_fails_loudly(monkeypatch):
    runtime = _Runtime({"agent.list": {"agents": "not-a-list"}, "adapter.list": {"adapters": []}})
    monkeypatch.setattr(names, "get_services", lambda: _services(runtime))

    with pytest.raises(CliError) as caught:
        names.name_collision_check()
    assert caught.value.code == "INVALID_RESPONSE"


def test_a_restoring_daemon_is_not_turned_into_a_warning(monkeypatch):
    runtime = _Runtime(
        {"agent.list": ipc_errors.DaemonRestoringError("restoring"), "adapter.list": {"adapters": []}}
    )
    monkeypatch.setattr(names, "get_services", lambda: _services(runtime))

    with pytest.raises(ipc_errors.DaemonRestoringError):
        names.name_collision_check()


@pytest.mark.parametrize("code", ["CALLER_NOT_AUTHORIZED", "ACCOUNT_REQUIRED", "METHOD_NOT_FOUND"])
def test_a_bootstrap_refusal_is_not_evaluable_and_never_reads_offline(monkeypatch, code):
    runtime = _Runtime(
        {
            "agent.list": CliError(code, "refused"),
            "adapter.list": {"adapters": []},
        }
    )
    monkeypatch.setattr(
        names,
        "get_services",
        lambda: _services(runtime, {"running": True, "phase": "bootstrap", "pid": 7}),
    )
    monkeypatch.setattr(
        names,
        "_offline_names",
        lambda _services: (_ for _ in ()).throw(AssertionError("a refusal must not trigger the offline read")),
    )

    check = names.name_collision_check()

    assert check is not None
    assert check["name"] == "agent-adapter-name-collision"
    assert check["status"] == "warn"
    assert check["detail"].startswith("NOT-EVALUABLE")
    assert code in check["detail"]
    assert runtime.calls == ["agent.list"]


@pytest.mark.parametrize("code", ["CALLER_NOT_AUTHORIZED", "ACCOUNT_REQUIRED", "METHOD_NOT_FOUND"])
@pytest.mark.parametrize("phase", ["serving", None])
def test_a_serving_daemons_refusal_still_aborts_doctor(monkeypatch, code, phase):
    probe = {"running": True, "pid": 7, **({"phase": phase} if phase else {})}
    runtime = _Runtime({"agent.list": CliError(code, "refused"), "adapter.list": {"adapters": []}})
    monkeypatch.setattr(names, "get_services", lambda: _services(runtime, probe))
    monkeypatch.setattr(
        names,
        "_offline_names",
        lambda _services: (_ for _ in ()).throw(AssertionError("a refusal must not trigger the offline read")),
    )

    with pytest.raises(CliError) as caught:
        names.name_collision_check()
    assert caught.value.code == code


@pytest.mark.parametrize("failure", [CliError("DAEMON_ERROR", "probe"), ipc_errors.DaemonUnavailableError("down")])
def test_a_failed_phase_probe_reraises_the_original_refusal(monkeypatch, failure):
    runtime = _Runtime(
        {"agent.list": CliError("CALLER_NOT_AUTHORIZED", "refused"), "adapter.list": {"adapters": []}}
    )
    monkeypatch.setattr(names, "get_services", lambda: _services(runtime, failure))

    with pytest.raises(CliError) as caught:
        names.name_collision_check()
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"
    assert caught.value.__cause__ is None


def _both_modes(tmp_path, monkeypatch, *, ordinary: bool):  # type: ignore[no-untyped-def]
    """The check's answer online (the daemon's agent.list) and offline (the registry file).

    PR 2941 second review: the owner's user agent holds the reserved name
    ``hyprial-user`` on every machine.  It has no harness, no desired-state
    row and is never started or stopped by name, so it cannot cause the
    adapter/worker confusion this check reports; both modes leave it out.
    An ORDINARY agent that holds the name (an older home) is reported.
    """

    from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, ensure_user_agent
    from hyprial.identity.impl.agents import AgentRegistry
    from tests.identity.agents.registry.entity.support import NODE, OWNER, application

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    if ordinary:
        registry.create(USER_AGENT_NAME)
    else:
        assert ensure_user_agent(agents=registry) == "created"
    registry.close()
    app = application(tmp_path)
    (tmp_path / "channels.json").write_text(json.dumps({"version": 1, "gateways": [
        {"provider": "lark", "name": USER_AGENT_NAME, "appId": "test",
         "credentialRef": "lark-u", "routes": []},
    ]}), encoding="utf-8")
    adapters = {"adapters": [{"name": USER_AGENT_NAME}]}
    answers = {}
    for mode, agents in (("online", lambda: app.handle("agent.list", {})),
                         ("offline", None)):
        runtime = _Runtime({
            "agent.list": agents() if agents else ipc_errors.DaemonUnavailableError("down"),
            "adapter.list": adapters,
        })
        services = _services(runtime)
        services._hyprial_home = lambda: tmp_path
        services._state_dir = lambda: tmp_path
        monkeypatch.setattr(names, "get_services", lambda services=services: services)
        answers[mode] = names.name_collision_check()
    return answers


def test_the_user_agent_is_no_collision_online_or_offline(monkeypatch, tmp_path):
    answers = _both_modes(tmp_path, monkeypatch, ordinary=False)

    assert answers == {"online": None, "offline": None}


def test_an_ordinary_agent_holding_the_reserved_name_collides_in_both_modes(monkeypatch, tmp_path):
    answers = _both_modes(tmp_path, monkeypatch, ordinary=True)

    assert answers["online"] == answers["offline"]
    assert answers["online"] is not None and "hyprial-user" in str(answers["online"]["detail"])


def test_the_doctor_marker_is_the_daemons_user_agent_marker():
    from hyprial.daemon.impl.agent_templates import TEMPLATE_CAPABILITY
    from hyprial.daemon.impl.user_agent.naming import USER_AGENT_TEMPLATE

    assert names.USER_AGENT_MARKER == (TEMPLATE_CAPABILITY, USER_AGENT_TEMPLATE)
