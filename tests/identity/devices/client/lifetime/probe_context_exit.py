"""Observe context teardown in a fresh interpreter and always release the peer."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import time

from tests.identity.devices.client.support import LoopbackAuthority


def _open_fds() -> set[int] | None:
    if os.name != "posix":
        return None
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            entries = os.listdir(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        break
    else:
        raise RuntimeError("POSIX FD snapshot directory is unavailable")
    descriptors = set()
    for entry in entries:
        descriptor = int(entry)
        try:
            os.fstat(descriptor)
        except OSError as error:
            if error.errno == errno.EBADF:
                continue
            raise
        descriptors.add(descriptor)
    return descriptors


def _load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except (AttributeError, NotImplementedError, OSError):
        return None


def observe_context_exit() -> dict[str, object]:
    authority = LoopbackAuthority([], accept_timeout=0.3)
    baseline_fds = _open_fds()
    result: dict[str, object] = {
        "default_timeout": socket.getdefaulttimeout(),
        "fd_oracle": "AVAILABLE" if baseline_fds is not None else "NOT_AVAILABLE",
        "fd_unavailable_reason": (
            None if baseline_fds is not None else "unsupported platform"
        ),
        "baseline_fds": sorted(baseline_fds) if baseline_fds is not None else None,
        "start_load": _load_average(),
    }
    peer = None
    temporary = None
    try:
        with authority:
            temporary = Path(authority._temporary.name)
            peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            peer.settimeout(1.0)
            peer.connect(("127.0.0.1", authority.port))
            deadline = time.monotonic() + 3.0
            while (
                authority.observation.connections == 0
                and authority._thread.is_alive()
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            result["accepted"] = authority.observation.connections
            result["worker_alive_before_exit"] = authority._thread.is_alive()
            exit_started = time.monotonic()
        result["exit_seconds"] = time.monotonic() - exit_started
        result["worker_alive_after_exit"] = authority._thread.is_alive()
        result["listener_closed_after_exit"] = authority._listener.fileno() == -1
        result["temporary_removed_after_exit"] = not temporary.exists()
        result["server_fds_after_exit"] = (
            sorted(_open_fds() - baseline_fds - {peer.fileno()})
            if baseline_fds is not None
            else None
        )
    finally:
        if peer is not None:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
        if authority._thread is not None:
            authority._thread.join(timeout=5.0)
        if authority._listener is not None:
            authority._listener.close()
        if authority._temporary is not None:
            authority._temporary.cleanup()
        result["cleanup"] = {
            "worker_stopped": (
                authority._thread is None or not authority._thread.is_alive()
            ),
            "peer_closed": peer is None or peer.fileno() == -1,
            "listener_closed": (
                authority._listener is None or authority._listener.fileno() == -1
            ),
            "temporary_removed": temporary is None or not temporary.exists(),
        }
        result["fds_restored"] = (
            _open_fds() == baseline_fds if baseline_fds is not None else None
        )
    result["requests"] = len(authority.observation.requests)
    result["end_load"] = _load_average()
    return result


if __name__ == "__main__":
    print(json.dumps(observe_context_exit()))
