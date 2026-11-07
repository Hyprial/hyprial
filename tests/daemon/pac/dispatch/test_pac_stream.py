"""Real signal/pipe primitives for follow shutdown; no actor/launcher stubs."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys

from hyprial.daemon.impl.pac.graphs.events import OutputWatch, follow_lifetime


def test_idle_pipe_eof_is_observed_without_writing_a_heartbeat():
    reader, writer = os.pipe()
    stream = os.fdopen(writer, "w")
    watch = OutputWatch(stream)
    try:
        assert watch.closed() is False
        os.close(reader)
        reader = None
        assert watch.closed() is True
    finally:
        watch.close()
        stream.close()
        if reader is not None:
            os.close(reader)


def test_regular_log_file_is_not_a_closed_consumer(tmp_path):
    with (tmp_path / "stream.log").open("w") as stream:
        watch = OutputWatch(stream)
        try:
            assert watch.closed() is False
        finally:
            watch.close()


def test_follow_overrides_ignored_signals_and_restores_parent_policy(monkeypatch):
    kinds = (signal.SIGINT, signal.SIGTERM)
    original = {kind: signal.getsignal(kind) for kind in kinds}
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        for kind in kinds:
            signal.signal(kind, signal.SIG_IGN)
        with follow_lifetime(True):
            assert all(callable(signal.getsignal(kind)) for kind in kinds)
        assert all(signal.getsignal(kind) == signal.SIG_IGN for kind in kinds)
        with follow_lifetime(False) as watcher:
            assert watcher is None
            assert all(signal.getsignal(kind) == signal.SIG_IGN for kind in kinds)
    finally:
        for kind, handler in original.items():
            signal.signal(kind, handler)


def test_broken_pipe_does_not_fail_again_during_interpreter_flush():
    program = '''
import sys
from hyprial.daemon.impl.pac.graphs.events import silence_broken_pipe
print("ready", flush=True)
sys.stdin.readline()
try:
    print("reader is now closed", flush=True)
except BrokenPipeError:
    silence_broken_pipe()
'''
    with subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        assert child.stdout.readline().strip() == "ready"
        child.stdout.close()
        child.stdin.write("continue\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        assert child.stderr.read() == ""
