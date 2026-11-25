"""Backend selection: injection works without AF_UNIX and always restores."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root

from hyprial.kernel import (
    connect_local_ipc,
    listen_local_ipc,
    local_ipc_path_limit,
)
from hyprial.kernel.impl.manysys.ipc import active_local_ipc_backend
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend
from hyprial.kernel.impl.manysys.ipc import posix as local_ipc_posix

from .memory_backend import MemoryPipeBackend
from .test_contract import close_pair, connect_pair, read_frame

_HAS_AF_UNIX = hasattr(socket, "AF_UNIX")


@pytest.fixture
def ipc_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="hyprial-lipc-sel-", dir=hyprial_test_tmp_root()))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_listen_works_without_af_unix_through_an_injected_backend(
    ipc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not _HAS_AF_UNIX:
        pytest.skip("this platform has no AF_UNIX to remove")
    monkeypatch.delattr(socket, "AF_UNIX")
    with local_ipc_backend(MemoryPipeBackend()):
        pair = connect_pair(ipc_root / "daemon.sock")
        try:
            pair.client.sendall(b'{"version":1,"id":"no-af-unix"}\n')
            assert read_frame(pair.stream)["id"] == "no-af-unix"
        finally:
            pair.stream.close()
            pair.client.close()
            pair.listener.close()


@pytest.mark.skipif(os.name == "nt", reason="the default AF_UNIX backend is POSIX only")
def test_injection_is_restored_after_the_context_exits(ipc_root: Path) -> None:
    with local_ipc_backend(MemoryPipeBackend()):
        listener = listen_local_ipc(ipc_root / "injected.sock")
        listener.close()
        assert not stat.S_ISSOCK((ipc_root / "injected.sock").stat().st_mode)
    listener = listen_local_ipc(ipc_root / "default.sock")
    try:
        assert stat.S_ISSOCK((ipc_root / "default.sock").stat().st_mode)
    finally:
        listener.close()


@pytest.mark.skipif(os.name == "nt", reason="the default AF_UNIX backend is POSIX only")
def test_the_default_backend_is_the_unix_socket(ipc_root: Path) -> None:
    listener = listen_local_ipc(ipc_root / "default.sock")
    try:
        assert stat.S_ISSOCK((ipc_root / "default.sock").stat().st_mode)
    finally:
        listener.close()


def test_the_injected_backend_owns_the_path_limit() -> None:
    with local_ipc_backend(MemoryPipeBackend()):
        assert local_ipc_path_limit() is None


@pytest.mark.skipif(os.name == "nt", reason="the AF_UNIX path limit is POSIX only")
def test_the_default_backend_reports_the_af_unix_path_limit() -> None:
    limit = local_ipc_path_limit()
    assert isinstance(limit, int)
    assert limit >= 100


def test_connect_reaches_the_injected_listener_only(ipc_root: Path) -> None:
    backend = MemoryPipeBackend()
    with local_ipc_backend(backend):
        listener = listen_local_ipc(ipc_root / "daemon.sock")
        try:
            client = connect_local_ipc(ipc_root / "daemon.sock", 1.0)
            client.close()
        finally:
            listener.close()
        with pytest.raises(OSError):
            connect_local_ipc(ipc_root / "daemon.sock", 1.0)


@pytest.mark.skipif(os.name == "nt", reason="the AF_UNIX backend is POSIX only")
def test_the_backend_hardens_the_endpoint_between_bind_and_listen(
    ipc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint hardening belongs to the backend, in the daemon's old order.

    The pre-seam server did bind, chmod 0600, listen; the entry point must not
    add a chmod after listen, and nothing may follow the listen.
    """

    events: list[str] = []
    real_socket = socket.socket

    class RecordingSocket(real_socket):
        def bind(self, address) -> None:  # type: ignore[no-untyped-def]
            events.append("bind")
            super().bind(address)

        def listen(self, *args: object, **kwargs: object) -> None:
            events.append("listen")
            super().listen(*args, **kwargs)  # type: ignore[arg-type]

    real_chmod = os.chmod

    def recording_chmod(path: object, mode: int) -> None:
        events.append(f"chmod:{oct(mode)}")
        real_chmod(path, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(
        local_ipc_posix,
        "socket",
        SimpleNamespace(
            socket=RecordingSocket,
            AF_UNIX=socket.AF_UNIX,
            SOCK_STREAM=socket.SOCK_STREAM,
        ),
    )
    monkeypatch.setattr(local_ipc_posix, "os", SimpleNamespace(chmod=recording_chmod))

    listener = listen_local_ipc(ipc_root / "order.sock")
    try:
        assert events == ["bind", "chmod:0o600", "listen"]
    finally:
        listener.close()


@pytest.mark.skipif(os.name == "nt", reason="the AF_UNIX peer credential is POSIX only")
def test_accept_reports_an_unreadable_uid_instead_of_raising(
    ipc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable peer credential is a recorded field, never an accept error.

    The daemon server never read uids before the seam and its accept loop must
    stay error-free here; the bootstrap refusal-on-unreadable rule is the
    caller's decision (the later wire.py slice), taken from this field.
    """

    def refusing(_connection: object) -> int | None:
        raise OSError("peer credential is unreadable")

    monkeypatch.setattr(local_ipc_posix, "read_peer_uid", refusing)
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        assert pair.peer.pid == os.getpid()
        assert pair.peer.uid is None
        assert pair.peer.uid_unreadable is True
        assert pair.peer.same_user_enforced is False
        pair.client.sendall(b'{"version":1,"id":"uid-test"}\n')
        assert read_frame(pair.stream)["id"] == "uid-test"
    finally:
        close_pair(pair)


def test_a_second_different_injection_is_rejected_while_one_is_active() -> None:
    first, second = MemoryPipeBackend(), MemoryPipeBackend()
    with local_ipc_backend(first):
        with pytest.raises(RuntimeError, match="local IPC backend"):
            with local_ipc_backend(second):
                pass
        assert active_local_ipc_backend() is first


def test_forcing_the_default_is_rejected_while_an_injection_is_active() -> None:
    with local_ipc_backend(MemoryPipeBackend()):
        with pytest.raises(RuntimeError, match="local IPC backend"):
            with local_ipc_backend(None):
                pass


def test_the_same_backend_may_nest_and_exits_in_order() -> None:
    backend = MemoryPipeBackend()
    with local_ipc_backend(backend):
        with local_ipc_backend(backend):
            assert active_local_ipc_backend() is backend
        assert active_local_ipc_backend() is backend
    with local_ipc_backend(MemoryPipeBackend()):
        assert active_local_ipc_backend() is not backend


def test_an_exited_injection_does_not_leak_into_a_later_one() -> None:
    leaked = MemoryPipeBackend()
    with local_ipc_backend(leaked):
        pass
    with local_ipc_backend(MemoryPipeBackend()):
        assert active_local_ipc_backend() is not leaked


def test_a_concurrent_different_injection_is_rejected_not_interleaved() -> None:
    """One thread holds an injection; a challenger thread is refused outright.

    The selection is process-global on purpose (a daemon or host thread must
    see a main-thread injection), so the guard is mutual exclusion between
    different backends rather than thread isolation.
    """

    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []
    held = MemoryPipeBackend()

    def hold() -> None:
        with local_ipc_backend(held):
            entered.set()
            outcomes.append("held")
            assert release.wait(2.0)
        outcomes.append("released")

    def challenge() -> None:
        assert entered.wait(2.0)
        try:
            with local_ipc_backend(MemoryPipeBackend()):
                outcomes.append("intruder-held")
        except RuntimeError:
            outcomes.append("rejected")

    holder = threading.Thread(target=hold)
    challenger = threading.Thread(target=challenge)
    holder.start()
    challenger.start()
    try:
        challenger.join(2.0)
        assert not challenger.is_alive()
    finally:
        release.set()
        holder.join(2.0)
    assert "intruder-held" not in outcomes
    assert "rejected" in outcomes
    assert outcomes[-1] == "released"
    with local_ipc_backend(MemoryPipeBackend()):
        pass


def test_an_injection_is_served_to_threads_that_listen_or_connect() -> None:
    with local_ipc_backend(MemoryPipeBackend()):
        seen: dict[str, object] = {}

        def consume() -> None:
            seen["limit"] = local_ipc_path_limit()

        worker = threading.Thread(target=consume)
        worker.start()
        worker.join(2.0)
        assert not worker.is_alive()
        assert seen["limit"] is None
