# Decision record (test_signals_and_stdin_stop, W1-MANYSYS 2026-11-21):
# - Exercises deliver_main_termination on both pthread_kill and Windows
#   _thread.interrupt_main(signum) paths, plus StdinStopWatcher across EOF, LF,
#   CRLF, chunked lines, blocking POSIX os.pipe() close/join wakeups, POSIX
#   select fallback, Windows PeekNamedPipe (avail == 0 close, avail > 0 read,
#   ERROR_BROKEN_PIPE / ERROR_PIPE_NOT_CONNECTED EOF), reentrant close(), and
#   concurrent close() while a slow on_stop() callback is executing outside the
#   internal lock.
"""Tests for ``deliver_main_termination`` and ``StdinStopWatcher``."""

from __future__ import annotations

import os
import signal
import threading
import time
import types

import pytest

from hyprial.kernel import StdinStopWatcher, deliver_main_termination
from hyprial.kernel.impl.manysys.signals import stdin_stop, termination


def test_deliver_main_termination_uses_pthread_kill_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        termination,
        "signal",
        types.SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            pthread_kill=lambda ident, sig: calls.append((ident, sig)),
        ),
    )
    monkeypatch.setattr(
        termination,
        "_thread",
        types.SimpleNamespace(
            interrupt_main=lambda *_args: pytest.fail(
                "interrupt_main should not run when pthread_kill exists"
            )
        ),
    )

    deliver_main_termination()
    assert calls == [(threading.main_thread().ident, int(signal.SIGTERM))]


def test_deliver_main_termination_falls_back_to_interrupt_main_without_pthread_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        termination, "signal", types.SimpleNamespace(SIGTERM=signal.SIGTERM)
    )
    received: list[int] = []
    done = threading.Event()
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum: int, _frame: object) -> None:
        received.append(signum)
        done.set()

    signal.signal(signal.SIGTERM, handler)
    try:
        worker = threading.Thread(
            target=lambda: deliver_main_termination(signal.SIGTERM), daemon=True
        )
        worker.start()
        worker.join(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not done.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received == [int(signal.SIGTERM)]
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize(
    "chunks",
    [
        (),
        (b"noise\nshutdown\n",),
        (b"noise\r\nshutdown\r\n",),
        (b"partial-", b"line\r\nshut", b"down\r\ntrailing"),
    ],
)
def test_stdin_stop_watcher_triggers_on_eof_lf_and_crlf(
    chunks: tuple[bytes, ...],
) -> None:
    read_fd, write_fd = os.pipe()
    triggered = threading.Event()
    watcher = StdinStopWatcher(read_fd, on_stop=triggered.set)
    watcher.start()
    try:
        if not chunks:
            os.close(write_fd)
            write_fd = -1
        else:
            for piece in chunks:
                os.write(write_fd, piece)
        assert triggered.wait(timeout=2.0)
        assert watcher.pending.is_set()
        watcher.join(timeout=2.0)
        assert watcher._thread is not None and not watcher._thread.is_alive()
    finally:
        watcher.close()
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


def test_stdin_stop_watcher_close_wakes_blocking_pipe_in_subsecond_without_firing() -> None:
    read_fd, write_fd = os.pipe()
    assert os.get_blocking(read_fd) is True
    fired = threading.Event()
    watcher = StdinStopWatcher(read_fd, on_stop=fired.set)
    watcher.start()
    try:
        time.sleep(0.05)
        assert watcher._thread is not None and watcher._thread.is_alive()
        started = time.monotonic()
        watcher.close()
        watcher.join(timeout=1.0)
        elapsed = time.monotonic() - started
        assert not watcher._thread.is_alive()
        assert elapsed < 0.5
        assert not fired.is_set()
        assert not watcher.pending.is_set()
        os.fstat(read_fd)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_stdin_stop_watcher_close_seals_nonblocking_descriptor_without_firing() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    fired = threading.Event()
    watcher = StdinStopWatcher(read_fd, on_stop=fired.set)
    watcher.start()
    try:
        time.sleep(0.05)
        watcher.close()
        watcher.join(timeout=1.0)
        assert watcher._thread is not None and not watcher._thread.is_alive()
        assert not fired.is_set()
        assert not watcher.pending.is_set()
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_stdin_stop_watcher_slow_callback_does_not_block_concurrent_close() -> None:
    read_fd, write_fd = os.pipe()
    entered_callback = threading.Event()
    release_callback = threading.Event()

    def slow_stop() -> None:
        entered_callback.set()
        release_callback.wait(timeout=2.0)

    watcher = StdinStopWatcher(read_fd, on_stop=slow_stop)
    watcher.start()
    try:
        os.write(write_fd, b"shutdown\n")
        assert entered_callback.wait(timeout=2.0)
        started = time.monotonic()
        watcher.close()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        release_callback.set()
        watcher.join(timeout=1.0)
        assert watcher._thread is not None and not watcher._thread.is_alive()
        assert watcher.pending.is_set()
    finally:
        release_callback.set()
        os.close(write_fd)
        os.close(read_fd)


def test_stdin_stop_watcher_posix_select_error_fallback_and_reentrant_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    stopped = threading.Event()

    def fake_read(_fd: int, _size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise BlockingIOError
        return b"shutdown\r\n"

    def failing_select(*_args: object) -> tuple[list[int], list[int], list[int]]:
        raise OSError("select rejected special descriptor")

    monkeypatch.setattr(
        stdin_stop,
        "os",
        types.SimpleNamespace(
            name="posix",
            read=fake_read,
            get_blocking=lambda _fd: False,
            set_blocking=lambda _fd, _flag: None,
        ),
    )
    monkeypatch.setattr(
        stdin_stop, "select", types.SimpleNamespace(select=failing_select)
    )

    watcher: StdinStopWatcher | None = None

    def reentrant_stop() -> None:
        assert watcher is not None
        watcher.close()
        stopped.set()

    watcher = StdinStopWatcher(0, on_stop=reentrant_stop)
    watcher.start()
    assert stopped.wait(timeout=2.0)
    assert watcher.pending.is_set()
    watcher.join(timeout=1.0)
    assert watcher._thread is not None and not watcher._thread.is_alive()


def test_windows_peek_named_pipe_zero_avail_wakes_immediately_on_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fired = threading.Event()
    peek_calls = threading.Event()

    def fake_peek(
        _handle: int,
        _buf: object,
        _size: int,
        _read: object,
        avail_ptr: object,
        _left: object,
    ) -> int:
        avail_ptr._obj.value = 0  # type: ignore[attr-defined]
        peek_calls.set()
        return 1

    fake_kernel32 = types.SimpleNamespace(
        GetFileType=lambda _handle: 0x0003,
        PeekNamedPipe=fake_peek,
        GetLastError=lambda: 0,
    )
    monkeypatch.setattr(
        stdin_stop,
        "os",
        types.SimpleNamespace(
            name="nt",
            read=lambda *_args: pytest.fail("os.read must not run when avail == 0"),
        ),
    )
    monkeypatch.setattr(
        stdin_stop,
        "_msvcrt",
        types.SimpleNamespace(get_osfhandle=lambda _fd: 100),
    )
    monkeypatch.setattr(stdin_stop, "_load_windows_kernel32", lambda: fake_kernel32)

    watcher = StdinStopWatcher(0, on_stop=fired.set)
    watcher.start()
    assert peek_calls.wait(timeout=1.0)
    started = time.monotonic()
    watcher.close()
    watcher.join(timeout=1.0)
    elapsed = time.monotonic() - started
    assert watcher._thread is not None and not watcher._thread.is_alive()
    assert elapsed < 0.5
    assert not fired.is_set()
    assert not watcher.pending.is_set()


def test_windows_peek_named_pipe_reads_available_shutdown_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fired = threading.Event()
    payload = b"noise\r\nshutdown\r\n"
    read_sizes: list[int] = []

    def fake_peek(
        _handle: int,
        _buf: object,
        _size: int,
        _read: object,
        avail_ptr: object,
        _left: object,
    ) -> int:
        avail_ptr._obj.value = len(payload)  # type: ignore[attr-defined]
        return 1

    def fake_read(_fd: int, count: int) -> bytes:
        read_sizes.append(count)
        return payload[:count]

    fake_kernel32 = types.SimpleNamespace(
        GetFileType=lambda _handle: 0x0003,
        PeekNamedPipe=fake_peek,
        GetLastError=lambda: 0,
    )
    monkeypatch.setattr(
        stdin_stop, "os", types.SimpleNamespace(name="nt", read=fake_read)
    )
    monkeypatch.setattr(
        stdin_stop,
        "_msvcrt",
        types.SimpleNamespace(get_osfhandle=lambda _fd: 101),
    )
    monkeypatch.setattr(stdin_stop, "_load_windows_kernel32", lambda: fake_kernel32)

    watcher = StdinStopWatcher(0, on_stop=fired.set)
    watcher.start()
    assert fired.wait(timeout=1.0)
    assert watcher.pending.is_set()
    assert read_sizes == [len(payload)]
    watcher.join(timeout=1.0)
    assert watcher._thread is not None and not watcher._thread.is_alive()


@pytest.mark.parametrize("winerror", [109, 233])
def test_windows_peek_named_pipe_broken_pipe_triggers_eof_immediately(
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    fired = threading.Event()
    fake_kernel32 = types.SimpleNamespace(
        GetFileType=lambda _handle: 0x0003,
        PeekNamedPipe=lambda *_args: 0,
        GetLastError=lambda: winerror,
    )
    monkeypatch.setattr(
        stdin_stop,
        "os",
        types.SimpleNamespace(
            name="nt",
            read=lambda *_args: pytest.fail(
                "os.read must not run on broken Windows pipe"
            ),
        ),
    )
    monkeypatch.setattr(
        stdin_stop,
        "_msvcrt",
        types.SimpleNamespace(get_osfhandle=lambda _fd: 102),
    )
    monkeypatch.delattr(stdin_stop.ctypes, "WinDLL", raising=False)
    monkeypatch.delattr(stdin_stop.ctypes, "get_last_error", raising=False)
    monkeypatch.setattr(
        stdin_stop.ctypes,
        "windll",
        types.SimpleNamespace(kernel32=fake_kernel32),
        raising=False,
    )

    watcher = StdinStopWatcher(0, on_stop=fired.set)
    watcher.start()
    assert fired.wait(timeout=1.0)
    assert watcher.pending.is_set()
    watcher.join(timeout=1.0)
    assert watcher._thread is not None and not watcher._thread.is_alive()
