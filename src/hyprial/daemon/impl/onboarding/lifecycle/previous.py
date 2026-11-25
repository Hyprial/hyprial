# Decision record:
# - A serving daemon's ``daemon.json`` has no birth identity, so it must agree
#   with the home heartbeat; that rule is unchanged and never relaxed.
# - A bootstrap-phase marker or record whose recorded birth identity differs
#   from the live process at its pid names a process that no longer exists
#   (pid reuse, typically after a reboot): both records are reaped under the
#   held locks.  A matching identity, a marker without identity and a platform
#   that cannot read identities still refuse; a serving ``daemon.json`` and the
#   heartbeat keep refusing a reused pid.  (KP-079)
# - A bootstrap daemon publishes a ``daemon.json`` with ``phase: "bootstrap"``,
#   its pid and its own ``birthIdentity`` (no heartbeat exists in that phase).
#   Such a marker is accepted only when the OS positively reports that exact
#   birth gone; a live process, a reused pid or a missing/foreign identity
#   still fails closed.  ``bootstrap.json``, when present, must name the same
#   process.  Once proven dead, the marker and the bootstrap record are
#   removed together so the next admission starts from a clean home.
"""Inspect existing lifecycle records and reap only provably dead bootstrap ones.

Heartbeat freshness is not proof of process death. The normal daemon marker
has no birth identity, so it must agree with the existing home heartbeat.
Readers run under identity and state locks and never inspect credentials.
"""

import json
import os
from pathlib import Path
import stat

from ..processes import require_previous_process_exit
from ..records import BootstrapRecord


def _read_object(path: Path) -> dict | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("previous daemon record is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("previous daemon record changed during read")
        text = stream.read(65537)
    if len(text) > 65536:
        raise ValueError("previous daemon record exceeds size bound")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("previous daemon record is not an object")
    return value


def require_previous_instances_exited(home: Path, state_dir: Path, socket_path: Path) -> None:
    heartbeat = _read_object(home / ".active_daemon")
    normal = _read_object(state_dir / "daemon.json")
    bootstrap = BootstrapRecord.read(state_dir)
    if heartbeat is not None:
        require_previous_process_exit(heartbeat.get("pid"), heartbeat.get("processIdentity"))
    marker_is_bootstrap = normal is not None and normal.get("phase") == "bootstrap"
    if normal is not None and marker_is_bootstrap:
        _require_bootstrap_marker_exited(normal, bootstrap, socket_path)
    elif normal is not None:
        if (type(normal.get("schemaVersion")) is not int or normal["schemaVersion"] != 1
                or type(normal.get("pid")) is not int or normal["pid"] <= 0
                or normal.get("socket") != str(socket_path)):
            raise ValueError("previous normal daemon record is invalid or differently bound")
        if heartbeat is None or normal["pid"] != heartbeat.get("pid"):
            raise RuntimeError("previous daemon birth identity cannot be verified")
    if bootstrap is not None:
        if bootstrap.socket != str(socket_path):
            raise ValueError("previous bootstrap socket binding does not match")
        require_previous_process_exit(
            bootstrap.pid, bootstrap.birth_identity, reuse_proves_exit=True
        )
        if not bootstrap.remove_if_owned(state_dir):
            raise RuntimeError("previous bootstrap record changed during admission")
    if marker_is_bootstrap:
        (state_dir / "daemon.json").unlink(missing_ok=True)


def _require_bootstrap_marker_exited(
    marker: dict, bootstrap: BootstrapRecord | None, socket_path: Path
) -> None:
    pid = marker.get("pid")
    birth = marker.get("birthIdentity")
    if (type(marker.get("schemaVersion")) is not int or marker["schemaVersion"] != 1
            or type(pid) is not int or pid <= 0
            or marker.get("socket") != str(socket_path)):
        raise ValueError("previous bootstrap marker is invalid or differently bound")
    if not isinstance(birth, str) or not birth.strip():
        raise RuntimeError("previous bootstrap marker has no birth identity")
    if bootstrap is not None and (bootstrap.pid, bootstrap.birth_identity) != (pid, birth):
        raise RuntimeError("previous bootstrap marker disagrees with the bootstrap record")
    require_previous_process_exit(pid, birth, reuse_proves_exit=True)
