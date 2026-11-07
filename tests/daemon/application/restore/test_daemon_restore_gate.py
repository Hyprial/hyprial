"""The restore gate: the accept loop runs ahead of restore, ping answers throughout.

Before this, ``_serve`` ran only after both restore halves completed, so every
IPC method went unanswered while restore was in flight -- the 0.5s `ps`
readiness probes parked in the listen backlog until they timed out, which is
the mechanism behind the 86-second silent window measured on 2026-08-31.
Restore now runs on its own thread; the daemon serves the light set
(``ping``/``shutdown``) immediately and refuses everything else with
``DAEMON_RESTORING`` instead of queueing it.

These tests use a real socket and the real serve loop.  The state root lives
under /tmp because AF_UNIX paths are length-limited and pytest's tmp_path on
this host exceeds it.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.support.runtime._daemon_diagnostics import thread_stack_locations, write
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (


    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


class _FakeHarnesses:
    def status(self) -> tuple[()]:
        return ()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _InboxEvents:
    def publish(self, _event: object) -> None:
        pass


class _IdleRuntime:
    def on_timer(self, _observed_at_ms: int | None = None) -> Any:
        return type(
            "Outcome",
            (),
            {
                "harness_restarts": 0,
                "inbox_results": 0,
                "inbox_pruned": 0,
                "inbox_pruned_items": (),
            },
        )()

    def stop(self) -> None:
        pass


def _ipc_call(
    socket_path: Path,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {
                    "version": 1,
                    "id": "probe",
                    "method": method,
                    "params": params or {},
                }
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


@pytest.fixture
def state_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="hyprial-gate-", dir=hyprial_test_tmp_root()))
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _BlockedRestore:
    """A restore that holds until released, and answers stop_event itself.

    The poll on ``stop_event`` is the same contract the real restore loops
    carry: a shutdown landing mid-restore must not wait out the fleet.
    """

    def __init__(self, app: DaemonApplication) -> None:
        self._app = app
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        while not self.release.is_set() and not self._app.stop_event.is_set():
            time.sleep(0.01)
        self.finished.set()


def _running_app(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> tuple[DaemonApplication, _BlockedRestore, threading.Thread]:
    """A daemon on a thread whose adapter restore is blocked until released."""

    state = root / "state"
    state.mkdir(parents=True)
    home = root / "home"
    home.mkdir(mode=0o700)
    (state / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="restore-gate-test",
        hyprial_home=home,
        **_app_injections(),
    )
    database = root / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _InboxEvents(),
    )
    app._inbox_coordinator = coordinator
    app._inbox = DeliveryCustodyFacade(coordinator, database)
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    monkeypatch.setattr(
        app,
        "_start_runtime",
        lambda: setattr(app, "_runtime", _IdleRuntime()),
    )
    blocked = _BlockedRestore(app)
    monkeypatch.setattr(app, "_restore_adapters", blocked)
    daemon = threading.Thread(target=app.run, daemon=True)
    daemon.start()
    assert blocked.started.wait(2.0), "restore never started"
    return app, blocked, daemon


def _stop(
    app: DaemonApplication, daemon: threading.Thread, blocked: _BlockedRestore
) -> None:
    blocked.release.set()
    app.stop_event.set()
    started = time.monotonic()
    daemon.join(timeout=10.0)
    if daemon.is_alive():
        write(
            "daemon-restore-gate-stop",
            {
                "kind": "thread-stop-timeout",
                "elapsedSeconds": time.monotonic() - started,
                "threadStack": thread_stack_locations(daemon),
            },
        )
    assert not daemon.is_alive(), "daemon thread outlived its stop"


def test_ping_answers_during_restore(
    monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    app, blocked, daemon = _running_app(monkeypatch, state_root)
    try:
        started = time.monotonic()
        response = _ipc_call(app.socket_path, "ping", timeout=0.5)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, f"ping queued behind restore for {elapsed:.2f}s"
        result = response["result"]
        assert result["running"] is True
        assert result["pid"] == os.getpid()
        assert result["nodeId"] == "restore-gate-test"
        assert result["phase"] == "restoring"
        assert result["restorePending"] is True
        assert not blocked.finished.is_set()

        # The gate opens when restore completes, and the probe says so.
        blocked.release.set()
        assert app._restore_done.wait(2.0), "gate never opened after restore"
        result = _ipc_call(app.socket_path, "ping")["result"]
        assert result["phase"] == "serving"
        assert result["restorePending"] is False
    finally:
        _stop(app, daemon, blocked)


def test_heavy_methods_rejected_while_restoring(
    monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    app, blocked, daemon = _running_app(monkeypatch, state_root)
    try:
        for method in ("ps", "top.snapshot", "message.send", "agent.list"):
            started = time.monotonic()
            response = _ipc_call(app.socket_path, method, timeout=0.5)
            elapsed = time.monotonic() - started
            assert elapsed < 0.5, f"{method} queued behind restore"
            error = response.get("error")
            assert error is not None, f"{method} was served during restore"
            assert error["code"] == ipc_errors.DAEMON_RESTORING

        # Once the gate opens the same methods are served normally.
        blocked.release.set()
        assert app._restore_done.wait(2.0), "gate never opened after restore"
        response = _ipc_call(app.socket_path, "ps")
        assert response["result"]["daemon"]["running"] is True
    finally:
        _stop(app, daemon, blocked)


def test_daemon_json_published_before_restore_completes(
    monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    """daemon.json means "serving" now, not "restored": it must exist -- with
    this process's pid -- while restore is still blocked."""

    app, blocked, daemon = _running_app(monkeypatch, state_root)
    try:
        marker = json.loads((app.state_dir / "daemon.json").read_text(encoding="utf-8"))
        assert marker["pid"] == os.getpid()
        assert marker["nodeId"] == "restore-gate-test"
        assert not blocked.finished.is_set()
    finally:
        _stop(app, daemon, blocked)


def test_shutdown_is_served_while_restore_is_blocked(
    monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    """A wedged restore must not make the daemon unstoppable over IPC.

    ``shutdown`` is in the light set; the restore loop polls ``stop_event``,
    so the whole daemon leaves promptly without the release ever coming.
    """

    app, blocked, daemon = _running_app(monkeypatch, state_root)
    response = _ipc_call(app.socket_path, "shutdown", timeout=0.5)
    assert response["result"]["stopping"] is True

    daemon.join(timeout=10.0)
    assert not daemon.is_alive(), "restore thread kept the daemon alive"
    assert blocked.finished.is_set(), "restore never observed stop_event"
    assert not app._restore_done.is_set(), "gate opened for a stopped daemon"


def test_restore_failure_still_fails_the_daemon(
    monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    """A restore step that escapes its own isolation aborts startup, as it
    did when restore ran inline -- now through the stop path."""

    state = state_root / "state"
    state.mkdir(parents=True)
    home = state_root / "home"
    home.mkdir(mode=0o700)
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="restore-gate-test",
        hyprial_home=home,
        **_app_injections(),
    )
    monkeypatch.setattr(
        app,
        "_start_runtime",
        lambda: setattr(app, "_runtime", _IdleRuntime()),
    )

    def collapsing_restore() -> None:
        raise RuntimeError("synthetic adapter restore collapse")

    monkeypatch.setattr(app, "_restore_adapters", collapsing_restore)

    with pytest.raises(RuntimeError, match="synthetic adapter restore collapse"):
        app.run()
