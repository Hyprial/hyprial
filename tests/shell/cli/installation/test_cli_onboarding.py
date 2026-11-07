"""New-machine init/login onboarding through the existing login test seams."""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.login.commands as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'tests/fixtures'))

from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402


import hyprial.daemon.impl.network.tailcat as tailcat_module
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonLaunchResult
from hyprial.daemon.impl.configuration.identity import resolve_node_owner
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.identity.impl.device import (
    DeviceRecord,
    device_key_path,
    write_device_record,
)

_OWNER = "alicesmith"


@pytest.fixture(autouse=True)
def _service_doctor_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    monkeypatch.setattr(
        service_module,
        "daemon_service_doctor_check",
        lambda **_kwargs: None,
    )
    # ``init`` installs the per-user supervisor by default; keep the real
    # systemctl/launchctl out of these onboarding cases as well.
    monkeypatch.setattr(
        service_module,
        "_run_service",
        lambda operation: {"ok": True, "supervision": "supervised"},
    )


def _free_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def _ownerless_environment(
    monkeypatch: pytest.MonkeyPatch, home: Path, state: Path
) -> dict[str, str]:
    """Remove both owner cells supplied by the suite's autouse fixture."""

    fixture_home = Path(os.environ["HYPRIAL_HOME"])
    (fixture_home / "settings.json").unlink(missing_ok=True)
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setenv("HYPRIAL_ZENOH_LISTEN", _free_endpoint())
    monkeypatch.setenv("HYPRIAL_ZENOH_CONNECT", "")
    monkeypatch.setenv("HYPRIAL_NODE_ID", "onboarding-test")
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)

    # Empty-control: if either fixture owner leaks back in, the onboarding
    # branch could stay green without ever exercising login.
    with pytest.raises(ValueError, match="user identity is not set"):
        resolve_node_owner({"HYPRIAL_HOME": str(home)}, hyprial_home=home)

    return {
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(state),
        "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_LISTEN": os.environ["HYPRIAL_ZENOH_LISTEN"],
        "HYPRIAL_ZENOH_CONNECT": "",
        "HYPRIAL_NODE_ID": "onboarding-test",
    }


def _profile(server: Any) -> NetworkProfile:
    return NetworkProfile(
        org="acme",
        issuer=server.issuer,
        client_id=server.config.client_id,
    )


def _stage_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Tailcat sidecar seam: the device stage provisions key+record
    for real (files under the test home), and doctor's sidecar check passes.
    """

    def ensure(home_path: Path, *, owner: str, device_id: str, environ: Any = None) -> DeviceRecord:
        key = device_key_path(Path(home_path))
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key.write_text('{"v": 3, "test-only": true}\n', encoding="utf-8")
        key.chmod(0o600)
        record = DeviceRecord(
            device_id=device_id,
            owner=owner,
            server_public="nodekey:onboarding-server",
            client_public="nodekey:onboarding-client",
            key_generation=1,
        )
        write_device_record(Path(home_path), record)
        return record

    monkeypatch.setattr(tailcat_module, "ensure_device_key", ensure)
    monkeypatch.setattr(
        tailcat_module,
        "verify_tailcat_sidecar",
        lambda home, environ=None: {
            "v": 6,
            "sidecar": "test-double",
            "tailcat": tailcat_module.TAILCAT_COMMIT,
        },
    )


def _wire_login(monkeypatch: pytest.MonkeyPatch, server: Any) -> None:
    profile = _profile(server)
    monkeypatch.setattr(
        _cli_get_services(), "resolve_profile", lambda environ=None, **kw: (profile, "profile.json")
    )
    import hyprial.shell.impl.login.flow as login_module

    monkeypatch.setattr(login_module, "_default_open", lambda _url: False)


def _wait_pid_gone(pid: int, process_identity: str | None) -> None:
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        # init/login ran in-process under CliRunner, so the daemon is this
        # test process's own child. Once it exits nothing reaps it until some
        # later Popen runs subprocess._cleanup(), and on Linux the zombie keeps
        # /proc/<pid>/stat with the same starttime -- identity would match for
        # the whole deadline. Reap it here (as test_gui_runtime does); a live
        # daemon is not reaped by WNOHANG and still fails below.
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if _read_process_identity(pid) != process_identity:
            return
        time.sleep(0.05)
    raise AssertionError(f"daemon pid {pid} survived test cleanup")


def _stop_daemon(env: dict[str, str], pid: int) -> None:
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    process_identity = _read_process_identity(pid)
    assert process_identity is not None
    stopped = CliRunner().invoke(_cli_root_app, ["daemon", "stop", "--json"], env=env)
    assert stopped.exit_code == 0, stopped.output
    _wait_pid_gone(pid, process_identity)


def _assert_discovery_disabled(state: Path) -> None:
    records = [
        json.loads(line)
        for line in (state / "logs" / "daemon.jsonl").read_text("utf-8").splitlines()
    ]
    resolved = [row for row in records if row.get("event") == "zenoh.endpoints.resolved"]
    assert resolved
    assert resolved[-1]["discovery"] == "disabled"
    assert resolved[-1]["discovered"] == 0


def test_init_without_owner_runs_login_then_starts_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-onboard-", dir=hyprial_test_tmp_root()) as raw:
        home, state = Path(raw) / "home", Path(raw) / "state"
        env = _ownerless_environment(monkeypatch, home, state)
        _stage_device(monkeypatch)
        calls = {"initialize": 0, "login": 0}
        original_initialize = _cli_get_services().initialize_hyprial_home
        original_flow = _cli_get_services()._run_login_cli_flow

        def counted_initialize() -> None:
            calls["initialize"] += 1
            original_initialize()

        def counted_flow(**kwargs: Any) -> dict[str, Any]:
            calls["login"] += 1
            return original_flow(**kwargs)

        monkeypatch.setattr(_cli_get_services(), "initialize_hyprial_home", counted_initialize)
        monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", counted_flow)

        with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
            _wire_login(monkeypatch, server)
            result = CliRunner().invoke(
                _cli_root_app,
                ["init", "--listen", env["HYPRIAL_ZENOH_LISTEN"], "--json"],
                env=env,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert calls == {"initialize": 1, "login": 1}
        assert payload["identity"]["owner"] == _OWNER
        # The tailnet-cutover device stage replaced the join report.
        assert payload["device"]["ready"] is True
        assert payload["device"]["deviceId"] == "onboarding-test"
        assert payload["running"] is True
        assert json.loads((home / "settings.json").read_text("utf-8"))["owner"] == _OWNER
        assert '"event": "device"' in (result.stderr or "")
        pid = payload["pid"]
        try:
            doctor = CliRunner().invoke(_cli_root_app, ["doctor", "--json"], env=env)
            assert doctor.exit_code == 0, doctor.output
            assert json.loads(doctor.stdout)["ok"] is True
            _assert_discovery_disabled(state)
        finally:
            _stop_daemon(env, pid)


def test_init_identity_failure_keeps_home_and_never_launches_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    launched: list[bool] = []
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kwargs: launched.append(True),
    )

    with fake_oidc(FakeOidcConfig(outcome="denied", pending_polls=0)) as server:
        _wire_login(monkeypatch, server)
        result = CliRunner().invoke(_cli_root_app, ["init", "--json"], env=env)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "ACCESS_DENIED"
    assert payload["data"]["nextSteps"] == ["hyprial login", "hyprial init"]
    assert "hyprial login" in payload["error"]
    assert "hyprial init" in payload["error"]
    assert home.is_dir()
    assert launched == []
    assert not (state / "daemon.json").exists()
    assert not (state / "daemon.sock").exists()


def test_init_interruption_names_recovery_and_never_launches_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    monkeypatch.setattr(
        _cli_get_services(), "_run_login_cli_flow", lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kwargs: pytest.fail("interrupted login must not launch daemon"),
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"], env=env)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "INTERRUPTED"
    assert payload["data"]["nextSteps"] == ["hyprial login", "hyprial init"]
    assert home.is_dir()


def test_init_with_owner_preserves_launch_shape_without_calling_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", "e2e")
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_kw: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("no daemon")))
    monkeypatch.setattr(_cli_get_services(), "_initialize_org_context", lambda: None)
    monkeypatch.setattr(
        _cli_get_services(),
        "_run_login_cli_flow",
        lambda **_kwargs: pytest.fail("existing owner must bypass login"),
    )
    status = {
        "running": True,
        "pid": 4242,
        "epoch": "same-shape",
        "phase": "serving",
        "owner": "e2e",
        "zenoh": {"listen": ["tcp/127.0.0.1:1"], "connect": []},
    }
    monkeypatch.setattr(
        _cli_get_services(), "_launch_daemon_process", lambda **_kwargs: DaemonLaunchResult.launched(status)
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "started": True,
        "alreadyRunning": False,
        # init configures autostart by default (see test_cli_init.py); the
        # supervisor here is the stubbed one from the module fixture.
        "service": {"ok": True, "supervision": "supervised"},
        **status,
    }


def test_login_identity_failure_keeps_initialized_home_and_names_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kwargs: pytest.fail("failed login must not launch daemon"),
    )

    with fake_oidc(FakeOidcConfig(outcome="denied", pending_polls=0)) as server:
        _wire_login(monkeypatch, server)
        result = CliRunner().invoke(_cli_root_app, ["login", "--json"], env=env)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "ACCESS_DENIED"
    assert payload["data"]["nextSteps"] == ["hyprial login"]
    assert "home is initialized" in payload["error"]
    assert home.is_dir()
    assert not (state / "daemon.json").exists()


def test_login_interruption_keeps_initialized_home_and_names_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    monkeypatch.setattr(
        _cli_get_services(), "_run_login_cli_flow", lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kwargs: pytest.fail("interrupted login must not launch daemon"),
    )

    result = CliRunner().invoke(_cli_root_app, ["login", "--json"], env=env)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "INTERRUPTED"
    assert payload["data"]["nextSteps"] == ["hyprial login"]
    assert "rerun `hyprial login`" in payload["error"]
    assert home.is_dir()
    assert not (state / "daemon.json").exists()


def test_init_keeps_identity_and_starts_daemon_when_device_provisioning_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    # No _stage_device: the sidecar is absent, so the device stage reports
    # ready=false (D12) while the identity and the daemon start are kept.
    status = {
        "running": True,
        "pid": 4242,
        "phase": "serving",
        "owner": _OWNER,
        "zenoh": {"listen": ["tcp/127.0.0.1:1"], "connect": []},
    }
    monkeypatch.setattr(
        _cli_get_services(), "_launch_daemon_process", lambda **_kwargs: DaemonLaunchResult.launched(status)
    )

    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        _wire_login(monkeypatch, server)
        result = CliRunner().invoke(_cli_root_app, ["init", "--json"], env=env)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["identity"]["owner"] == _OWNER
    assert payload["device"]["ready"] is False
    assert payload["device"]["error"]["code"] == "SIDECAR_MISSING"
    warning = next(item for item in payload["warnings"] if item["code"] == "DEVICE_KEY_NOT_READY")
    assert "provision the device key" in warning["message"]
    assert resolve_node_owner({"HYPRIAL_HOME": str(home)}, hyprial_home=home) == _OWNER


def test_login_without_home_initializes_once_and_finishes_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-onboard-", dir=hyprial_test_tmp_root()) as raw:
        home, state = Path(raw) / "home", Path(raw) / "state"
        env = _ownerless_environment(monkeypatch, home, state)
        calls = {"initialize": 0, "login": 0}
        original_initialize = _cli_get_services().initialize_hyprial_home
        original_flow = _cli_get_services()._run_login_cli_flow

        _stage_device(monkeypatch)

        def initialize_and_stage() -> None:
            calls["initialize"] += 1
            original_initialize()

        def counted_flow(**kwargs: Any) -> dict[str, Any]:
            calls["login"] += 1
            return original_flow(**kwargs)

        monkeypatch.setattr(_cli_get_services(), "initialize_hyprial_home", initialize_and_stage)
        monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", counted_flow)

        with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
            _wire_login(monkeypatch, server)
            result = CliRunner().invoke(_cli_root_app, ["login", "--json"], env=env)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert calls == {"initialize": 1, "login": 1}
        assert payload["identity"]["owner"] == _OWNER
        assert payload["device"]["ready"] is True
        assert payload["daemon"]["running"] is True
        assert home.is_dir()
        assert json.loads((home / "settings.json").read_text("utf-8"))["owner"] == _OWNER
        pid = payload["daemon"]["pid"]
        try:
            doctor = CliRunner().invoke(_cli_root_app, ["doctor", "--json"], env=env)
            assert doctor.exit_code == 0, doctor.output
            assert json.loads(doctor.stdout)["ok"] is True
            _assert_discovery_disabled(state)
        finally:
            _stop_daemon(env, pid)
        login_files = sorted(
            str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()
        )

        # Compare against the other public route, not a hand-built expected
        # list: both entries must leave the same home layout after shutdown.
        init_home, init_state = Path(raw) / "init-home", Path(raw) / "init-state"
        init_env = _ownerless_environment(monkeypatch, init_home, init_state)
        monkeypatch.setattr(_cli_get_services(), "initialize_hyprial_home", original_initialize)
        monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", original_flow)
        with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
            _wire_login(monkeypatch, server)
            initialized = CliRunner().invoke(
                _cli_root_app,
                ["init", "--listen", init_env["HYPRIAL_ZENOH_LISTEN"], "--json"],
                env=init_env,
            )
        assert initialized.exit_code == 0, initialized.output
        init_pid = json.loads(initialized.stdout)["pid"]
        _stop_daemon(init_env, init_pid)
        init_files = sorted(
            str(path.relative_to(init_home))
            for path in init_home.rglob("*")
            if path.is_file()
        )
        assert login_files == init_files


def test_login_bootstrap_uses_init_ready_timeout_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    env["HYPRIAL_INIT_READY_TIMEOUT"] = "2.75"
    observed: list[float] = []

    def complete_identity(**_kwargs: Any) -> dict[str, Any]:
        (home / "settings.json").write_text(
            json.dumps({"owner": _OWNER}), encoding="utf-8"
        )
        return {
            "ok": True,
            "identity": {"owner": _OWNER},
            "device": {"ready": True},
        }

    def launch(*, ready_timeout: float, **_kwargs: Any) -> DaemonLaunchResult:
        observed.append(ready_timeout)
        return DaemonLaunchResult.launched(
            {"running": True, "pid": 4242, "phase": "serving"}
        )

    monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", complete_identity)
    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)

    result = CliRunner().invoke(_cli_root_app, ["login", "--json"], env=env)

    assert result.exit_code == 0, result.output
    assert observed == [2.75]


@pytest.mark.parametrize("outcome", ["success", "denied"])
def test_default_json_login_without_home_keeps_device_behind_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    """Default ``login --json`` on a missing home: identity gates the device stage.

    The device stage (ensure_device_key) runs only after ``run_login``
    returned -- a denied authorization never touches the tailcat seam.
    """

    import hyprial.shell.impl.login.flow as login_module

    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    order: list[str] = []

    original_login = login_module.run_login

    def recording_login(*args: Any, **kwargs: Any) -> Any:
        order.append("identity-start")
        result = original_login(*args, **kwargs)
        order.append("identity-ok")
        return result

    def recording_device(home_path: Path, *, owner: str, device_id: str, environ: Any = None) -> DeviceRecord:
        order.append("device")
        return DeviceRecord(
            device_id=device_id,
            owner=owner,
            server_public="nodekey:onboarding-server",
            client_public="nodekey:onboarding-client",
            key_generation=1,
        )

    monkeypatch.setattr(login_module, "run_login", recording_login)
    monkeypatch.setattr(tailcat_module, "ensure_device_key", recording_device)
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kwargs: DaemonLaunchResult.launched(
            {"running": True, "pid": 4242, "phase": "serving"}
        ),
    )

    with fake_oidc(FakeOidcConfig(outcome=outcome, pending_polls=0)) as server:
        _wire_login(monkeypatch, server)
        result = CliRunner().invoke(_cli_root_app, ["login", "--json"], env=env)

    assert home.is_dir()
    if outcome == "denied":
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["code"] == "ACCESS_DENIED"
        assert order == ["identity-start"]
    else:
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["identity"]["owner"] == _OWNER
        assert payload["device"]["ready"] is True
        assert order == ["identity-start", "identity-ok", "device"]


# -- merge glue #493 x #508: login routing and first-time daemon start --------


def _record_login_route(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[Any]]:
    """Stub both halves so a login run only records which route it took."""

    seen: dict[str, list[Any]] = {"flow": [], "complete": []}

    def flow(**kwargs: Any) -> dict[str, Any]:
        seen["flow"].append(kwargs)
        return {"ok": True, "identity": {"owner": _OWNER}}

    def complete(**kwargs: Any) -> dict[str, Any]:
        seen["complete"].append(kwargs)
        return {"running": True, "pid": 4242}

    monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", flow)
    monkeypatch.setattr(_cli_get_services(), "_complete_initialization", complete)
    monkeypatch.setattr(_cli_get_services(), "_initialize_org_context", lambda: None)
    return seen


@pytest.mark.parametrize(
    ("cell", "first_time"),
    [
        ("home-missing", True),
        ("home-missing-env-owner", True),
        ("home-without-owner", True),
        ("owner-with-empty-issuer", False),
        ("env-owner-only", False),
    ],
)
def test_login_routes_first_time_setup_by_owner_not_by_home_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cell: str, first_time: bool
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    _ownerless_environment(monkeypatch, home, state)
    if cell != "home-missing" and cell != "home-missing-env-owner":
        home.mkdir()
    if cell == "owner-with-empty-issuer":
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner", "identityIssuer": ""}), encoding="utf-8"
        )
    if cell in {"env-owner-only", "home-missing-env-owner"}:
        monkeypatch.setenv("HYPRIAL_OWNER", "env-owner")
    seen = _record_login_route(monkeypatch)

    assert _cli_owner_1._login_route_is_first_time_setup() is first_time
    result = CliRunner().invoke(_cli_root_app, ["login", "--json"])

    assert result.exit_code == 0, result.output
    assert len(seen["flow"]) == 1
    flow_kwargs = seen["flow"][0]
    if first_time:
        assert flow_kwargs.get("first_time_setup") is True
        assert flow_kwargs.get("no_daemon") is True
        assert len(seen["complete"]) == 1
        assert home.is_dir()
    else:
        assert flow_kwargs.get("first_time_setup", False) is False
        assert flow_kwargs.get("no_daemon") is False
        assert seen["complete"] == []


@pytest.mark.parametrize(
    "settings_text",
    ["{not json", json.dumps(["owner", "not-an-object"])],
    ids=["unparseable", "not-an-object"],
)
def test_malformed_settings_is_not_silently_routed_to_first_time_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_text: str
) -> None:
    """Malformed settings are not provably ownerless: they stay on #493's path,
    which reports them, instead of being bootstrapped over as a fresh home."""

    home, state = tmp_path / "home", tmp_path / "state"
    _ownerless_environment(monkeypatch, home, state)
    home.mkdir()
    (home / "settings.json").write_text(settings_text, encoding="utf-8")

    assert _cli_owner_1._login_route_is_first_time_setup() is False


@pytest.mark.parametrize("entry", ["init", "login"])
def test_first_time_setup_starts_daemon_once_via_init_with_the_committed_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str
) -> None:
    """The identity step never launches; init's start path launches exactly
    once, with init's endpoints and ready budget.  The identity lock is
    released before the launch (the daemon acquires it itself) and the
    committed generation travels as the launch check."""

    from hyprial.identity.impl.identity_transaction import (
        IdentityTransactionBusy,
        IdentityTransactionLock,
    )

    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    env["HYPRIAL_INIT_READY_TIMEOUT"] = "2.5"
    listen = _free_endpoint()
    launches: list[dict[str, Any]] = []

    def lock_is_held_elsewhere() -> bool:
        try:
            IdentityTransactionLock.acquire(home, timeout=0).close()
        except IdentityTransactionBusy:
            return True
        return False

    def launch(**kwargs: Any) -> DaemonLaunchResult:
        launches.append({**kwargs, "lockHeld": lock_is_held_elsewhere()})
        return DaemonLaunchResult.launched(
            {
                "running": True,
                "pid": 4242,
                "phase": "serving",
                "owner": _OWNER,
                "migration": {"status": "applied", "rewrittenCells": 0},
                "zenoh": {"listen": [listen], "connect": []},
            }
        )

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)
    arguments = (
        ["init", "--listen", listen, "--json"]
        if entry == "init"
        else ["login", "--json"]
    )
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        _wire_login(monkeypatch, server)
        result = CliRunner().invoke(_cli_root_app, arguments, env=env)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(launches) == 1
    assert launches[0]["ready_timeout"] == 2.5
    assert launches[0]["listen"] == (listen if entry == "init" else None)
    assert launches[0]["lockHeld"] is False
    assert "identity_transaction" not in launches[0]
    assert launches[0]["expected_identity_generation"] == 1
    assert payload["identity"]["owner"] == _OWNER
    # No sidecar on this machine: the device stage reports not-ready, the
    # identity is kept and there is no partial failure (D12).
    assert payload["device"]["ready"] is False
    assert payload["device"]["error"]["code"] == "SIDECAR_MISSING"
    if entry == "login":
        assert payload["migration"]["status"] == "applied"
        assert payload["daemon"]["running"] is True


def test_switch_account_dry_run_on_missing_home_refuses_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    monkeypatch.setattr(
        _cli_get_services(),
        "resolve_profile",
        lambda *_a, **_kw: pytest.fail("dry-run on a missing home reached identity"),
    )

    def listing() -> list[tuple[str, int, int]]:
        return sorted(
            (str(path), path.stat().st_size, path.stat().st_mtime_ns)
            for path in tmp_path.rglob("*")
        )

    before = listing()
    result = CliRunner().invoke(
        _cli_root_app, ["login", "--switch-account", "--dry-run", "--json"], env=env
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "HYPRIAL_HOME_NOT_INITIALIZED"
    assert "writes nothing" in payload["error"]
    assert not home.exists()
    assert listing() == before
