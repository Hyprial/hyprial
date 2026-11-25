"""In-process autoupdate scheduling and accept-loop isolation."""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


from hyprial.daemon.impl.autoupdate import (
    AUTOUPDATE_CHILD_ENV,
    InProcessAutoUpdateScheduler,
    next_scheduled_run,
)
from hyprial.daemon.impl.autoupdate.actor import AutoUpdateAuthority
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.runtime import ReconcileSummary
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections



def _home_with_auto_upgrade(tmp_path: Path, name: str = "home") -> Path:
    """A scheduler home with guard 1 explicitly opted in (spec r1: the
    autoUpgrade switch defaults OFF, so a scheduling-mechanics test must
    write the settings.json opt-in itself)."""

    home = tmp_path / name
    home.mkdir(mode=0o700)
    (home / "settings.json").write_text(
        json.dumps({"autoUpgrade": True}) + "\n", encoding="utf-8"
    )
    return home


def test_next_run_preserves_0317_and_1517_local_schedule() -> None:
    local = timezone(timedelta(hours=8))

    assert next_scheduled_run(datetime(2026, 8, 24, 3, 16, 59, tzinfo=local)) == (
        datetime(2026, 8, 24, 3, 17, tzinfo=local)
    )
    assert next_scheduled_run(datetime(2026, 8, 24, 3, 17, tzinfo=local)) == (
        datetime(2026, 8, 24, 15, 17, tzinfo=local)
    )
    assert next_scheduled_run(datetime(2026, 8, 24, 15, 17, tzinfo=local)) == (
        datetime(2026, 8, 25, 3, 17, tzinfo=local)
    )


def test_scheduler_runs_off_thread_and_preserves_daemon_path(
    monkeypatch: Any, tmp_path: Path
) -> None:
    inherited_path = "/restricted/bin:/opt/test-codex-bin"
    monkeypatch.setenv("PATH", inherited_path)
    called = threading.Event()
    observation: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observation.update(
            command=command,
            kwargs=kwargs,
            thread=threading.current_thread().name,
        )
        called.set()
        return subprocess.CompletedProcess(command, 0, '{"ok":true}\n', "")

    scheduler = InProcessAutoUpdateScheduler(
        state_dir=tmp_path,
        runner=runner,
        # Scheduling mechanics only: explicit autoUpgrade opt-in so the
        # default-off guard (spec r1) cannot silently skip the run under
        # test.
        hyprial_home=_home_with_auto_upgrade(tmp_path),
    )
    scheduler.start()
    try:
        assert scheduler.trigger("test") is True
        assert called.wait(2.0), "scheduled update never ran"
        deadline = time.monotonic() + 2.0
        while scheduler.status()["active"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert observation["thread"] == "hyprial-autoupdate"
    env = observation["kwargs"]["env"]
    assert env["PATH"] == inherited_path
    assert env[AUTOUPDATE_CHILD_ENV] == "1"
    assert observation["command"][-4:] == ["daemon", "upgrade", "run", "--json"]


def test_scheduler_fires_after_sleep_crosses_calendar_point(tmp_path: Path) -> None:
    """A wake/clock jump across 03:17 must catch up instead of skipping it."""

    clock = [datetime(2026, 8, 24, 3, 16, 59)]
    called = threading.Event()

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        called.set()
        return subprocess.CompletedProcess(command, 0, "{}\n", "")

    scheduler = InProcessAutoUpdateScheduler(
        state_dir=tmp_path,
        runner=runner,
        now=lambda: clock[0],
        calendar_recheck_seconds=0.01,
        hyprial_home=_home_with_auto_upgrade(tmp_path),
    )
    scheduler.start()
    try:
        deadline = time.monotonic() + 1.0
        while scheduler.status()["nextRunAt"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        clock[0] = datetime(2026, 8, 24, 3, 20)
        assert called.wait(1.0), "missed calendar point was not caught up"
        assert scheduler.status()["lastTrigger"] == "schedule"
    finally:
        scheduler.stop()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    def status(self) -> tuple[()]:
        return ()


class _FastRuntime:
    def poll(self) -> ReconcileSummary:
        return ReconcileSummary(0, 0)

    def stop(self) -> None:
        pass


def _request(socket_path: Path, method: str) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {"version": 1, "id": method, "method": method, "params": {}}
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


def test_never_finishing_update_does_not_block_accept() -> None:
    """Mutation: running the update on the accept flow makes every ps red."""

    release = threading.Event()
    entered = threading.Event()

    def hung_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        entered.set()
        release.wait()
        return subprocess.CompletedProcess(command, 0, "{}", "")

    root = Path(tempfile.mkdtemp(prefix="hyprial-au-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    state.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="autoupdate-accept-test",
        hyprial_home=root / "home",
        **_app_injections(),
    )
    app._inbox = InboxService(state / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._runtime = _FastRuntime()  # type: ignore[assignment]
    scheduler = AutoUpdateAuthority(
        state_dir=state,
        runner=hung_runner,
        hyprial_home=_home_with_auto_upgrade(root),
        actor_host=app._agent_session_domains.actor_hosts.host_for("autoupdate"),
        scheduler=app._maintenance_scheduler,
    )
    app._autoupdate = scheduler
    scheduler.start()
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        triggered = _request(app.socket_path, "autoupdate.trigger")
        assert triggered["result"]["triggered"] is True
        assert entered.wait(1.0), "hung update never started"
        for _ in range(5):
            started = time.monotonic()
            response = _request(app.socket_path, "ps")
            assert response["result"]["daemon"]["running"] is True
            assert time.monotonic() - started < 1.0
    finally:
        release.set()
        assert scheduler.stop(timeout=2.0)
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        shutil.rmtree(root, ignore_errors=True)
