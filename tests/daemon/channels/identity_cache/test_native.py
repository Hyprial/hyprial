"""Real Darwin process probes are restricted to children created by this test."""

import json
import os
import select
import signal
import subprocess
import sys
import threading

import pytest

from hyprial.daemon.impl.mcp.channel import ownership

from .support import reset_cache

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='actual Darwin libproc/ps probe; portable synthetic controls run separately')


def test_real_owned_births_keep_full_markers_without_repeated_ps(tmp_path, monkeypatch):
    reset_cache()
    children = []
    try:
        for _ in range(8):
            children.append(subprocess.Popen(
                [sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=tmp_path,
            ))
        owned = {process.pid for process in children}
        real_ps = ownership._PROCESS_POPEN
        real_native = ownership._read_darwin_process_start
        calls = []

        def native(pid):
            assert pid in owned
            return real_native(pid)

        def ps(argv, **kwargs):
            pid = int(argv[-1])
            assert pid in owned
            calls.append(pid)
            return real_ps(argv, **kwargs)

        monkeypatch.setattr(ownership, '_read_darwin_process_start', native)
        monkeypatch.setattr(ownership, '_PROCESS_POPEN', ps)
        markers = {}
        for process in children:
            assert process.poll() is None
            marker = ownership._read_process_identity(process.pid)
            assert marker is not None and 'darwin-starttime:' in marker and ';ps-lstart:' in marker
            markers[process.pid] = marker
        path = tmp_path / 'persisted-fences.json'
        path.write_text(json.dumps(markers))
        persisted = json.loads(path.read_text())
        for _ in range(3):
            for process in children:
                assert process.poll() is None
                marker = persisted[str(process.pid)]
                assert ownership._read_process_identity(process.pid) == marker
                assert ownership._owner_process_status(process.pid, marker) is ownership._OwnerProcessStatus.ALIVE
        assert len(calls) == len(children)
    finally:
        for process in children:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reset_cache()


def test_fork_does_not_inherit_a_locked_identity_cache():
    reset_cache()
    lock = getattr(ownership, '_DARWIN_IDENTITY_CACHE_LOCK', threading.RLock())
    entered, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(2)
    read_fd, write_fd = os.pipe()
    child = None
    try:
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                identity = ownership._read_process_identity(os.getpid())
                os.write(write_fd, b'valid' if identity else b'unreadable')
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        write_fd = -1
        ready, _, _ = select.select([read_fd], [], [], 5)
        assert ready, 'forked owned process could not acquire its identity cache'
        assert os.read(read_fd, 32) == b'valid'
    finally:
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if child:
            waited, _ = os.waitpid(child, os.WNOHANG)
            if waited == 0:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
        reset_cache()
