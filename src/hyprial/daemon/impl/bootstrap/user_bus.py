"""Reach the per-user systemd bus from a headless process.

``systemctl --user`` needs a reachable *user* bus. A login session exports
``XDG_RUNTIME_DIR`` and ``DBUS_SESSION_BUS_ADDRESS``; a headless process
(systemd unit, cron job, ssh command without a session) usually exports
neither, and systemctl then fails with::

    Failed to connect to user scope bus via local transport:
    $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined

That is an environment gap, not a broken service: the bus socket normally
already exists at ``/run/user/<uid>/bus``. These helpers classify and repair
the environment without parsing translated supervisor output.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from hyprial.kernel import current_uid

USER_BUS_UNAVAILABLE_REASON = "user-bus-unavailable"
USER_BUS_ADDRESS_UNREACHABLE_REASON = "user-bus-address-unreachable"
UserBusState = Literal["ready", "no-session", "address-unreachable"]


def session_bus_environment(
    base: Mapping[str, str] | None = None,
    *,
    uid: int | None = None,
) -> dict[str, str]:
    """Return an environment that can reach the per-user systemd bus.

    Only fill in what is missing: an explicitly configured bus address is
    always respected, and a runtime directory is only inferred when the
    corresponding bus socket actually exists on disk.
    """

    environment = dict(os.environ if base is None else base)
    if environment.get("DBUS_SESSION_BUS_ADDRESS"):
        return environment
    runtime_dir = environment.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        candidate = Path(f"/run/user/{current_uid() if uid is None else uid}")
        if (candidate / "bus").exists():
            runtime_dir = str(candidate)
            environment["XDG_RUNTIME_DIR"] = runtime_dir
    if runtime_dir and (Path(runtime_dir) / "bus").exists():
        environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    return environment


def user_bus_state(
    base: Mapping[str, str] | None = None,
    *,
    uid: int | None = None,
) -> UserBusState:
    """Classify bus reachability **without reading supervisor output**.

    systemd translates its messages, so matching English stderr text
    classifies correctly only under an English locale.  The probe answers the
    same question from the environment and the filesystem:

    ``ready``                 -- an address is configured and its socket exists
    ``no-session``            -- nothing configured and no per-user socket
    ``address-unreachable``   -- an address is configured but its socket is missing
    """

    environment = dict(os.environ if base is None else base)
    configured = environment.get("DBUS_SESSION_BUS_ADDRESS")
    if configured:
        socket_path = _unix_socket_path(configured)
        if socket_path is None or socket_path.exists():
            return "ready"
        return "address-unreachable"
    runtime_dir = environment.get("XDG_RUNTIME_DIR")
    if runtime_dir and (Path(runtime_dir) / "bus").exists():
        return "ready"
    candidate = Path(f"/run/user/{current_uid() if uid is None else uid}") / "bus"
    return "ready" if candidate.exists() else "no-session"


def _unix_socket_path(address: str) -> Path | None:
    """Return the socket path of a ``unix:path=`` address, else None."""

    for part in address.split(";"):
        if part.startswith("unix:path="):
            return Path(part[len("unix:path=") :])
    return None
