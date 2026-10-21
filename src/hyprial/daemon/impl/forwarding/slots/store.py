"""Slot paths, endpoints and the persisted peer→slot assignment.

The zenoh connect set is fixed when the session opens: N local slot
endpoints owned by the Tailcat sidecar (stable-zenoh-session-slots design
§2).  Everything here is derivable from the hyprial home and the pool
size alone -- deliberately, so the daemon, the sidecar and a restarted
incarnation of either all name the same sockets without talking first.

The assignment (peer label → slot index) is persisted under the state
dir: a daemon or sidecar restart rebinds every peer to the slot it held
before, so the connect set does not change across restarts either (§5).
The pool only ever grows -- a slot index, once handed out for a path, must
never be reused for a *different* path -- and growth is the one remaining
event that changes the connect set (the ``reconfigure_connect`` fallback).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from hyprial.kernel import atomic_json_write
from hyprial.daemon.impl.bootstrap.discovery import (  # noqa: F401  (re-exported)
    TCP_SLOT_GROW_SPEC,
    slot_locator,
)

#: Default pool size (§8: enough for the current org; growth is the
#: alarmed rebuild fallback).
DEFAULT_SLOT_POOL_SIZE = 32

#: Overrides the initial pool size; growth persisted in the store wins
#: when larger, so a restart never shrinks a grown pool back under its
#: live assignments.
SLOT_POOL_ENV = "HYPRIAL_ZENOH_SLOT_POOL"

#: The persisted assignment, beside the other tailcat state files.
SLOTS_STATE_RELPATH = "state/tailcat/slots.json"

#: Slot sockets live in one directory derived from the home, with short
#: fixed names: both ends derive ``slot-NN`` from the index alone, which
#: is what lets a grown pool bind lazily (a ``map-peer`` naming a slot
#: beyond the ``up`` list implies the same directory and naming rule).
SLOTS_DIR_RELPATH = "state/tailcat/slots"

#: Which local transport the slots use.  zenoh 1.9.0 has no
#: unixsock-stream implementation on Windows (the link crate is cfg'd to
#: ``target_family = "unix"``; notes/windows-unixsock-2026-10-08.md §1),
#: so Windows slots are loopback TCP listeners with kernel-assigned
#: ports, reported by the sidecar at ``up``.  The env override exists so
#: the TCP path can be exercised on any platform.
SLOT_TRANSPORT_ENV = "HYPRIAL_ZENOH_SLOT_TRANSPORT"

_STATE_VERSION = 1


def slot_transport() -> str:
    """``unix`` or ``tcp``: the slot listener transport on this platform.

    Windows has no zenoh unixsock-stream dialer, so it must use TCP;
    everywhere else unix sockets stay the default (stable paths, no port
    accounting).  ``HYPRIAL_ZENOH_SLOT_TRANSPORT`` overrides for tests
    and for E2E-ing the TCP path off-Windows; an unknown value fails
    loud.
    """

    raw = os.environ.get(SLOT_TRANSPORT_ENV)
    if raw is None or not raw.strip():
        return "tcp" if sys.platform == "win32" else "unix"
    lowered = raw.strip().lower()
    if lowered in {"unix", "tcp"}:
        return lowered
    raise ValueError(f"{SLOT_TRANSPORT_ENV} must be unix or tcp; got {raw!r}")


def slot_specs(home: Path, pool_size: int) -> tuple[str, ...]:
    """The ``up`` request's ``slots`` value for this platform's transport.

    Unix: one stable socket path per slot (both ends derive them without
    talking).  TCP: one grow spec per slot -- the kernel assigns every
    port and the sidecar reports the resolved ``tcp:127.0.0.1:<port>``
    ids in its ``state`` events, in request order.
    """

    if slot_transport() == "tcp":
        return (TCP_SLOT_GROW_SPEC,) * pool_size
    return slot_socket_paths(home, pool_size)


def slot_pool_size(default: int = DEFAULT_SLOT_POOL_SIZE) -> int:
    """The configured initial pool size; invalid values fail loud."""

    raw = os.environ.get(SLOT_POOL_ENV)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{SLOT_POOL_ENV} must be a positive integer; got {raw!r}")
    return value


def slots_dir(home: Path) -> Path:
    """Where the slot sockets live under ``home``."""

    return Path(home) / SLOTS_DIR_RELPATH


def ensure_slots_dir(home: Path) -> Path:
    """Create the slot socket directory; idempotent, present-tolerant.

    Nothing else owns this directory's lifetime: on the unix transport the
    daemon must have created it before the sidecar binds the ``up`` slots
    (a fresh home ships without it -- the e2e 2026-10-08 finding), and the
    sidecar's own listen-time ``MkdirAll`` is the second line of defense,
    not a substitute.  0700 is the state-dir convention under the hyprial
    home (``atomic_json_write`` parents, squire addressing, login homes).
    """

    directory = slots_dir(home)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def slot_socket_path(home: Path, index: int) -> Path:
    """The socket path for one slot; stable across restarts by construction."""

    return slots_dir(home) / f"slot-{index:02d}"


def slot_endpoint(home: Path, index: int) -> str:
    """The zenoh locator dialing one slot."""

    return f"unixsock-stream/{slot_socket_path(home, index)}"


def slot_endpoints(home: Path, pool_size: int) -> tuple[str, ...]:
    """The fixed connect set for a pool of ``pool_size`` slots."""

    return tuple(slot_endpoint(home, index) for index in range(pool_size))


def slot_socket_paths(home: Path, pool_size: int) -> tuple[str, ...]:
    """The ``up`` request's ``slots`` value: one path per slot, in order."""

    return tuple(str(slot_socket_path(home, index)) for index in range(pool_size))


class SlotAssignments:
    """The persisted peer→slot map and the pool size, loaded once per daemon.

    Keys are the peer labels the forwarding pipeline already uses
    (``owner/deviceId``, the ``directory_peers`` keys).  Every mutation is
    written through atomically; an absent or unreadable file starts empty
    (the worst case is a rebind, which is a link drop the catch-up layers
    already cover -- design §6).
    """

    def __init__(self, home: Path, *, base_size: int | None = None) -> None:
        self._home = Path(home)
        self._path = self._home / SLOTS_STATE_RELPATH
        self._base = base_size if base_size is not None else slot_pool_size()
        self._pool_size = self._base
        self._assignments: dict[str, int] = {}
        self._tcp_ports: dict[int, int] = {}
        self._load()

    def _load(self) -> None:
        try:
            record = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(record, dict) or record.get("v") != _STATE_VERSION:
            return
        pool = record.get("poolSize")
        raw = record.get("assignments")
        raw_tcp = record.get("tcpPorts")
        if not isinstance(pool, int) or not isinstance(raw, dict):
            return
        assignments: dict[str, int] = {}
        taken: set[int] = set()
        for label, slot in raw.items():
            if (
                isinstance(label, str)
                and isinstance(slot, int)
                and 0 <= slot
                and slot not in taken
            ):
                assignments[label] = slot
                taken.add(slot)
        tcp_ports: dict[int, int] = {}
        if isinstance(raw_tcp, dict):
            for raw_key, value in raw_tcp.items():
                try:
                    key = int(raw_key)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, int) and 0 < value <= 65535 and key >= 0:
                    tcp_ports[key] = value
        # A grown pool stays grown; persisted ports also keep their index
        # covered so a restart can name the same fixed TCP connect set.
        self._pool_size = max(
            self._base,
            pool,
            *(slot + 1 for slot in taken),
            *(index + 1 for index in tcp_ports),
            0,
        )
        self._assignments = assignments
        self._tcp_ports = tcp_ports

    def _save(self) -> None:
        atomic_json_write(
            self._path,
            {
                "v": _STATE_VERSION,
                "poolSize": self._pool_size,
                "assignments": dict(sorted(self._assignments.items())),
                "tcpPorts": dict(sorted(self._tcp_ports.items())),
            },
        )

    @property
    def pool_size(self) -> int:
        return self._pool_size

    @property
    def assignments(self) -> dict[str, int]:
        """A copy; mutate only through assign/release."""

        return dict(self._assignments)

    def endpoints(self) -> tuple[str, ...]:
        """The statically-derivable connect set at the CURRENT pool size.

        Unix transport only: TCP slot ports are kernel-assigned per
        sidecar incarnation, so there is nothing derivable here -- the
        live backend answers from the sidecar-reported ids instead, and
        this fallback is empty.
        """

        if slot_transport() == "tcp":
            return ()
        return slot_endpoints(self._home, self._pool_size)

    def socket_paths(self) -> tuple[str, ...]:
        return slot_socket_paths(self._home, self._pool_size)

    def specs(self) -> tuple[str, ...]:
        """The current ``up.slots`` value, including persisted TCP ports."""

        if slot_transport() != "tcp":
            return slot_specs(self._home, self._pool_size)
        return tuple(
            f"tcp:127.0.0.1:{self._tcp_ports[index]}"
            if index in self._tcp_ports
            else TCP_SLOT_GROW_SPEC
            for index in range(self._pool_size)
        )

    def record_tcp_ports(
        self,
        reported: tuple[str, ...],
        *,
        requested: tuple[str, ...] | None = None,
    ) -> list[tuple[int, str, str]]:
        """Persist ports and report explicit requests that fell back."""

        if slot_transport() != "tcp":
            return []
        mismatches: list[tuple[int, str, str]] = []
        if requested is not None:
            for index, wanted in enumerate(requested):
                if wanted.startswith("tcp:") and not wanted.endswith(":0"):
                    if index < len(reported) and reported[index] != wanted:
                        mismatches.append((index, wanted, reported[index]))
        changed = False
        for index, slot_id in enumerate(reported):
            if not slot_id.startswith("tcp:127.0.0.1:"):
                continue
            try:
                port = int(slot_id.rpartition(":")[2])
            except ValueError:
                continue
            if not 0 < port <= 65535:
                continue
            if self._tcp_ports.get(index) != port:
                self._tcp_ports[index] = port
                changed = True
        if changed:
            self._save()
        return mismatches

    def slot_for(self, label: str) -> int | None:
        return self._assignments.get(label)

    def assign(self, label: str) -> tuple[int, bool]:
        """``(slot, grown)``: reuse the persisted slot, else the lowest free one.

        When every slot is taken the pool grows by one base-size step --
        one growth per exhaustion event, so the one rebuild it costs
        covers a whole wave of joins (§5: the alarmed
        ``reconfigure_connect`` fallback).
        """

        existing = self._assignments.get(label)
        if existing is not None:
            return existing, False
        taken = set(self._assignments.values())
        free = next(
            (index for index in range(self._pool_size) if index not in taken),
            None,
        )
        grown = free is None
        if free is None:
            free = self._pool_size
            self._pool_size += self._base
        self._assignments[label] = free
        self._save()
        return free, grown

    def release(self, label: str) -> int | None:
        """Free the peer's slot (after ``unmap-peer``); None when unknown."""

        slot = self._assignments.pop(label, None)
        if slot is not None:
            self._save()
        return slot
