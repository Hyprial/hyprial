"""``serve`` with the daemon driver in process: arguments, refusals and a dead watcher.

Decision: host-serves-daemon-in-process-20261119 (2822, B3).  ``serve`` runs in
this process against the rebind suite's scripted daemon.  Refusals happen before
the ready line (exit 2 for arguments and configuration, 3 for the daemon, one JSON
object on stderr); a watcher that dies stops the real uvicorn server through
``should_exit`` and the host exits 3 after it printed its stopped line.  The only
timer is the test-only bound that ends a hung run with the host's own SIGINT path.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.shell.impl.gui.host import __main__ as host_main
from hyprial.shell.impl.gui.host import app as app_module
from hyprial.shell.impl.gui.host import local as local_module
from hyprial.shell.impl.gui.host.app import HostApp
from hyprial.shell.impl.gui.host.local import LocalDaemon
from hyprial.shell.impl.request_http.bootstrap import gui as bootstrap_gui
from tests.shell.gui.host.local.support import NODE, world
from tests.shell.gui.host.support import GRANT, UPSTREAM_TOKEN
from tests.shell.request_http.rebind.support import OWNER, write_home

RUN_BOUND_SECONDS = 60.0


def assets(tmp_path: Path) -> Path:
    root = tmp_path / "gui"
    root.mkdir(exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>Hyprial</title>")
    return root


def arguments(tmp_path: Path, *extra: str) -> list[str]:
    home = tmp_path / "home"
    return [
        "serve", "--port", "0", "--root", str(assets(tmp_path)),
        "--home", str(home), "--state-dir", str(home / "state"), "--socket", str(home / "state" / "daemon.sock"),
        "--node", NODE, *extra,
    ]


@pytest.fixture(autouse=True)
def launcher_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("HYPRIAL_GUI_", "HYPRIAL_BACKEND")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HYPRIAL_GUI_BACKEND_DRIVER", "daemon")
    monkeypatch.setenv("HYPRIAL_GUI_LAUNCH_GRANT", GRANT)


def last_report(capfd: Any) -> tuple[str, dict[str, Any]]:
    captured = capfd.readouterr()
    return captured.out, json.loads(captured.err.strip().splitlines()[-1])


def test_a_partial_set_of_daemon_arguments_is_refused(tmp_path, capfd) -> None:
    argv = ["serve", "--port", "0", "--root", str(assets(tmp_path)), "--home", str(tmp_path / "home")]
    assert host_main.serve(argv) == 2
    out, report = last_report(capfd)
    assert report["code"] == "INVALID_ARGUMENT" and "HYPRIAL_GUI_READY" not in out
    argv = ["serve", "--port", "0", "--root", str(assets(tmp_path)), "--node", NODE]
    assert host_main.serve(argv) == 2


@pytest.mark.parametrize("name", ["HYPRIAL_BACKEND_ENDPOINT", "HYPRIAL_GUI_UPSTREAM_TOKEN"])
def test_an_upstream_variable_is_refused_for_the_in_process_driver(tmp_path, monkeypatch, capfd, name) -> None:
    world(tmp_path, monkeypatch, phase="serving")
    monkeypatch.setenv(name, "http://127.0.0.1:9" if name.endswith("ENDPOINT") else UPSTREAM_TOKEN)
    assert host_main.serve(arguments(tmp_path)) == 2
    out, report = last_report(capfd)
    assert report["code"] == "INVALID_CONFIG" and name in report["message"]
    assert UPSTREAM_TOKEN not in json.dumps(report) and "HYPRIAL_GUI_READY" not in out


def test_the_contract_mock_driver_cannot_be_served_in_process(tmp_path, monkeypatch, capfd) -> None:
    monkeypatch.setenv("HYPRIAL_GUI_BACKEND_DRIVER", "contract-mock")
    assert host_main.serve(arguments(tmp_path)) == 2
    assert last_report(capfd)[1]["code"] == "INVALID_CONFIG"


def test_no_daemon_is_exit_three_before_any_ready_line(tmp_path, monkeypatch, capfd) -> None:
    daemon = world(tmp_path, monkeypatch, phase="serving")
    daemon.up = False
    assert host_main.serve(arguments(tmp_path)) == 3
    out, report = last_report(capfd)
    assert report["code"] == "BACKEND_UNAVAILABLE" and report["ok"] is False
    assert "HYPRIAL_GUI_READY" not in out and "HYPRIAL_GUI_STOPPED" not in out


def test_a_watcher_that_dies_ends_the_host_with_exit_three(tmp_path, monkeypatch, capfd) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    attach = bootstrap_gui.new_host

    def new_host(config: Any, **extra: Any) -> Any:
        if extra.get("attach_only"):
            raise RuntimeError("reattach blew up")
        return attach(config, **extra)

    original = HostApp.startup

    async def startup(self: HostApp) -> None:
        await original(self)
        write_home(tmp_path, owner=OWNER)
        daemon.become_serving()

    monkeypatch.setattr(bootstrap_gui, "new_host", new_host)
    monkeypatch.setattr(HostApp, "startup", startup)
    bound_fired = threading.Event()

    def end_hung_run() -> None:
        bound_fired.set()
        signal.raise_signal(signal.SIGINT)

    bound = threading.Timer(RUN_BOUND_SECONDS, end_hung_run)
    bound.start()
    started = time.monotonic()
    try:
        code = host_main.serve(arguments(tmp_path))
    finally:
        bound.cancel()
    elapsed = time.monotonic() - started
    assert not bound_fired.is_set(), "the host was ended by the test bound, not by the failure"
    assert elapsed < RUN_BOUND_SECONDS / 2
    captured = capfd.readouterr()
    reports = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    assert code == 3, captured.err
    assert captured.out.startswith("HYPRIAL_GUI_READY ") and "HYPRIAL_GUI_STOPPED" in captured.out
    names = [r.get("event") or r.get("code") for r in reports]
    assert "watcher_failed" in names and names[-1] == "GUI_WATCHER_FAILED"
    assert names.index("watcher_failed") < names.index("stopped")


def test_an_exception_after_the_daemon_attach_still_closes_the_local_daemon(tmp_path, monkeypatch) -> None:
    world(tmp_path, monkeypatch, phase="serving")
    closed: list[LocalDaemon] = []
    close = LocalDaemon.close

    def counting_close(self: LocalDaemon) -> None:
        closed.append(self)
        close(self)

    def broken_build_app(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("build_app blew up")

    monkeypatch.setattr(LocalDaemon, "close", counting_close)
    monkeypatch.setattr(app_module, "build_app", broken_build_app)
    with pytest.raises(RuntimeError, match="build_app blew up"):
        host_main.serve(arguments(tmp_path))
    assert len(closed) == 1 and closed[0].watcher_alive() is False


def test_a_watcher_that_failed_before_the_start_prints_no_ready_line(tmp_path, monkeypatch, capfd) -> None:
    world(tmp_path, monkeypatch, phase="serving")

    def dying_watcher(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("watcher died at once")

    stop_on_failure = LocalDaemon.stop_on_failure

    def after_the_failure(self: LocalDaemon, callback: Any) -> None:
        assert self.wait_for_failure(RUN_BOUND_SECONDS)
        stop_on_failure(self, callback)

    monkeypatch.setattr(local_module, "watch_principal", dying_watcher)
    monkeypatch.setattr(LocalDaemon, "stop_on_failure", after_the_failure)
    bound_fired = threading.Event()

    def end_hung_run() -> None:
        bound_fired.set()
        signal.raise_signal(signal.SIGINT)

    bound = threading.Timer(RUN_BOUND_SECONDS, end_hung_run)
    bound.start()
    try:
        code = host_main.serve(arguments(tmp_path))
    finally:
        bound.cancel()
    captured = capfd.readouterr()
    assert not bound_fired.is_set()
    assert code == 3 and "HYPRIAL_GUI_READY" not in captured.out
    assert "HYPRIAL_GUI_STOPPED" in captured.out
    assert "watcher_failed" in captured.err
