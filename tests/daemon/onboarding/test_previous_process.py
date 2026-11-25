"""Bootstrap never treats missing process observations as proof of exit."""

import os
import subprocess
import sys
import time

import pytest

from hyprial.daemon.impl.onboarding import processes


@pytest.mark.parametrize("pid,birth", [(0, "x"), (-1, "x"), (True, "x"), (1, "")])
def test_invalid_record_is_not_proof_of_exit(pid, birth):
    with pytest.raises(ValueError):
        processes.require_previous_process_exit(pid, birth)


def test_permission_failure_is_not_dead(monkeypatch):
    def denied(_pid):
        raise PermissionError("probe denied")
    monkeypatch.setattr(processes, "probe_process", denied)
    with pytest.raises(RuntimeError, match="unverified"):
        processes.require_previous_process_exit(100, "original-birth")


@pytest.mark.parametrize("observed", [None, "original-birth", "reused-birth"])
def test_existing_pid_is_refused_even_with_unknown_or_changed_birth(monkeypatch, observed):
    monkeypatch.setattr(processes, "probe_process", lambda _pid: None)
    monkeypatch.setattr(processes, "process_birth_identity", lambda _pid: observed)
    with pytest.raises(RuntimeError):
        processes.require_previous_process_exit(100, "original-birth")


@pytest.mark.skipif(os.name == "nt", reason="POSIX child exit observation")
def test_real_child_must_exit_before_admission(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        env={"HOME": str(tmp_path)},
    )
    try:
        birth = processes.process_birth_identity(child.pid)
        assert birth is not None
        with pytest.raises(RuntimeError):
            processes.require_previous_process_exit(child.pid, birth)
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
        processes.require_previous_process_exit(child.pid, birth)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_reuse_proves_exit_only_when_the_caller_declares_it(monkeypatch):
    monkeypatch.setattr(processes, "probe_process", lambda _pid: None)
    monkeypatch.setattr(processes, "process_birth_identity", lambda _pid: "reused-birth")
    processes.require_previous_process_exit(100, "original-birth", reuse_proves_exit=True)
    with pytest.raises(RuntimeError, match="reused"):
        processes.require_previous_process_exit(100, "original-birth")


@pytest.mark.parametrize("observed,match", [("original-birth", "not exited"), (None, "unverified")])
def test_reuse_proof_never_covers_a_matching_or_unreadable_identity(monkeypatch, observed, match):
    monkeypatch.setattr(processes, "probe_process", lambda _pid: None)
    monkeypatch.setattr(processes, "process_birth_identity", lambda _pid: observed)
    with pytest.raises(RuntimeError, match=match):
        processes.require_previous_process_exit(100, "original-birth", reuse_proves_exit=True)


def test_reuse_proof_does_not_cover_a_probe_error(monkeypatch):
    def denied(_pid):
        raise PermissionError("probe denied")

    monkeypatch.setattr(processes, "probe_process", denied)
    with pytest.raises(RuntimeError, match="unverified"):
        processes.require_previous_process_exit(100, "original-birth", reuse_proves_exit=True)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="exited-state observation is read from /proc")
def test_exited_but_unreaped_process_counts_as_exited(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        env={"HOME": str(tmp_path)},
    )
    try:
        birth = processes.process_birth_identity(child.pid)
        assert birth is not None
        child.kill()
        deadline = time.monotonic() + 10
        while not processes.process_exited_unreaped(child.pid, birth) and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(child.pid, 0)
        assert processes.process_birth_identity(child.pid) == birth
        processes.require_previous_process_exit(child.pid, birth)
    finally:
        child.wait(timeout=10)


def test_exited_state_of_another_birth_is_not_proof(monkeypatch):
    monkeypatch.setattr(processes, "probe_process", lambda _pid: None)
    monkeypatch.setattr(processes, "process_birth_identity", lambda _pid: "original-birth")
    monkeypatch.setattr(processes, "process_exited_unreaped", lambda _pid, _birth: False)
    with pytest.raises(RuntimeError, match="has not exited"):
        processes.require_previous_process_exit(100, "original-birth")
