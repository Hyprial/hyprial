"""adapter.reload IPC: hot-pick-up `adapter add` without a daemon restart."""

from __future__ import annotations

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

import json
import sys
import textwrap
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.worker.process import LarkWorkerLauncher
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, LarkPortClient
from hyprial.kernel import PersistentConfigStore


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _channels(*names: str) -> dict[str, object]:
    return {
        "version": 1,
        "gateways": [
            {
                "provider": "lark",
                "name": name,
                "appId": f"app-{name}",
                "credentialRef": name,
                "routes": [],
            }
            for name in names
        ],
    }


_FAKE_WORKER = """
import json, os, sys, time




name = sys.argv[1]
fd = int(os.environ["HYPRIAL_LARK_READY_FD"])
os.write(fd, json.dumps({"status": "online", "name": name}).encode() + b"\\n")
while True:
    time.sleep(0.05)
"""


def _application(tmp_path: Path, *configured: str) -> DaemonApplication:
    home = tmp_path / "home"
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_json(home / "channels.json", _channels(*configured))
    for name in configured:
        _write_json(
            home / "secrets" / f"{name}.json",
            {"appSecret": f"secret-{name}"},
        )
    script = tmp_path / "fake_worker.py"
    script.write_text(textwrap.dedent(_FAKE_WORKER), encoding="utf-8")
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="node-1",
        hyprial_home=home,
        owner="op",
        **_app_injections(),
    )
    launcher = LarkWorkerLauncher(
        hyprial_home=home,
        state_dir=state,
        socket_path=state / "daemon.sock",
        config_store=PersistentConfigStore(home, state),
        command=[sys.executable, str(script)],
        startup_timeout=5.0,
    )
    channels = app.load_persistent_configuration().channels
    events = CorrelatedDomainEvents()
    app._adapters = AdapterRuntime(
        launcher,
        channels,
        restore_confirm_timeout=1.0,
        event_sink=events,
        configuration_source=lambda: app.load_persistent_configuration().channels,
    )
    app._lark_events = events
    app._lark_client = LarkPortClient(app._adapters, events)
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    return app


def _close_application(app: DaemonApplication) -> None:
    try:
        app._close()
    finally:
        app._close_home_lease()


def test_start_after_add_requires_no_daemon_restart(tmp_path: Path) -> None:
    app = _application(tmp_path, "lark-a")
    try:
        # Before the reload the startup snapshot does not know lark-b.
        with pytest.raises(DaemonRequestError) as failure:
            app.handle("adapter.start", {"name": "lark-b"})
        assert failure.value.code == "ADAPTER_NOT_FOUND"

        # `adapter add` only writes files; then the operator reloads.
        _write_json(app.hyprial_home / "channels.json", _channels("lark-a", "lark-b"))
        _write_json(
            app.hyprial_home / "secrets" / "lark-b.json",
            {"appSecret": "secret-lark-b"},
        )
        reloaded = app.handle("adapter.reload", {})
        assert reloaded["ok"] is True
        assert reloaded["added"] == ["lark-b"]

        started = app.handle("adapter.start", {"name": "lark-b"})
        assert started["ok"] is True
        assert started["adapter"]["name"] == "lark-b"
    finally:
        _close_application(app)


def test_reload_never_restarts_a_running_adapter(tmp_path: Path) -> None:
    app = _application(tmp_path, "lark-a")
    try:
        assert app.handle("adapter.start", {"name": "lark-a"})["ok"] is True
        assert app._adapters is not None
        before = app._adapters._processes["lark-a"]

        reloaded = app.handle("adapter.reload", {})

        assert reloaded["removed"] == []
        assert app._adapters._processes["lark-a"] is before
        assert before.running
    finally:
        _close_application(app)


def test_reload_reports_a_corrupted_config_as_fact(tmp_path: Path) -> None:
    app = _application(tmp_path, "lark-a")
    try:
        (app.hyprial_home / "channels.json").write_text("{ not json", encoding="utf-8")

        with pytest.raises(DaemonRequestError) as failure:
            app.handle("adapter.reload", {})

        assert failure.value.code == "ADAPTER_RELOAD_FAILED"
    finally:
        _close_application(app)


def test_detached_worker_stays_visible_and_stoppable_over_ipc(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path, "lark-a")
    try:
        assert app.handle("adapter.start", {"name": "lark-a"})["ok"] is True

        # The config goes away while the worker keeps running.
        _write_json(app.hyprial_home / "channels.json", _channels())
        reloaded = app.handle("adapter.reload", {})
        assert reloaded["removedRunning"] == ["lark-a"]

        # Still visible: status reports the live worker, marked detached.
        status = app.handle("adapter.status", {"name": "lark-a"})
        assert status["adapter"]["status"] == "detached"
        assert status["adapter"]["configured"] is False
        assert status["adapter"]["processRunning"] is True

        # Still stoppable, and the stop is acknowledged rather than erroring.
        stopped = app.handle("adapter.stop", {"name": "lark-a"})
        assert stopped["ok"] is True
        assert stopped["changed"] is True
        assert stopped["adapter"]["configured"] is False
    finally:
        _close_application(app)
