"""Upgrade clients must call the daemon, not open local business authorities."""
from types import SimpleNamespace

import pytest


def test_doctor_fix_calls_run_then_status_only(monkeypatch):
    from hyprial.shell.impl.cli.commands.doctor import upgrade_steps as module
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        return {"steps": [{"id": "org-membership-v1", "state": "pending"}],
                "pending": ["org-membership-v1"], "legacyArgvFiles": [], "legacyProcesses": []}

    def forbidden(*args, **kwargs):
        pytest.fail("CLI must not open an upgrade runner or state authority")

    services = SimpleNamespace(runtime=SimpleNamespace(request=request), _state_dir=forbidden,
                               _hyprial_home=forbidden, CliError=RuntimeError)
    monkeypatch.setattr(module, "get_services", lambda: services)
    result = module._upgrade_steps_doctor_check(fix=True)
    assert result["status"] == "warn"
    assert [method for method, _ in calls] == ["upgrade.steps.run", "upgrade.steps.status"]


@pytest.mark.parametrize("transient", [False, True])
def test_unavailable_daemon_is_pending_without_local_execution(transient):
    from hyprial.kernel import RequestPortError, ipc_errors
    from hyprial.shell.impl.cli.commands.upgrade.commands import _run_upgrade_steps

    def unavailable(*args, **kwargs):
        if transient:
            raise ipc_errors.DaemonUnavailableError("not running")
        raise RequestPortError("DAEMON_UNAVAILABLE", "not running")

    def forbidden():
        raise AssertionError("offline upgrade client must not open local migration state")

    services = SimpleNamespace(runtime=SimpleNamespace(request=unavailable),
                               _state_dir=forbidden, _hyprial_home=forbidden)
    result = _run_upgrade_steps(services)
    assert result["unavailable"] and result["pending"]


@pytest.mark.parametrize("state", ["applied_unconfirmed", "conflict"])
def test_doctor_distinguishes_receiver_confirmation_state(monkeypatch, state):
    from hyprial.shell.impl.cli.commands.doctor import upgrade_steps as module

    def request(*args, **kwargs):
        return {"steps": [{"id": "org-membership-v1", "state": state}],
                "pending": ["org-membership-v1"], "legacyArgvFiles": [], "legacyProcesses": []}

    monkeypatch.setattr(module, "get_services", lambda: SimpleNamespace(runtime=SimpleNamespace(request=request)))
    result = module._upgrade_steps_doctor_check(fix=True)
    assert result["status"] == ("ok" if state == "applied_unconfirmed" else "warn")
    assert state in result["detail"]
    assert ("action" in result) == (state == "conflict")


@pytest.mark.parametrize("problem", ["pending", "failed", "conflicts"])
def test_doctor_unconfirmed_does_not_hide_other_migration_problems(monkeypatch, problem):
    from hyprial.shell.impl.cli.commands.doctor import upgrade_steps as module

    def request(*args, **kwargs):
        return {"steps": [{"id": "org-membership-v1", "state": "applied_unconfirmed",
                           "data": {problem: [{"reason": "other-problem"}]}}],
                "pending": ["org-membership-v1"], "legacyArgvFiles": [], "legacyProcesses": []}

    monkeypatch.setattr(module, "get_services", lambda: SimpleNamespace(runtime=SimpleNamespace(request=request)))
    result = module._upgrade_steps_doctor_check(fix=False)
    assert result["status"] == "warn"
    assert "action" in result


def test_upgrade_restart_handoff_calls_daemon_only(monkeypatch):
    from hyprial.shell.impl.cli.commands.upgrade.commands import _run_upgrade_steps
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("CLI must not execute migration locally")

    def request(method, params=None, **kwargs):
        calls.append(method)
        return {"steps": [], "pending": []}

    services = SimpleNamespace(runtime=SimpleNamespace(request=request), _state_dir=forbidden,
                               _hyprial_home=forbidden)
    assert _run_upgrade_steps(services)["pending"] == []
    assert calls == ["upgrade.steps.run"]
