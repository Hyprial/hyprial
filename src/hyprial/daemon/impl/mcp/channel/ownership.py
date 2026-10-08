"""Owner-process identity and liveness probes (darwin/proc/ps)."""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path



from hyprial.daemon.impl.mcp.api  import DaemonDisconnected

_logger = logging.getLogger(__name__)

_PROCESS_POPEN = subprocess.Popen

_PROC_ROOT = Path("/proc")

_OWNER_IDENTITY_MISMATCH_GRACE_SECONDS = 10.0

class _DarwinProcBsdInfo(ctypes.Structure):
    """Stable prefix of Darwin's ``proc_bsdinfo`` including process birth."""

    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]

_DARWIN_PROC_PIDTBSDINFO = 3

class _OwnerProcessStatus(StrEnum):
    ALIVE = "alive"
    PID_MISSING = "pid-missing"
    IDENTITY_MISMATCH = "identity-mismatch"
    UNKNOWN = "unknown"

# Daemon-contact failures the channel poll loop must survive without crashing.
# A daemon restart (the deploy path) drops the Unix socket and, for a moment
# while the new daemon boots, can answer with an error envelope surfaced as
# RuntimeError (see mcp.unix). A version-skewed daemon can also return a
# response shape this child cannot parse -- _remember_messages raises TypeError
# on a non-array messages field and coordinator.enqueue raises ValueError on an
# empty deliveryId. None of these may propagate out of the poll task: Claude
# Code cannot respawn a dead stdio MCP child, so an unguarded raise here would
# orphan the channel and force a full session restart -- the exact failure this
# loop exists to prevent. Cancellation is a BaseException and is never caught
# here, so shutdown still propagates cleanly.
_DAEMON_CONTACT_ERRORS = (
    DaemonDisconnected,
    RuntimeError,
    OSError,
    TimeoutError,
    TypeError,
    ValueError,
)

def _read_darwin_process_identity(pid: int) -> str | None:
    """Read one macOS process birth time without the sandboxed ``ps`` CLI."""

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = library.proc_pidinfo
    except (OSError, AttributeError):
        return None
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _DarwinProcBsdInfo()
    try:
        read = proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except (OSError, ValueError):
        return None
    if (
        read != ctypes.sizeof(info)
        or info.pid != pid
        or info.start_sec <= 0
    ):
        return None
    return f"darwin-starttime:{info.start_sec}:{info.start_usec}"

#: Component schemes a process-birth marker may carry. A marker is one or more
#: ``scheme:value`` components joined by ``;``; a single-component marker is
#: indistinguishable from the historical single-scheme strings, so markers
#: written by older builds parse in the same grammar.
_KNOWN_IDENTITY_SCHEMES = frozenset(
    {"proc-starttime", "darwin-starttime", "ps-lstart"}
)

#: The ps fallback prints a local-time string, so its environment is pinned:
#: an unpinned ``lstart`` changes with the reader's TZ/LC, and the launcher
#: and the Claude-Code-spawned child do not share one environment. Pinning
#: makes the component a function of the process birth alone.
_PS_IDENTITY_ENV = {"PATH": os.defpath, "TZ": "UTC0", "LC_ALL": "C"}

def _identity_components(marker: str) -> dict[str, str]:
    """Split a marker into comparable ``scheme -> value`` components.

    A piece without a recognized scheme opaques the whole marker: comparing
    two unknown formats component-wise would invent evidence either way.
    """

    components: dict[str, str] = {}
    for piece in marker.split(";"):
        scheme, separator, value = piece.partition(":")
        if not separator or scheme not in _KNOWN_IDENTITY_SCHEMES:
            return {"raw": marker}
        components[scheme] = value
    return components

def _read_proc_process_identity(pid: int) -> str | None:
    """Read the procfs starttime component (Linux)."""

    stat_path = _PROC_ROOT / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # A Linux host without procfs can still use the portable fallback.
        # If procfs exists but this PID vanished between kill(0) and read,
        # retain the child for this observation; the next liveness probe
        # will produce PID_MISSING.
        return None
    except (OSError, UnicodeError):
        # Permission denial and unreadable procfs are deliberately
        # fail-safe. Do not switch marker formats mid-process and mistake
        # that format change for PID reuse.
        return None
    # /proc/<pid>/stat field 2 (comm) may contain spaces or ')'. Split
    # after its final ')' so remainder[19] is field 22, starttime.
    comm_end = stat.rfind(")")
    remainder = stat[comm_end + 2 :].split() if comm_end >= 0 else []
    if len(remainder) <= 19 or not remainder[19].isdigit():
        return None
    return f"proc-starttime:{remainder[19]}"

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

def _resolve_tmux_pane_owner(
    tmux_session: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    read_identity: Callable[[int], str | None] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[int, str] | None:
    """Resolve the owner fence of a detached-tmux TUI to its pane process.

    A detached-tmux launcher exits right after spawn, so its PID can never be
    the owner fence: the pane's top process lives exactly as long as the tmux
    session and takes that role instead.  Any lookup failure degrades to the
    getppid compatibility watch rather than fencing a live session to death.
    """

    resolved_read_identity = (
        _read_process_identity if read_identity is None else read_identity
    )
    tmux_bin = which("tmux")
    if tmux_bin is None:
        return None
    try:
        result = run(
            [
                tmux_bin,
                "display-message",
                "-p",
                "-t",
                tmux_session,
                "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        pid = int(str(result.stdout).strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    identity = resolved_read_identity(pid)
    if identity is None:
        return None
    return pid, identity

def _read_process_identity(pid: int) -> str | None:
    """Return a multi-component process-birth marker for PID reuse fencing.

    Every component the platform offers is recorded, so a reader that later
    loses one source (a sandboxed ps spawn, a transiently unreadable procfs)
    still matches on the surviving component instead of flipping the whole
    marker format mid-watch and mimicking PID reuse.
    """

    if pid <= 0:
        return None
    if sys.platform == "win32":
        from hyprial.kernel import windows_process_identity as process_identity

        try:
            return process_identity(pid)
        except OSError:
            return None
    components: list[str] = []
    if sys.platform.startswith("linux"):
        proc_identity = _read_proc_process_identity(pid)
        if proc_identity is not None:
            # procfs answered; keep the historical single-component marker
            # and skip the ps spawn entirely.
            return proc_identity
    if sys.platform == "darwin":
        native_identity = _read_darwin_process_identity(pid)
        if native_identity is not None:
            components.append(native_identity)
    ps_identity = _read_ps_process_identity(pid)
    if ps_identity is not None:
        components.append(ps_identity)
    return ";".join(components) if components else None

def _owner_process_status(
    pid: int,
    expected_identity: str,
    *,
    signal_process: Callable[[int, int], None] = os.kill,
    read_identity: Callable[[int], str | None] = _read_process_identity,
) -> _OwnerProcessStatus:
    """Classify a safe owner-fence observation without exposing its marker.

    Comparison is component-wise so markers can cross formats (an older
    launcher wrote a single-scheme string; a newer child may read several
    components). Any shared component with an equal value proves the same
    process. A shared component whose values ALL differ is positive PID-reuse
    evidence (IDENTITY_MISMATCH). No shared component -- like an unreadable
    marker -- is NO evidence: UNKNOWN, fail-safe, and never on the reaping
    path. "cannot read" and "read and it differs" must stay distinct
    verdicts; confusing them is what both killed live owners (format skew
    read as mismatch) and kept orphans alive (unreadable read as mismatch's
    opposite)."""

    try:
        if sys.platform == "win32" and signal_process is os.kill:
            from hyprial.kernel import windows_process_identity as process_identity

            process_identity(pid)
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
    for scheme in shared:
        if expected_components[scheme] == observed_components[scheme]:
            return _OwnerProcessStatus.ALIVE
    if shared:
        return _OwnerProcessStatus.IDENTITY_MISMATCH
    return _OwnerProcessStatus.UNKNOWN

def _owner_process_alive(
    pid: int,
    expected_identity: str,
    *,
    signal_process: Callable[[int, int], None] = os.kill,
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
