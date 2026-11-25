"""``daemon status`` and ``doctor`` against a daemon that is up but has no account.

Decision: daemon-first-z2-status-real-exception-20261116.  The request port
turns every wire refusal into ``services.CliError`` (``RequestPortError``), not
a kernel ``DaemonRequestError``; a test that makes the runtime raise the kernel
class passes while the real command fails.  The mocked cases below therefore
raise the real class, and the real-socket cases serve an actual
``BootstrapWireHost`` and go through the real request port.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon import BootstrapWireHost
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.daemon.commands as _daemon_commands
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from tests.support.runtime._tmp import hyprial_test_tmp_root

PROBE = {
    "running": True,
    "pid": 123,
    "epoch": "bootstrap-epoch",
    "nodeId": "node",
    "owner": None,
    "socket": "/tmp/daemon.sock",
    "phase": "bootstrap",
}


def _status_with_refusal(
    monkeypatch: pytest.MonkeyPatch, error: Exception, probe: object = None
) -> dict:
    services = _cli_get_services()
    captured: dict[str, object] = {}

    class _Runtime:
        def request(self, method: str, **_kwargs: object) -> object:
            assert method == "ps"
            raise error

        def execute(self, operation, **_kwargs: object) -> None:  # type: ignore[no-untyped-def]
            captured["result"] = operation()

    monkeypatch.setattr(services, "runtime", _Runtime())
    def _probe(**_kwargs: object) -> dict:
        if isinstance(probe, Exception):
            raise probe
        return dict(PROBE if probe is None else probe)

    monkeypatch.setattr(services, "_daemon_probe", _probe)
    _daemon_commands.daemon_status(json_output=True)
    return captured["result"]


@pytest.mark.parametrize(
    "code", [ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.METHOD_NOT_FOUND, "ACCOUNT_REQUIRED"]
)
def test_status_falls_back_to_ping_for_the_refusals_a_bootstrap_daemon_sends(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    refusal = _cli_get_services().CliError(code, "bootstrap refusal")
    assert _status_with_refusal(monkeypatch, refusal) == {"ok": True, "daemon": PROBE}


def test_status_does_not_swallow_other_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = _cli_get_services().CliError(ipc_errors.INVALID_ARGUMENT, "not a bootstrap refusal")
    with pytest.raises(type(refusal)) as raised:
        _status_with_refusal(monkeypatch, refusal)
    assert raised.value.code == ipc_errors.INVALID_ARGUMENT


@pytest.mark.parametrize("phase", ["serving", "reconciled", None])
def test_status_reraises_the_refusal_when_the_probe_is_not_a_bootstrap(
    monkeypatch: pytest.MonkeyPatch, phase: str | None
) -> None:
    refusal = _cli_get_services().CliError(ipc_errors.METHOD_NOT_FOUND, "ps is not served")
    probe = {key: value for key, value in PROBE.items() if key != "phase"}
    if phase is not None:
        probe["phase"] = phase
    with pytest.raises(type(refusal)) as raised:
        _status_with_refusal(monkeypatch, refusal, probe)
    assert raised.value is refusal


def test_status_reraises_the_refusal_when_the_probe_itself_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = _cli_get_services().CliError("ACCOUNT_REQUIRED", "refused")
    with pytest.raises(type(refusal)) as raised:
        _status_with_refusal(monkeypatch, refusal, ipc_errors.DaemonUnavailableError("gone"))
    assert raised.value is refusal


class _ServedBootstrap:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
        self.root = Path(tempfile.mkdtemp(prefix="z2s", dir=hyprial_test_tmp_root()))
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir(mode=0o700)
        (self.home / "settings.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
        services = _cli_get_services()
        monkeypatch.setattr(services, "_hyprial_home", lambda: self.home)
        monkeypatch.setattr(services, "_state_dir", lambda: self.state)
        monkeypatch.setattr(services, "_socket_path", lambda: self.state / "daemon.sock")
        self.host = BootstrapWireHost.open(self.home, state_dir=self.state)
        self.outcome: list[str] = []
        self.thread = threading.Thread(target=lambda: self.outcome.append(self.host.serve()))
        self.thread.start()

    def close(self) -> None:
        self.host.request_stop()
        self.thread.join(timeout=30)
        self.host.close()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch):
    daemon = _ServedBootstrap(monkeypatch)
    try:
        yield daemon
    finally:
        daemon.close()


def test_status_against_a_real_bootstrap_socket_reports_the_phase(served) -> None:
    result = CliRunner().invoke(_cli_root_app, ["daemon", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["daemon"]["running"] is True
    assert payload["daemon"]["phase"] == "bootstrap"
    assert payload["daemon"]["owner"] is None


def test_name_collision_check_is_not_evaluable_against_a_real_bootstrap_socket(served) -> None:
    from hyprial.shell.impl.cli.commands.doctor.checks.names import name_collision_check

    check = name_collision_check()

    assert check is not None
    assert check["status"] == "warn"
    assert check["detail"].startswith("NOT-EVALUABLE")
    assert "ACCOUNT_REQUIRED" in check["detail"]


def test_doctor_reports_a_bootstrap_daemon_as_one_json_object(served) -> None:
    from hyprial.shell.impl.cli.commands.doctor import commands as doctor_commands

    report = doctor_commands._doctor_result()

    checks = {item["name"]: item for item in report["checks"]}
    assert checks["daemon"]["status"] == "warn"
    assert "bootstrap" in checks["daemon"]["detail"]
    assert checks["agent-adapter-name-collision"]["status"] == "warn"
    assert report["exitCode"] in {0, 1, 10}
    json.dumps(report)


def test_doctor_still_fails_for_a_refusal_that_is_not_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hyprial.shell.impl.cli.commands.doctor import commands as doctor_commands

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    services = _cli_get_services()
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    refusal = services.CliError(ipc_errors.INVALID_ARGUMENT, "not a bootstrap refusal")

    class _Runtime:
        def request(self, method: str, *_args: object, **_kwargs: object) -> object:
            if method == "agent.list":
                return {"agents": []}
            if method == "adapter.list":
                return {"adapters": []}
            raise refusal

    monkeypatch.setattr(services, "runtime", _Runtime())
    monkeypatch.setattr(services, "_daemon_probe", lambda **_kwargs: dict(PROBE))

    report = doctor_commands._doctor_result()

    daemon = [item for item in report["checks"] if item["name"] == "daemon"]
    assert [item["status"] for item in daemon] == ["fail"]
    assert "not a bootstrap refusal" in daemon[0]["detail"]
    assert report["exitCode"] == 1
