# Decision record (test_stdin_stop_windows_branches, KP-136 slice 2026-11-24):
# - Pins every branch of StdinStopWatcher._read_windows_chunk with fake
#   kernel32/msvcrt so the Windows behaviour is fixed by tests on any host:
#   console FILE_TYPE_CHAR (kbhit polling, console loss, Ctrl-Z line, NUL),
#   FILE_TYPE_PIPE handled and unlisted PeekNamedPipe errors, and unknown file
#   types that reach the generic select tail.
# - The fake select raises OSError(WSAENOTSOCK) because Windows select accepts
#   only sockets; that is what makes unlisted pipe errors and unknown file
#   types end as EOF after one fallback slice (pinned, not endorsed; see the
#   KP-136 report).
# - Console loss is modelled as GetConsoleMode starting to fail on the stdin
#   handle; before the KP-136 fix the kbhit loop never re-checked the handle
#   and could not stop.
# - deliver_main_termination without pthread_kill trips the Python handler only
#   at the next bytecode boundary, so a blocking call on the main thread delays
#   delivery until it returns; a SIG_DFL disposition makes it a no-op.
"""Branch tests for the Windows side of ``StdinStopWatcher``."""

from __future__ import annotations

import signal
import threading
import time
import types
from collections.abc import Callable

import pytest

from hyprial.kernel import StdinStopWatcher, deliver_main_termination
from hyprial.kernel.impl.manysys.signals import stdin_stop, termination

_WSAENOTSOCK = 10038


def _windows_select(*_args: object) -> tuple[list[int], list[int], list[int]]:
    error = OSError(
        _WSAENOTSOCK, "An operation was attempted on something that is not a socket"
    )
    raise error


def _install_windows(
    monkeypatch: pytest.MonkeyPatch,
    kernel32: object,
    *,
    read: Callable[[int, int], bytes],
    kbhit: Callable[[], bool] | None = None,
) -> list[str]:
    selects: list[str] = []

    def recording_select(*args: object) -> tuple[list[int], list[int], list[int]]:
        selects.append("select")
        return _windows_select(*args)

    msvcrt = types.SimpleNamespace(get_osfhandle=lambda _fd: 200)
    if kbhit is not None:
        msvcrt.kbhit = kbhit
    monkeypatch.setattr(stdin_stop, "os", types.SimpleNamespace(name="nt", read=read))
    monkeypatch.setattr(stdin_stop, "_msvcrt", msvcrt)
    monkeypatch.setattr(stdin_stop, "_load_windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(
        stdin_stop, "select", types.SimpleNamespace(select=recording_select)
    )
    return selects


def _no_read(*_args: object) -> bytes:
    pytest.fail("os.read must not run on this branch")


def _console_mode(results: list[bool]) -> Callable[[object, object], int]:
    calls = iter(results)

    def get_console_mode(_handle: object, _mode: object) -> int:
        return int(next(calls, results[-1]))

    return get_console_mode


def _run(watcher: StdinStopWatcher, fired: threading.Event, wait: float) -> bool:
    watcher.start()
    try:
        return fired.wait(timeout=wait)
    finally:
        watcher.close()
        watcher.join(timeout=1.0)


def test_console_kbhit_loop_keeps_waiting_and_close_wakes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = threading.Event()

    def kbhit() -> bool:
        polls.set()
        return False

    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: 0x0002, GetConsoleMode=_console_mode([True])
    )
    _install_windows(monkeypatch, kernel32, read=_no_read, kbhit=kbhit)
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    watcher.start()
    assert polls.wait(timeout=1.0)
    assert not fired.wait(timeout=0.4)
    started = time.monotonic()
    watcher.close()
    watcher.join(timeout=1.0)
    assert watcher._thread is not None and not watcher._thread.is_alive()
    assert time.monotonic() - started < 0.5
    assert not watcher.pending.is_set()


def test_console_loss_is_observed_as_eof_in_kbhit_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: 0x0002,
        GetConsoleMode=_console_mode([True, True, False]),
    )
    _install_windows(monkeypatch, kernel32, read=_no_read, kbhit=lambda: False)
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 2.0)
    assert watcher.pending.is_set()


@pytest.mark.parametrize(
    ("payload", "stops"),
    [(b"", True), (b"shutdown\r\n", True), (b"\x1a\r\n", False), (b"noise\r\n", False)],
)
def test_console_line_after_kbhit(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, stops: bool
) -> None:
    reads: list[int] = []

    def read(_fd: int, size: int) -> bytes:
        reads.append(size)
        return payload

    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: 0x0002, GetConsoleMode=_console_mode([True])
    )
    _install_windows(monkeypatch, kernel32, read=read, kbhit=lambda: len(reads) == 0)
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 0.6 if not stops else 2.0) is stops
    assert reads[0] == 4096


def test_char_device_that_is_not_a_console_reads_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: 0x0002, GetConsoleMode=_console_mode([False])
    )
    _install_windows(monkeypatch, kernel32, read=lambda _fd, _n: b"")
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 1.0)


@pytest.mark.parametrize("winerror", [6, 232])
def test_pipe_listed_errors_stop_without_select(
    monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: 0x0003,
        PeekNamedPipe=lambda *_args: 0,
        GetLastError=lambda: winerror,
    )
    monkeypatch.delattr(stdin_stop.ctypes, "get_last_error", raising=False)
    selects = _install_windows(monkeypatch, kernel32, read=_no_read)
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 1.0)
    assert selects == []


@pytest.mark.parametrize("winerror", [1, 5, 230, 536, 995])
def test_pipe_unlisted_errors_fall_through_to_select_and_stop(
    monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: 0x0003,
        PeekNamedPipe=lambda *_args: 0,
        GetLastError=lambda: winerror,
    )
    monkeypatch.delattr(stdin_stop.ctypes, "get_last_error", raising=False)
    selects = _install_windows(monkeypatch, kernel32, read=_no_read)
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 1.0)
    assert selects == ["select"]


@pytest.mark.parametrize("file_type", [0x0000, 0x8000])
def test_unknown_file_types_stop_via_select_tail(
    monkeypatch: pytest.MonkeyPatch, file_type: int
) -> None:
    kernel32 = types.SimpleNamespace(GetFileType=lambda _h: file_type)
    selects = _install_windows(monkeypatch, kernel32, read=_no_read)
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 1.0)
    assert selects == ["select"]


@pytest.mark.parametrize("handle", [-1, 0])
def test_invalid_osfhandle_is_eof(monkeypatch: pytest.MonkeyPatch, handle: int) -> None:
    kernel32 = types.SimpleNamespace(
        GetFileType=lambda _h: pytest.fail("no type probe")
    )
    _install_windows(monkeypatch, kernel32, read=_no_read)
    monkeypatch.setattr(
        stdin_stop, "_msvcrt", types.SimpleNamespace(get_osfhandle=lambda _fd: handle)
    )
    fired = threading.Event()
    watcher = StdinStopWatcher(0, on_stop=fired.set)
    assert _run(watcher, fired, 1.0)


def test_interrupt_main_runs_handler_only_after_blocking_call_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        termination, "signal", types.SimpleNamespace(SIGTERM=signal.SIGTERM)
    )
    handled_at: list[float] = []
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_a: handled_at.append(time.monotonic()))
    try:
        delivered = threading.Event()

        def deliver() -> None:
            time.sleep(0.05)
            deliver_main_termination(signal.SIGTERM)
            delivered.set()

        worker = threading.Thread(target=deliver, daemon=True)
        started = time.monotonic()
        worker.start()
        time.sleep(0.6)
        worker.join(timeout=1.0)
        assert delivered.is_set()
        assert len(handled_at) == 1
        assert handled_at[0] - started >= 0.55
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_interrupt_main_is_a_no_op_while_sigterm_is_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        termination, "signal", types.SimpleNamespace(SIGTERM=signal.SIGTERM)
    )
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        deliver_main_termination(signal.SIGTERM)
        time.sleep(0.05)
    finally:
        signal.signal(signal.SIGTERM, previous)
