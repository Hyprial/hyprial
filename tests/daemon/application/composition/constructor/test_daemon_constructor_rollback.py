from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl import application as application_module


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


def test_constructor_failure_drains_created_actor_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = {thread.ident for thread in threading.enumerate()}

    def fail_home_guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected home guard failure")

    monkeypatch.setattr(application_module, "ActiveDaemonHeartbeat", fail_home_guard)
    with pytest.raises(RuntimeError, match="injected home guard failure"):
        application_module.DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            node_id="rollback-test",
            owner="owner",
        **_app_injections(),
        )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        new_threads = [
            thread for thread in threading.enumerate()
            if thread.ident not in before
            and (
                "actor" in thread.name.lower()
                or "effect" in thread.name.lower()
                or "writer" in thread.name.lower()
            )
        ]
        if not new_threads:
            break
        time.sleep(0.01)
    assert not new_threads, [thread.name for thread in new_threads]
