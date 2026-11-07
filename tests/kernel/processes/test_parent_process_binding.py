"""The public child-side binding fences parent PID reuse and can be released."""

import os
import subprocess
import sys
import threading

import pytest

from hyprial import kernel


@pytest.mark.skipif(os.name == "nt", reason="native POSIX parent identity probe")
@pytest.mark.parametrize("fault", ["pid", "birth", "probe", "callback", "blocked-callback"])
def test_parent_binding_fails_closed_and_exits_after_cleanup(tmp_path, fault):
    assert callable(kernel.bind_current_process_to_parent)
    marker = tmp_path / "parent-lost"
    script = """
import os, sys, time
from pathlib import Path
from hyprial.kernel import bind_current_process_to_parent
from hyprial.kernel.impl.processes import owned_process
pid, identity, fault, marker = int(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
if fault == 'pid':
    pid += 1000000
elif fault == 'birth':
    identity = 'reused-birth'
elif fault == 'probe':
    def broken(_pid):
        raise OSError('probe unavailable')
    owned_process.process_birth_identity = broken
def cleanup():
    marker.write_text('parent-lost')
    if fault == 'callback':
        raise RuntimeError('cleanup failed')
    if fault == 'blocked-callback':
        time.sleep(30)
if fault in {'callback', 'blocked-callback'}:
    identity = 'reused-birth'
bind_current_process_to_parent(pid, identity, before_exit=cleanup)
time.sleep(30)
"""
    result = subprocess.run([sys.executable, "-c", script, str(os.getpid()),
                             kernel.process_birth_identity(os.getpid()), fault, str(marker)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "parent-lost"


def test_parent_binding_release_is_idempotent():
    from hyprial.kernel.impl.processes import owned_process

    assert kernel.bind_current_process_to_parent is owned_process.bind_current_process_to_parent
    assert "bind_current_process_to_parent" in kernel.__all__
    if os.name == "nt":
        identity = kernel.windows_process_identity(os.getppid())
    else:
        identity = kernel.process_birth_identity(os.getppid())
    release = kernel.bind_current_process_to_parent(os.getppid(), identity)
    release()
    release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX identity probe injection")
def test_parent_binding_release_reports_a_probe_that_has_not_stopped(monkeypatch):
    from hyprial.kernel.impl.processes import owned_process

    entered, unblock = threading.Event(), threading.Event()

    def blocked_probe(_pid):
        entered.set()
        unblock.wait(5)
        return "held-birth"

    monkeypatch.setattr(owned_process, "process_birth_identity", blocked_probe)
    release = kernel.bind_current_process_to_parent(os.getppid(), "held-birth")
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="binding cleanup is unconfirmed"):
            release()
    finally:
        unblock.set()
        release()


@pytest.mark.parametrize("pid,identity", [(0, "birth"), (False, "birth"), (1, ""), (1, None)])
def test_parent_binding_rejects_missing_identity(pid, identity):
    with pytest.raises(ValueError):
        kernel.bind_current_process_to_parent(pid, identity)
