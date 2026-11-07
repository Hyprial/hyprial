"""Doctor name-collision diagnostics: online request first, B06 offline only."""

from __future__ import annotations

import json

import pytest

from hyprial.kernel import daemon_request, ipc_errors
from hyprial.shell.impl.cli.commands.common.support import CliError
from hyprial.shell.impl.cli.commands.doctor.checks import names


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


def _services(runtime):
    return type("Services", (), {"runtime": runtime, "CliError": CliError})()


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


def test_other_request_error_is_not_silently_treated_as_offline(monkeypatch):
    runtime = _Runtime(
        {
            "agent.list": ipc_errors.CallerNotAuthorizedError("no") if hasattr(ipc_errors, "CallerNotAuthorizedError") else CliError("CALLER_NOT_AUTHORIZED", "no"),
            "adapter.list": {"adapters": []},
        }
    )
    monkeypatch.setattr(names, "get_services", lambda: _services(runtime))

    with pytest.raises(CliError) as caught:
        names.name_collision_check()
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"
