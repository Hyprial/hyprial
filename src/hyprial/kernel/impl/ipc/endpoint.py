"""Pure daemon IPC endpoint selection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path


def daemon_socket_path(
    state_dir: Callable[[], Path], *, env: Mapping[str, str]
) -> Path:
    """Return the daemon socket selected by the supplied environment.

    ``state_dir`` is a **callable**, not a value: an authoritative
    ``HARNESS_SOCKET_PATH`` must be honoured without touching the state root
    at all (the base implementation never evaluated it on that path, and
    callers may deliberately make it unavailable).
    """

    if "HARNESS_STATE_DIR" in env or "HYPRIAL_HOME" in env:
        return state_dir() / "daemon.sock"
    configured = env.get("HARNESS_SOCKET_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return state_dir() / "daemon.sock"
