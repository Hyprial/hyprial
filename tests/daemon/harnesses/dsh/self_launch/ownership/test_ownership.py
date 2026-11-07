from __future__ import annotations

import os
import signal
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.harnesses.dsh import DshHarnessProcess, dsh_worker_home

from ..support import (
    _install_fake_dsh,
    _pid_alive,
    _ready_or_fail,
    _reap_stub_pids,
    _registered_stub_pids,
    _runtime_for,
    _spec,
    _wait_for,
)


def test_stop_reaps_the_process_group_and_escalates_to_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_DSH_MODE", "ignore-term")
    home = dsh_worker_home(tmp_path / "state", "worker-e")
    process = DshHarnessProcess(_spec(), dsh_home=home)
    try:
        _ready_or_fail(process)
        pid = process.pid
        assert isinstance(pid, int)
        # The child ignores SIGTERM: stop() must escalate to SIGKILL and prove
        # the whole process group is gone.
        started = time.monotonic()
        process.stop()
        elapsed = time.monotonic() - started
        assert elapsed >= 0.2
        assert not _pid_alive(pid)
        assert process.pid is None
    finally:
        if not process._force_stopped_client():
            process.stop()


def test_malformed_turn_timeout_env_fails_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    pid_log = tmp_path / "children.pids"
    monkeypatch.setenv("FAKE_DSH_PID_LOG", str(pid_log))
    monkeypatch.setenv("HYPRIAL_TURN_TIMEOUT_SECONDS", "not-a-number")
    home = dsh_worker_home(tmp_path / "state", "worker-badenv")
    process: DshHarnessProcess | None = None
    try:
        with pytest.raises(ValueError, match="HYPRIAL_TURN_TIMEOUT_SECONDS"):
            process = DshHarnessProcess(_spec("worker-badenv"), dsh_home=home)
    finally:
        if process is not None:
            process.stop()
        assert _reap_stub_pids(_registered_stub_pids(pid_log)) == []


def test_runtime_stop_after_child_sigkill_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime layer: a SIGKILLed DSH child must not make stop() refuse."""

    from hyprial.daemon.impl.api import HarnessDelivery  # noqa: F401  (parity import)

    pid_log = tmp_path / "children.pids"
    monkeypatch.setenv("FAKE_DSH_PID_LOG", str(pid_log))
    runtime = _runtime_for(tmp_path, monkeypatch, name="worker-kill")
    spec = _spec("worker-kill")
    try:
        runtime.start(spec)
        pid = runtime.status()[0]["pid"]
        assert isinstance(pid, int), runtime.status()
        os.kill(pid, signal.SIGKILL)
        assert _wait_for(lambda: not _pid_alive(pid), timeout=10)
        runtime.stop()
        assert not _pid_alive(pid)
    finally:
        remaining = _reap_stub_pids(_registered_stub_pids(pid_log))
        assert remaining == [], remaining


def test_runtime_stop_after_respawn_stops_the_new_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime layer: after an internal respawn, stop() must kill the NEW child."""

    from hyprial.daemon.impl.api import HarnessDelivery

    pid_log = tmp_path / "children.pids"
    prompt_marker = tmp_path / "prompt.marker"
    monkeypatch.setenv("FAKE_DSH_PID_LOG", str(pid_log))
    monkeypatch.setenv("FAKE_DSH_MODE", "hang-turn")
    monkeypatch.setenv("FAKE_DSH_PROMPT_MARKER", str(prompt_marker))
    runtime = _runtime_for(tmp_path, monkeypatch, name="worker-respawn")
    spec = _spec("worker-respawn")
    try:
        runtime.start(spec)
        old_pid = runtime.status()[0]["pid"]
        assert isinstance(old_pid, int), runtime.status()
        delivery = HarnessDelivery(
            delivery_id="respawn-turn",
            conversation_id="respawn-turn",
            sender="agent:test:sender",
            recipient="agent:test:dsh",
            message="hang this turn",
        )
        assert runtime.dispatch("worker-respawn", delivery) is True
        assert _wait_for(prompt_marker.exists, timeout=10), (
            "fake dsh never saw session.prompt"
        )
        os.kill(old_pid, signal.SIGKILL)
        assert _wait_for(
            lambda: any(
                pid != old_pid and _pid_alive(pid)
                for pid in _registered_stub_pids(pid_log)
            ),
            timeout=30,
        ), (
            "no respawn observed within 30s; "
            f"pids={_registered_stub_pids(pid_log)} "
            f"alive={[pid for pid in _registered_stub_pids(pid_log) if _pid_alive(pid)]}"
        )
        new_pid = _registered_stub_pids(pid_log)[-1]
        assert new_pid != old_pid and _pid_alive(new_pid)
        runtime.stop()
        assert _wait_for(lambda: not _pid_alive(new_pid), timeout=10), (
            f"stop() left the respawned child {new_pid} alive"
        )
    finally:
        remaining = _reap_stub_pids(_registered_stub_pids(pid_log))
        assert remaining == [], remaining
