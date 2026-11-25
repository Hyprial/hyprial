# Decision record (W2B-1 & R3, 2026-11-21):
# - Delegates OS parent PID lookup, Darwin libproc start-time probing,
#   platform-scoped birth marker reading, and process existence probing to
#   hyprial.kernel (manysys), removing all platform branches and /proc or
#   libproc path literals from ownership.py while preserving exact 2c706e2850
#   marker emission (`proc-starttime`, `darwin-starttime`/`darwin-startsec`/
#   `ps-lstart`, `win-filetime`), Darwin cache semantics, and legacy scheme
#   alias normalization (`linux`, `proc`, `darwin`, `win32`) on read/compare.
# - Birth marker parsing is owned by manysys.parse_process_birth_marker.
# - Resolves register_at_fork with getattr before invoking it so Windows
#   Pyright analysis does not report an unavailable os attribute.
"""Owner-process identity and liveness probes."""
from __future__ import annotations

import logging
import os
import subprocess
import sys  # noqa: F401
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hyprial.daemon.impl.mcp.api import DaemonDisconnected
from hyprial.kernel import (
    darwin_process_start_time,
    manysys,
    probe_process,
    process_parent_pid,
    scoped_process_birth_marker,
)

_logger = logging.getLogger(__name__)

_PROCESS_POPEN = subprocess.Popen


@dataclass(frozen=True, slots=True)
class _DarwinIdentityProbe:
    owner_thread: int
    done: threading.Event


_DARWIN_IDENTITY_CACHE_LIMIT = 512
_DARWIN_IDENTITY_CACHE: OrderedDict[int, tuple[tuple[int, int], str]] = OrderedDict()
_DARWIN_IDENTITY_CACHE_LOCK = threading.RLock()
_DARWIN_IDENTITY_INFLIGHT: dict[int, _DarwinIdentityProbe] = {}


def _reset_darwin_identity_cache() -> None:
    """A forked child must not inherit locks held by vanished parent threads."""

    global _DARWIN_IDENTITY_CACHE, _DARWIN_IDENTITY_CACHE_LOCK, _DARWIN_IDENTITY_INFLIGHT
    _DARWIN_IDENTITY_CACHE = OrderedDict()
    _DARWIN_IDENTITY_CACHE_LOCK = threading.RLock()
    _DARWIN_IDENTITY_INFLIGHT = {}


_register_at_fork = getattr(os, "register_at_fork", None)
if _register_at_fork is not None:
    _register_at_fork(after_in_child=_reset_darwin_identity_cache)

_PROC_ROOT: Path | None = None

_OWNER_IDENTITY_MISMATCH_GRACE_SECONDS = 10.0


class _OwnerProcessStatus(StrEnum):
    ALIVE = "alive"
    PID_MISSING = "pid-missing"
    IDENTITY_MISMATCH = "identity-mismatch"
    UNKNOWN = "unknown"


_DAEMON_CONTACT_ERRORS = (
    DaemonDisconnected,
    RuntimeError,
    OSError,
    TimeoutError,
    TypeError,
    ValueError,
)


def _read_darwin_process_start(pid: int) -> tuple[int, int] | None:
    """Read one macOS process birth time without the sandboxed ``ps`` CLI."""

    return darwin_process_start_time(pid)


def _read_process_parent(pid: int) -> int | None:
    """Read one parent PID without trusting caller-provided process facts."""

    if pid <= 1:
        return 0
    return process_parent_pid(pid)


_KNOWN_IDENTITY_SCHEMES = frozenset(
    {
        "proc-starttime",
        "darwin-starttime",
        "darwin-startsec",
        "ps-lstart",
        "win-filetime",
    }
)

#: The ps fallback prints a local-time string, so its environment is pinned:
#: an unpinned ``lstart`` changes with the reader's TZ/LC, and the launcher
#: and the Claude-Code-spawned child do not share one environment. Pinning
#: makes the component a function of the process birth alone.
_PS_IDENTITY_ENV = {"PATH": os.defpath, "TZ": "UTC0", "LC_ALL": "C"}

def _identity_components(marker: str) -> dict[str, str]:
    return manysys.parse_process_birth_marker(marker)


def _process_identities_match(expected: str, observed: str) -> bool:
    """Match birth evidence, prioritizing a shared precise Darwin component.

    Node's Darwin uptime clock can straddle the kernel's whole-second birth
    boundary. Only that explicitly coarse component gets a one-second
    tolerance, and only when it is the ONLY shared component. A common
    native microsecond marker must agree even if coarser fields are equal.
    Other legacy component matching remains unchanged; opaque markers stay
    exact, and a coarse-second near-miss never overrides another shared
    component's disagreement.
    """

    expected_components = _identity_components(expected)
    observed_components = _identity_components(observed)
    shared = expected_components.keys() & observed_components.keys()
    if "darwin-starttime" in shared:
        # A precise birth conflict is PID reuse even within the same second;
        # equal ps-lstart/coarse-second components cannot overrule it.
        return expected_components["darwin-starttime"] == observed_components["darwin-starttime"]
    for scheme in shared:
        if expected_components[scheme] == observed_components[scheme]:
            return True
        if scheme == "darwin-startsec" and shared == {"darwin-startsec"}:
            try:
                if abs(
                    int(expected_components[scheme])
                    - int(observed_components[scheme])
                ) <= 1:
                    return True
            except ValueError:
                pass
    return False


def _process_identities_comparable(expected: str, observed: str) -> bool:
    """Return whether two markers have any process-birth scheme in common."""

    shared = (
        _identity_components(expected).keys()
        & _identity_components(observed).keys()
    )
    return bool(shared - {"raw"})


def _read_ps_process_identity(pid: int) -> str | None:
    """Read the portable ps component under a pinned TZ/LC environment."""

    try:
        process = _PROCESS_POPEN(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_PS_IDENTITY_ENV,
        )
        stdout, _ = process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    except OSError:
        return None
    if process.returncode != 0:
        return None
    identity = stdout.strip()
    return f"ps-lstart:{identity}" if identity else None


def _cached_darwin_identity(pid: int, start: tuple[int, int]) -> str | None:
    with _DARWIN_IDENTITY_CACHE_LOCK:
        entry = _DARWIN_IDENTITY_CACHE.get(pid)
        if entry is not None and entry[0] == start:
            _DARWIN_IDENTITY_CACHE.move_to_end(pid)
            return entry[1]
    return None


def _darwin_birth_marker(start: tuple[int, int], ps_identity: str | None = None) -> str:
    # Preserve native microseconds, the Node-compatible coarse component,
    # and the historical ps component whenever it was successfully observed.
    components = [f"darwin-starttime:{start[0]}:{start[1]}", f"darwin-startsec:{start[0]}"]
    if ps_identity is not None:
        components.append(ps_identity)
    return ";".join(components)


def _probe_darwin_identity(pid: int) -> str | None:
    for _ in range(2):
        current = _read_darwin_process_start(pid)
        if current is None:
            return _read_ps_process_identity(pid)
        cached = _cached_darwin_identity(pid, current)
        if cached is not None:
            return cached
        ps_identity = _read_ps_process_identity(pid)
        confirmed = _read_darwin_process_start(pid)
        if confirmed is None:
            return ps_identity
        if confirmed != current:
            continue
        identity = _darwin_birth_marker(current, ps_identity)
        # Missing ps must remain retryable for legacy ps-only owners.
        if ps_identity is not None:
            with _DARWIN_IDENTITY_CACHE_LOCK:
                _DARWIN_IDENTITY_CACHE[pid] = current, identity
                _DARWIN_IDENTITY_CACHE.move_to_end(pid)
                while len(_DARWIN_IDENTITY_CACHE) > _DARWIN_IDENTITY_CACHE_LIMIT:
                    _DARWIN_IDENTITY_CACHE.popitem(last=False)
        return identity
    return None  # Birth stayed unstable; do not manufacture a hybrid marker.


def _read_darwin_identity(pid: int, start: tuple[int, int]) -> str | None:
    cached = _cached_darwin_identity(pid, start)
    if cached is not None:
        return cached
    with _DARWIN_IDENTITY_CACHE_LOCK:
        probe = _DARWIN_IDENTITY_INFLIGHT.get(pid)
        owner = probe is None and len(_DARWIN_IDENTITY_INFLIGHT) < _DARWIN_IDENTITY_CACHE_LIMIT
        if owner:
            probe = _DarwinIdentityProbe(threading.get_ident(), threading.Event())
            _DARWIN_IDENTITY_INFLIGHT[pid] = probe
    if probe is None:
        # Capacity overflow keeps the original uncached probing behavior.
        return _probe_darwin_identity(pid)
    if not owner:
        if probe.owner_thread != threading.get_ident():
            probe.done.wait(2.0)
        current = _read_darwin_process_start(pid)
        if current is None:
            return None
        # A waiting cohort shares a failed attempt without permanently caching
        # its failure. Later callers retry ps. Native evidence is read afresh.
        return _cached_darwin_identity(pid, current) or _darwin_birth_marker(current)
    try:
        return _probe_darwin_identity(pid)
    finally:
        with _DARWIN_IDENTITY_CACHE_LOCK:
            if _DARWIN_IDENTITY_INFLIGHT.get(pid) is probe:
                _DARWIN_IDENTITY_INFLIGHT.pop(pid)
        probe.done.set()

def _darwin_marker_for_pid(pid: int) -> str | None:
    native_start = _read_darwin_process_start(pid)
    if native_start is not None:
        return _read_darwin_identity(pid, native_start)
    return _read_ps_process_identity(pid)


def _read_process_identity(pid: int) -> str | None:
    """Return a multi-component process-birth marker for PID reuse fencing.

    Every component the platform offers is recorded, so a reader that later
    loses one source (a sandboxed ps spawn, a transiently unreadable procfs)
    still matches on the surviving component instead of flipping the whole
    marker format mid-watch and mimicking PID reuse.
    """

    return scoped_process_birth_marker(
        pid,
        proc_root=_PROC_ROOT,
        darwin_reader=_darwin_marker_for_pid,
        ps_reader=_read_ps_process_identity,
    )


def _owner_process_status(
    pid: int,
    expected_identity: str,
    *,
    signal_process: Callable[[int, int], None] | None = None,
    read_identity: Callable[[int], str | None] = _read_process_identity,
) -> _OwnerProcessStatus:
    """Classify a safe owner-fence observation without exposing its marker.

    Comparison supports old single-scheme markers and newer multi-component
    markers. Shared precise Darwin births must agree; otherwise compatible
    shared components retain the legacy matching rules. Comparable but
    conflicting evidence is IDENTITY_MISMATCH. No shared component -- like an unreadable
    marker -- is NO evidence: UNKNOWN, fail-safe, and never on the reaping
    path. "cannot read" and "read and it differs" must stay distinct
    verdicts; confusing them is what both killed live owners (format skew
    read as mismatch) and kept orphans alive (unreadable read as mismatch's
    opposite)."""

    try:
        if signal_process is None:
            probe_process(pid)
        else:
            signal_process(pid, 0)
    except ProcessLookupError:
        return _OwnerProcessStatus.PID_MISSING
    except (PermissionError, OSError):
        return _OwnerProcessStatus.UNKNOWN
    observed_identity = read_identity(pid)
    if observed_identity is None:
        return _OwnerProcessStatus.UNKNOWN
    expected_components = _identity_components(expected_identity)
    observed_components = _identity_components(observed_identity)
    shared = expected_components.keys() & observed_components.keys()
    if _process_identities_match(expected_identity, observed_identity):
        return _OwnerProcessStatus.ALIVE
    if shared:
        return _OwnerProcessStatus.IDENTITY_MISMATCH
    return _OwnerProcessStatus.UNKNOWN


def _owner_process_alive(
    pid: int,
    expected_identity: str,
    *,
    signal_process: Callable[[int, int], None] | None = None,
    read_identity: Callable[[int], str | None] = _read_process_identity,
) -> bool:
    """Check owner liveness without mistaking PID reuse for the original owner.

    Permission denial and an unreadable birth marker are fail-safe: retain the
    channel rather than risk killing one whose owner may still be alive.
    """

    return _owner_process_status(
        pid,
        expected_identity,
        signal_process=signal_process,
        read_identity=read_identity,
    ) in {_OwnerProcessStatus.ALIVE, _OwnerProcessStatus.UNKNOWN}
