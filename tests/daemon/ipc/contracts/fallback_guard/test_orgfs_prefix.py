"""OrgFS family admission keeps its existing IPC and worker boundary."""

import pytest

from hyprial.daemon.impl.ipc import router
from tests.daemon.workers.test_worker_relay import frame, isolated as isolated, request


@pytest.mark.parametrize("method", ["orgfs", "orgfs.", "orgfs.unknown"])
def test_unknown_orgfs_refused_without_bridge(isolated, monkeypatch, method):
    _, app, _, _ = isolated
    monkeypatch.setattr(app, "_handle_orgfs", lambda *_a, **_kw: pytest.fail("bridge reached"))
    response = request(app.socket_path, frame(method))
    assert response["error"] == {"code": "METHOD_NOT_FOUND", "message": f"unknown daemon method {method}"}


@pytest.mark.parametrize("remove_marker", [False, True])
def test_missing_read_binding_refused_without_bridge(isolated, monkeypatch, remove_marker):
    from tests.daemon.ipc.contracts.fallback_guard.test_migrated_fallbacks import _WithoutOne

    _, app, _, _ = isolated
    monkeypatch.setattr(app, "_handle_orgfs", lambda *_a, **_kw: pytest.fail("bridge reached"))
    monkeypatch.setattr(router, "METHOD_REGISTRY", _WithoutOne(router.METHOD_REGISTRY, "orgfs.read"))
    if remove_marker:
        monkeypatch.setattr(router, "MIGRATED_METHODS", router.MIGRATED_METHODS - {"orgfs.read"})
    response = request(app.socket_path, frame("orgfs.read"))
    assert response["error"] == {"code": "METHOD_NOT_FOUND", "message": "unknown daemon method orgfs.read"}


@pytest.mark.parametrize("method", ["orgfs.spaces", "orgfs.unknown"])
def test_restore_refuses_before_orgfs_dispatch(isolated, monkeypatch, method):
    _, app, _, _ = isolated
    monkeypatch.setattr(app, "_handle_orgfs", lambda *_a, **_kw: pytest.fail("bridge reached"))
    app._restore_done.clear()
    try:
        response = request(app.socket_path, frame(method))
        assert response["error"] == {"code": "DAEMON_RESTORING", "message": "daemon is restoring its connectors; only the light methods (ping, shutdown) are served until restore completes"}
    finally:
        app._restore_done.set()


@pytest.mark.parametrize("method", ["orgfs.read", "orgfs.unknown"])
def test_worker_channel_rejects_orgfs_before_dispatch(isolated, monkeypatch, method):
    _, app, _, binding = isolated
    monkeypatch.setattr(app, "_handle_orgfs", lambda *_a, **_kw: pytest.fail("bridge reached"))
    response = request(app.socket_path, frame("worker.channel.request", binding.envelope(frame(method))))
    assert response["error"] == {"code": "METHOD_NOT_FOUND", "message": "Method unavailable on the worker channel"}


def test_registered_read_and_write_reach_real_bridge(isolated):
    _, app, _, _ = isolated
    try:
        made = request(app.socket_path, frame("orgfs.create", {"name": "prefix-test"}))["result"]
        spaces = request(app.socket_path, frame("orgfs.spaces"))["result"]["spaces"]
        assert any(item["space_id"] == made["space_id"] and item["name"] == "prefix-test" for item in spaces)
    finally:
        if app._orgfs_runtime is not None:
            app._orgfs_runtime.close()
            app._orgfs_runtime = None


def test_cli_space_output_and_error_match_across_prefix_retirement(isolated, monkeypatch, record_property):
    import json
    import re

    from typer.testing import CliRunner
    from hyprial.daemon.impl.orgfs.document.operations import spaces
    from hyprial.kernel import daemon_request
    from hyprial.shell.impl.cli.commands.common import support
    from hyprial.shell.impl.cli.commands.common.root import app as cli_app
    from hyprial.shell.impl.cli.commands.common.services import get_services
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

    _, app, _, _ = isolated
    monkeypatch.setattr(spaces, "_now", lambda: "2027-01-15T08:00:00Z")
    monkeypatch.setattr(get_services(), "require_initialized_hyprial_home", lambda: app.hyprial_home)
    monkeypatch.setattr(support, "_daemon_request", lambda method, params=None, **kwargs:
                        daemon_request(method, params, socket_path=app.socket_path, timeout=10))
    runner = CliRunner()
    commands = [
        ["org", "fs", "space", "create", "alpha"],
        ["org", "fs", "space", "create", "beta", "--json"],
        ["org", "fs", "space", "list"],
        ["org", "fs", "space", "list", "--json"],
        ["org", "fs", "read", "missing-space", "missing", "--json"],
    ]
    observations = []
    try:
        for command in commands:
            result = runner.invoke(cli_app, command)
            normalized = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<space-id>", result.stdout)
            observations.append({"command": command, "stdout": normalized, "stderr": result.stderr, "exit": result.exit_code})
        assert [item["exit"] for item in observations] == [0, 0, 0, 0, 1]
        assert json.loads(observations[1]["stdout"])["ok"] is True
        assert json.loads(observations[3]["stdout"])["ok"] is True
        assert json.loads(observations[4]["stdout"])["code"] == "unknown-space"
        record_property("cli_observations", json.dumps(observations, sort_keys=True))
    finally:
        if app._orgfs_runtime is not None:
            app._orgfs_runtime.close()
            app._orgfs_runtime = None


def test_cli_offline_and_restore_errors_match_across_prefix_retirement(isolated, monkeypatch, record_property, tmp_path):
    import json

    from typer.testing import CliRunner
    from hyprial.kernel import daemon_request
    from hyprial.shell.impl.cli.commands.common import support
    from hyprial.shell.impl.cli.commands.common.root import app as cli_app
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

    _, app, _, _ = isolated
    socket = [app.socket_path]
    monkeypatch.setattr(support, "_daemon_request", lambda method, params=None, **kwargs:
                        daemon_request(method, params, socket_path=socket[0], timeout=1, restore_wait=0))
    runner = CliRunner()
    app._restore_done.clear()
    try:
        restoring = runner.invoke(cli_app, ["org", "fs", "space", "list", "--json"])
        app._restore_done.set()
        socket[0] = tmp_path / "missing.sock"
        offline = runner.invoke(cli_app, ["org", "fs", "space", "list", "--json"])
        observations = [{"stdout": result.stdout, "stderr": result.stderr, "exit": result.exit_code}
                        for result in (restoring, offline)]
        assert [item["exit"] for item in observations] == [1, 1]
        assert json.loads(restoring.stdout)["code"] == "DAEMON_RESTORING"
        assert json.loads(offline.stdout)["ok"] is False
        record_property("cli_unavailable_observations", json.dumps(observations, sort_keys=True))
    finally:
        app._restore_done.set()
