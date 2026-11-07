"""Service mutations must not escape the configured home or account domain."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from hyprial.daemon.impl.operations.service import (
    ServiceConfig,
    ServiceManager,
    ServiceOwnershipError,
)


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
@pytest.mark.parametrize("operation", ["install", "uninstall", "restart", "reconcile"])
def test_nondefault_home_refused_before_runner(tmp_path, platform, operation):
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    manager = ServiceManager(
        ServiceConfig(Path("/opt/hyprial"), tmp_path, tmp_path / "test-home", tmp_path / "state"),
        platform=platform, runner=runner,
    )
    with pytest.raises(ServiceOwnershipError) as caught:
        if operation == "restart":
            manager.restart(lambda: pytest.fail("must not probe daemon"))
        elif operation == "reconcile":
            manager.reconcile(lambda: pytest.fail("must not probe daemon"), stop_stray=lambda _: None)
        else:
            getattr(manager, operation)()
    assert caught.value.reason == "service-home-nondefault"
    assert commands == []
    assert not manager.unit_path.exists()


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_fake_home_cannot_reach_real_user_manager(tmp_path, monkeypatch, platform):
    import hyprial.daemon.impl.operations.service as service

    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: pytest.fail("real manager invoked"))
    manager = ServiceManager(
        ServiceConfig(Path("/opt/hyprial"), tmp_path, tmp_path / ".hyprial", tmp_path / "state"),
        platform=platform,
    )
    with pytest.raises(ServiceOwnershipError) as caught:
        manager.install()
    assert caught.value.reason == "service-account-home-mismatch"
    assert not manager.unit_path.exists()


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_default_account_home_still_reaches_runner(tmp_path, monkeypatch, platform):
    import pwd
    from types import SimpleNamespace
    import hyprial.daemon.impl.operations.service as service

    commands = []
    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(tmp_path)))

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", run)
    manager = ServiceManager(
        ServiceConfig(Path("/opt/hyprial"), tmp_path, tmp_path / ".hyprial", tmp_path / "state"),
        platform=platform,
    )
    manager._require_service_home()
    command = ("launchctl", "print", "gui/501") if platform == "launchd" else ("systemctl", "--user", "show")
    manager._run(command)
    assert commands == [command]


def test_failloud_environment_isolates_home_and_xdg(tmp_path, monkeypatch):
    from tests.daemon.application.transport.test_daemon_zenoh_failloud import _environment

    monkeypatch.setenv("HOME", "/ambient-home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/ambient-config")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/ambient-bus")
    env = _environment(tmp_path)
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert "DBUS_SESSION_BUS_ADDRESS" not in env


@pytest.mark.parametrize("builder", [
    "adapter-online", "orgfs-purge", "owner-migration", "link-parity",
    "codex-preflight", "network-isolated", "pac-principal",
])
def test_additional_daemon_builders_own_home_and_xdg(tmp_path, monkeypatch, builder):
    from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness
    from tests.daemon.orgfs.purge.test_orgfs_purge_receipt import _IsolatedCli
    from tests.shell.cli.adapters.test_cli_adapter_online import _online_daemon

    def assert_root_local(env, root):
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            assert Path(env[key]).resolve().is_relative_to(root.resolve()), (builder, key)

    if builder == "link-parity":
        from tests.integration.cli.link.test_link_daemon_parity import _environment

        assert_root_local(_environment(tmp_path), tmp_path)
    elif builder == "codex-preflight":
        from tests.integration.e2e.e2e019.test_codex_credential_preflight import IsolatedCodexNode

        assert_root_local(IsolatedCodexNode(tmp_path).environment, tmp_path)
    elif builder in ("network-isolated", "pac-principal"):
        class CapturedEnvironment(Exception):
            pass

        if builder == "network-isolated":
            from types import SimpleNamespace
            from tests.daemon.network.test_network_isolated import _run_daemon

            def capture(*args, **kwargs):
                assert_root_local(kwargs["env"], tmp_path)
                raise CapturedEnvironment

            monkeypatch.setattr("subprocess.Popen", capture)
            with pytest.raises(CapturedEnvironment):
                _run_daemon(tmp_path, isolated=True, sentinel=SimpleNamespace(port=1), observe=0)
        else:
            from tests.integration.pac.principal import test_interactive_cli_principal_e2e as principal

            def capture_cli(env, *args):
                assert_root_local(env, Path(env["HYPRIAL_HOME"]).parent)
                raise CapturedEnvironment

            monkeypatch.setattr(principal, "_cli", capture_cli)
            with pytest.raises(CapturedEnvironment):
                principal.test_interactive_codex_cli_workflow_notice_stays_with_agent()
    elif builder == "adapter-online":
        class CapturedEnvironment(Exception):
            pass

        def capture(*args, **kwargs):
            env = kwargs["env"]
            assert_root_local(env, Path(env["HYPRIAL_HOME"]))
            raise CapturedEnvironment

        monkeypatch.setattr("subprocess.Popen", capture)
        with pytest.raises(CapturedEnvironment), _online_daemon():
            pytest.fail("must not start a real daemon")
    else:
        harness = _IsolatedCli(tmp_path) if builder == "orgfs-purge" else IsolatedHarness(tmp_path)
        assert_root_local(harness.environment, tmp_path)
