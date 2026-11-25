"""KP-092 option B: ``daemon stop`` says when a supervisor may restart the daemon.

A LOADED unit this installation cannot attribute is never stopped by the CLI,
so the IPC stop may be undone. The answer keeps ``ok: true`` and
``teardownCompleted`` and gains ``supervisorMayRestart`` plus
``unattributedSupervisor``; both keys are absent in every other case.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import daemon_stop
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app

from .test_same_installation_stop import _install_unit, interpreters  # noqa: F401
from .test_stop_routing import stop_env  # noqa: F401

NEW_KEYS = ("supervisorMayRestart", "unattributedSupervisor")


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_a_loaded_foreign_unit_adds_the_restart_fields_and_keeps_the_rest(
    stop_env, tmp_path, interpreters, monkeypatch, capsys, platform: str  # noqa: F811
) -> None:
    _services, ipc, _build = stop_env
    _unused, runner = _install_unit(
        tmp_path, interpreters["base"], interpreters["console"], platform, monkeypatch
    )

    result = daemon_stop._stop_daemon_for_operator()

    assert result["ok"] is True
    assert result["teardownCompleted"] is True
    assert result["supervisorMayRestart"] is True
    unit = result["unattributedSupervisor"]
    assert set(unit) == {"platform", "unit", "attributed", "ownershipReason"}
    assert unit["platform"] == platform
    assert unit["attributed"] is False
    assert str(tmp_path) not in json.dumps(unit)
    assert runner.mutations() == []
    assert ipc == [("shutdown",)]
    assert "not attributable" in capsys.readouterr().err


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_json_output_is_one_object_with_the_fields(
    stop_env, tmp_path, interpreters, monkeypatch, platform: str  # noqa: F811
) -> None:
    _install_unit(
        tmp_path, interpreters["base"], interpreters["console"], platform, monkeypatch
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "stop", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["supervisorMayRestart"] is True


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_an_attributable_unit_stopped_by_custody_has_no_restart_fields(
    stop_env, tmp_path, interpreters, monkeypatch, platform: str  # noqa: F811
) -> None:
    _install_unit(
        tmp_path, interpreters["module"], interpreters["console"], platform, monkeypatch
    )

    result = daemon_stop._stop_daemon_for_operator()

    assert not any(key in result for key in NEW_KEYS)
    assert result["supervisor"]["platform"] == platform


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_no_unit_has_no_restart_fields(stop_env, platform: str) -> None:  # noqa: F811
    _services, _ipc, build = stop_env
    build(platform, installed=False, loaded=False, job_pid=0)

    result = daemon_stop._stop_daemon_for_operator()

    assert not any(key in result for key in NEW_KEYS)


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_an_unloaded_foreign_unit_warns_but_adds_no_fields(
    stop_env, platform: str, capsys  # noqa: F811
) -> None:
    _services, _ipc, build = stop_env
    manager, _runner = build(platform, installed=True, loaded=False, job_pid=0)
    manager.unit_path.write_text("foreign\n", encoding="utf-8")

    result = daemon_stop._stop_daemon_for_operator()

    assert not any(key in result for key in NEW_KEYS)
    assert "not attributable" in capsys.readouterr().err
