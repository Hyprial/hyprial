# Decision record (manysys/process/owned_process, W1-MANYSYS & W2B-1 2026-11-21):
# - Migrated from kernel/impl/processes/owned_process.py into manysys/process.
# - Darwin proc_bsdinfo (<sys/proc_info.h>) places uint64_t pbi_start_tvsec at
#   byte offset 120 with total struct size 136 bytes; _format_darwin_birth_identity
#   formats timestamps with libc newlocale(LC_ALL_MASK, "") + strftime_l("%c")
#   (64-byte struct tm buffer for 56-byte 64-bit Darwin struct tm) so markers
#   persisted by the legacy `ps -o lstart=` path keep comparing equal across
#   C/en_US/zh_CN locales.
# - Linux /proc scanning skips FileNotFoundError (process exited during scan)
#   and returns None on other OSError/UnicodeError so ownership stays fail-safe.
# - Windows delegates process_birth_identity and parent-binding probes to
#   windows_process_identity (native FILETIME) rather than spawning `ps`.
# - W2B-1 extends this existing module (respecting the 7-file limit on
#   manysys/process/) with process_parent_pid, process_cpu_sample,
#   darwin_process_start_time, scoped_process_birth_marker,
#   probe_process_group_or_pid, and terminate_fenced_process.
# - W2B-1 R3 verified 2c706e2850:src/hyprial/daemon/impl/mcp/channel/ownership.py
#   exact wire/disk marker emission (`proc-starttime:<field22>`,
#   `darwin-starttime:<sec>:<usec>;darwin-startsec:<sec>[;ps-lstart:<utc>]` with
#   two-pass libproc birth confirmation around ps, `ps-lstart:<utc>`, and
#   `win-filetime:<u64>`) while normalizing legacy/platform-prefixed schemes
#   (`linux:<ticks>`, `proc:<ticks>`, `darwin:<sec>:<usec>`, `darwin:<sec>`,
#   `win32:<u64>`) in component matching and fenced termination for zero-skew
#   upgrade restarts.
# - OwnedProcessGroup retains rejected/stopping registrations until the entire
#   PGID is proven gone, keeps _stop_requested sticky across races, and treats a
#   live group with an unreadable live leader as ambiguous/not-stopped while
#   allowing descendant cleanup when the original leader PID has exited.
# - Process birth marker parsing has one public owner here; daemon identity
#   comparison and Windows fencing delegate to parse_process_birth_marker.
"""PID-group ownership fenced by stable process birth identity."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from hyprial.kernel.impl.manysys.process.facts import (
    ProcessLiveness,
    ProcessLivenessProbeError,
    ProcessLivenessState,
)
from hyprial.kernel.impl.manysys.process.spawn import probe_process

PROCESS_FORCE_TERM_SECONDS = 0.25
PROCESS_FORCE_KILL_SECONDS = 0.25
DEAD_PROCESS_STATES = frozenset({"X", "Z"})
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_PROC_BSDINFO_START_TVSEC_OFFSET = 120
_DARWIN_PROC_BSDINFO_SIZE = 136
_DARWIN_LC_ALL_MASK = 0x3F
_DARWIN_TM_BUFFER = 64
_CPU_PROBE_TIMEOUT_SECONDS = 1.0
_PS_CPUTIME = re.compile(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)")
_DARWIN_STARTTIME_RE = re.compile(r"\A[0-9]+:[0-9]+\Z")
_SCOPED_IDENTITY_SCHEMES = frozenset(
    {
        "proc-starttime",
        "darwin-starttime",
        "darwin-startsec",
        "ps-lstart",
        "win-filetime",
    }
)


class _DarwinProcBsdInfo(ctypes.Structure):
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


@lru_cache(maxsize=1)
def _darwin_proc_pidinfo() -> Any:
    import ctypes

    function = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    function.restype = ctypes.c_int
    return function


@lru_cache(maxsize=1)
def _darwin_libc() -> Any:
    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.newlocale.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
    libc.newlocale.restype = ctypes.c_void_p
    libc.freelocale.argtypes = (ctypes.c_void_p,)
    libc.freelocale.restype = ctypes.c_int
    libc.localtime_r.argtypes = (ctypes.POINTER(ctypes.c_long), ctypes.c_void_p)
    libc.localtime_r.restype = ctypes.c_void_p
    libc.strftime_l.argtypes = (
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    libc.strftime_l.restype = ctypes.c_size_t
    return libc


def _format_darwin_birth_identity(start_tvsec: int) -> str | None:
    import ctypes

    libc = _darwin_libc()
    libc.tzset()
    locale = libc.newlocale(_DARWIN_LC_ALL_MASK, b"", None)
    if not locale:
        return None
    try:
        when = ctypes.c_long(start_tvsec)
        tm = ctypes.create_string_buffer(_DARWIN_TM_BUFFER)
        if not libc.localtime_r(ctypes.byref(when), tm):
            return None
        out = ctypes.create_string_buffer(256)
        size = libc.strftime_l(out, len(out), b"%c", tm, locale)
    finally:
        libc.freelocale(locale)
    if not size:
        return None
    stamp = out.raw[:size].decode("utf-8").strip()
    return f"ps:{stamp}" if stamp else None


def _darwin_process_birth_identity(pid: int) -> str | None:
    import ctypes

    buffer = (ctypes.c_ubyte * _DARWIN_PROC_BSDINFO_SIZE)()
    read = _darwin_proc_pidinfo()(
        pid,
        _DARWIN_PROC_PIDTBSDINFO,
        0,
        ctypes.byref(buffer),
        _DARWIN_PROC_BSDINFO_SIZE,
    )
    if read != _DARWIN_PROC_BSDINFO_SIZE:
        return None
    start_tvsec = ctypes.c_uint64.from_buffer(
        buffer,
        _DARWIN_PROC_BSDINFO_START_TVSEC_OFFSET,
    ).value
    return _format_darwin_birth_identity(start_tvsec) if start_tvsec else None


def _ps_process_birth_identity(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    identity = result.stdout.strip() if result.returncode == 0 else ""
    return f"ps:{identity}" if identity else None


def parse_linux_process_stat(stat: str) -> tuple[str, int, str] | None:
    """Return state, process group, and birth marker from /proc/PID/stat."""

    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        return None
    try:
        process_group_id = int(fields[2])
    except ValueError:
        return None
    return fields[0], process_group_id, fields[19]


def read_linux_process_stat(pid: int) -> tuple[str, int, str] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return parse_linux_process_stat(stat)


def linux_group_has_live_members(process_group_id: int) -> bool | None:
    """Distinguish live group members from PID-1-owned zombie remnants."""

    saw_member = False
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return None
        observed = parse_linux_process_stat(stat)
        if observed is None:
            return None
        if observed[1] != process_group_id:
            continue
        saw_member = True
        if observed[0] not in DEAD_PROCESS_STATES:
            return True
    return False if saw_member else None


def darwin_group_has_live_members(process_group_id: int) -> bool | None:
    """Return Darwin group liveness, or ``None`` when it cannot be proven."""

    try:
        result = subprocess.run(
            ["ps", "-axo", "state=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    saw_member = False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None
        state, raw_group_id = fields
        try:
            observed_group_id = int(raw_group_id)
        except ValueError:
            return None
        if observed_group_id != process_group_id:
            continue
        saw_member = True
        if not state or state[0] not in DEAD_PROCESS_STATES:
            return True
    return False if saw_member else None


def process_birth_identity(pid: int) -> str | None:
    """Return a stable birth marker used to fence PID/PGID reuse."""

    if sys.platform.startswith("linux"):
        observed = read_linux_process_stat(pid)
        return f"proc:{observed[2]}" if observed is not None else None
    if sys.platform == "darwin":
        try:
            identity = _darwin_process_birth_identity(pid)
        except Exception:
            identity = None
        if identity is not None:
            return identity
    if sys.platform == "win32":
        from hyprial.kernel.impl.manysys.process.ports import windows_process_identity

        try:
            return windows_process_identity(pid)
        except OSError:
            return None
    return _ps_process_birth_identity(pid)


def process_exited_unreaped(pid: int, birth_identity: str) -> bool:
    """Return True only when that exact process has exited and merely awaits a reap."""

    if not sys.platform.startswith("linux"):
        return False
    observed = read_linux_process_stat(pid)
    if observed is None or f"proc:{observed[2]}" != birth_identity:
        return False
    return observed[0] in DEAD_PROCESS_STATES


def _read_darwin_bsdinfo(pid: int) -> _DarwinProcBsdInfo | None:
    try:
        proc_pidinfo = _darwin_proc_pidinfo()
    except (OSError, AttributeError):
        return None
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
    if read != ctypes.sizeof(info) or int(info.pid) != pid:
        return None
    return info


def darwin_process_start_time(pid: int) -> tuple[int, int] | None:
    """Return ``(start_sec, start_usec)`` for a live macOS process via ``libproc``."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    info = _read_darwin_bsdinfo(pid)
    if info is None or info.start_sec <= 0:
        return None
    return int(info.start_sec), int(info.start_usec)


def process_parent_pid(pid: int) -> int | None:
    """Return OS parent PID for ``pid``, or ``None`` if unavailable/reused."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        closing = stat.rfind(")")
        fields = stat[closing + 2 :].split() if closing >= 0 else []
        try:
            parent = int(fields[1])
        except (IndexError, ValueError):
            return None
        return parent if parent >= 0 else None
    if sys.platform == "darwin":
        info = _read_darwin_bsdinfo(pid)
        return int(info.ppid) if info is not None else None
    if sys.platform == "win32" or os.name == "nt":
        from hyprial.kernel.impl.manysys.process.windows_process import (
            windows_process_parent_pid,
        )

        return windows_process_parent_pid(pid)
    return None


def _parse_ps_cputime(value: str) -> float | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        match = _PS_CPUTIME.fullmatch(raw)
        if match is None:
            return None
        days, hours, minutes, seconds = match.groups()
        total = (
            int(days or 0) * 86400
            + int(hours or 0) * 3600
            + int(minutes) * 60
            + float(seconds)
        )
    except (TypeError, ValueError):
        return None
    return total if total >= 0 else None


def process_cpu_sample(pid: int) -> tuple[str, float] | None:
    """Return ``(birth_identity, cpu_seconds)`` without waiting on or signalling ``pid``."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            with (Path("/proc") / str(pid) / "stat").open("rb") as stream:
                raw = stream.read(4096)
            close = raw.rfind(b")")
            fields = raw[close + 1 :].split() if close >= 0 else []
            if len(fields) <= 19:
                return None
            ticks = int(fields[11]) + int(fields[12])
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
            if ticks < 0 or ticks_per_second <= 0:
                return None
            birth = f"proc:{fields[19].decode('ascii', 'replace')}"
            return birth, ticks / ticks_per_second
        except (OSError, TypeError, ValueError):
            return None
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["ps", "-o", "cputime=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=_CPU_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        cpu_seconds = _parse_ps_cputime(completed.stdout)
        if cpu_seconds is None:
            return None
        try:
            birth = _darwin_process_birth_identity(pid)
        except Exception:
            birth = None
        return (birth or f"pid:{pid}"), cpu_seconds
    if sys.platform == "win32" or os.name == "nt":
        from hyprial.kernel.impl.manysys.process.windows_process import (
            windows_process_cpu_sample,
        )

        return windows_process_cpu_sample(pid)
    return None


def _ps_utc_process_identity(pid: int) -> str | None:
    env = {"PATH": os.defpath, "TZ": "UTC0", "LC_ALL": "C"}
    try:
        process = subprocess.Popen(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
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


def _normalize_scoped_component(
    scheme: str, value: str
) -> tuple[str, str] | None:
    if scheme in _SCOPED_IDENTITY_SCHEMES:
        return (scheme, value) if value else None
    if scheme in {"linux", "proc"} and value.isdigit():
        return "proc-starttime", value
    if scheme == "win32" and value.isdigit():
        return "win-filetime", value
    if scheme == "darwin":
        if _DARWIN_STARTTIME_RE.fullmatch(value):
            return "darwin-starttime", value
        if value.isdigit():
            return "darwin-startsec", value
    return None


def scoped_process_birth_marker(
    pid: int,
    *,
    proc_root: Path | None = None,
    darwin_reader: Callable[[int], str | None] | None = None,
    ps_reader: Callable[[int], str | None] | None = None,
) -> str | None:
    """Return a scheme-prefixed birth marker matching 2c706e2850 ownership.py."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "win32" or os.name == "nt":
        from hyprial.kernel.impl.manysys.process.ports import windows_process_identity

        try:
            return windows_process_identity(pid)
        except OSError:
            return None
    resolved_ps = ps_reader or _ps_utc_process_identity
    if sys.platform.startswith("linux"):
        stat_path = (Path("/proc") if proc_root is None else proc_root) / str(pid) / "stat"
        try:
            stat = stat_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            stat = None
        if stat is not None:
            comm_end = stat.rfind(")")
            remainder = stat[comm_end + 2 :].split() if comm_end >= 0 else []
            if len(remainder) > 19 and remainder[19].isdigit():
                return f"proc-starttime:{remainder[19]}"
        return resolved_ps(pid)
    if sys.platform == "darwin":
        if darwin_reader is not None:
            return darwin_reader(pid)
        for _ in range(2):
            start = darwin_process_start_time(pid)
            if start is None:
                return resolved_ps(pid)
            ps_value = resolved_ps(pid)
            confirmed = darwin_process_start_time(pid)
            if confirmed is None:
                return ps_value
            if confirmed != start:
                continue
            parts = [
                f"darwin-starttime:{start[0]}:{start[1]}",
                f"darwin-startsec:{start[0]}",
            ]
            if ps_value is not None:
                parts.append(ps_value)
            return ";".join(parts)
        return None
    return resolved_ps(pid)


def split_process_birth_marker_pieces(
    marker: str,
) -> list[tuple[str, str]] | None:
    if not marker:
        return None
    pieces: list[tuple[str, str]] = []
    for piece in marker.split(";"):
        scheme, separator, value = piece.partition(":")
        if not separator:
            return None
        pieces.append((scheme, value))
    return pieces


def parse_process_birth_marker(marker: str) -> dict[str, str]:
    pieces = split_process_birth_marker_pieces(marker)
    if pieces is None:
        return {"raw": marker}
    components: dict[str, str] = {}
    for scheme, value in pieces:
        normalized = _normalize_scoped_component(scheme, value)
        if normalized is None:
            return {"raw": marker}
        canonical_scheme, canonical_value = normalized
        existing = components.get(canonical_scheme)
        if existing is not None and existing != canonical_value:
            return {"raw": marker}
        components[canonical_scheme] = canonical_value
    return components


_scoped_components = parse_process_birth_marker


def _scoped_markers_match(expected: str, observed: str) -> bool:
    expected_map = _scoped_components(expected)
    observed_map = _scoped_components(observed)
    shared = expected_map.keys() & observed_map.keys()
    if "darwin-starttime" in shared:
        return expected_map["darwin-starttime"] == observed_map["darwin-starttime"]
    for scheme in shared:
        if expected_map[scheme] == observed_map[scheme]:
            return True
        if scheme == "darwin-startsec" and shared == {"darwin-startsec"}:
            try:
                if abs(int(expected_map[scheme]) - int(observed_map[scheme])) <= 1:
                    return True
            except ValueError:
                pass
    return False


def _posix_birth_marker_matches(pid: int, birth_marker: str) -> bool:
    plain = process_birth_identity(pid)
    if plain is not None and plain == birth_marker:
        return True
    scoped = scoped_process_birth_marker(pid)
    if scoped is None:
        return False
    return _scoped_markers_match(birth_marker, scoped)


def probe_process_group_or_pid(pid: int) -> ProcessLiveness:
    """Probe process-group existence on POSIX or process existence on Windows."""
    try:
        if sys.platform == "win32" or os.name == "nt":
            probe_process(pid)
        else:
            os.killpg(pid, 0)
    except ProcessLookupError:
        return ProcessLiveness(ProcessLivenessState.DEAD, observed=True, pid=pid)
    except OSError as error:
        raise ProcessLivenessProbeError(
            f"existence probe failed for pid {pid}: {error}"
        ) from error
    return ProcessLiveness(ProcessLivenessState.ALIVE, observed=True, pid=pid)


def terminate_fenced_process(
    pid: int,
    *,
    birth_marker: str | None = None,
    process_group: bool = False,
) -> bool:
    """Terminate a process or process group only while its birth marker matches."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32" or os.name == "nt":
        from hyprial.kernel.impl.manysys.process.windows_process import (
            windows_terminate_fenced_process,
        )

        return windows_terminate_fenced_process(pid, birth_marker)
    if birth_marker is not None and not _posix_birth_marker_matches(pid, birth_marker):
        return False
    try:
        if process_group:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def bind_current_process_to_parent(
    parent_pid: int,
    parent_identity: str,
    *,
    before_exit: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """Exit this child when its parent PID/birth identity cannot be verified."""
    if (
        type(parent_pid) is not int
        or parent_pid <= 0
        or not isinstance(parent_identity, str)
        or not parent_identity
    ):
        raise ValueError("parent PID and birth identity are required")
    stopped = threading.Event()

    def read_identity() -> str | None:
        if os.name == "nt":
            from hyprial.kernel.impl.manysys.process.ports import windows_process_identity

            return windows_process_identity(parent_pid)
        return process_birth_identity(parent_pid)

    def cleanup() -> None:
        if before_exit is None:
            return
        try:
            before_exit()
        except BaseException:  # noqa: BLE001
            pass

    def watch_parent() -> None:
        while not stopped.is_set():
            try:
                alive = os.getppid() == parent_pid and read_identity() == parent_identity
            except Exception:  # noqa: BLE001
                alive = False
            if not alive:
                try:
                    if before_exit is not None:
                        callback = threading.Thread(
                            target=cleanup, name="parent-loss-cleanup", daemon=True
                        )
                        callback.start()
                        callback.join(timeout=0.25)
                finally:
                    os._exit(0)
            stopped.wait(0.2)

    watcher = threading.Thread(
        target=watch_parent, name="parent-process-binding", daemon=True
    )
    watcher.start()

    def release() -> None:
        stopped.set()
        watcher.join(timeout=1.5)
        if watcher.is_alive():
            raise RuntimeError("parent process binding cleanup is unconfirmed")

    return release


class OwnedProcessGroup:
    """Thread-safe OS-only cleanup handle for one subprocess generation."""

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._identity: str | None = None
        self._stop_requested = False
        self._registrations = 0
        self._observed = False

    @staticmethod
    def _process_birth_identity(pid: int) -> str | None:
        return process_birth_identity(pid)

    @staticmethod
    def _linux_group_has_live_members(process_group_id: int) -> bool | None:
        return linux_group_has_live_members(process_group_id)

    @staticmethod
    def _darwin_group_has_live_members(process_group_id: int) -> bool | None:
        return darwin_group_has_live_members(process_group_id)

    def register(self, pid: int) -> None:
        with self._lock:
            self._registrations += 1
            self._observed = True
        try:
            identity = self._process_birth_identity(pid)
            with self._lock:
                stopping = self._stop_requested
                if identity is not None and not stopping:
                    self._pid = pid
                    self._identity = identity
                    return
                self._pid = pid
                self._identity = identity
            self._close_unregistered_group(pid)
            self.release_if_gone(pid)
            if identity is None:
                raise ConnectionError(
                    f"cannot establish {self._label} process identity"
                )
            raise ConnectionError(f"{self._label} process is stopping")
        finally:
            with self._lock:
                self._registrations -= 1

    def exists(self, pid: int) -> bool:
        with self._lock:
            return self._pid == pid and self._is_current_locked()

    def signal(self, pid: int, signum: signal.Signals) -> None:
        with self._lock:
            if self._pid != pid or not self._is_current_locked():
                return
            try:
                os.killpg(pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    def release_if_gone(self, pid: int) -> None:
        with self._lock:
            if self._pid == pid and not self._group_has_live_members(pid):
                self._pid = None
                self._identity = None

    def liveness(self) -> ProcessLiveness:
        with self._lock:
            observed = self._observed
            pid = self._pid
            marker = self._identity
        if not observed:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=False,
                detail="process group has never been observed",
            )
        if pid is None:
            return ProcessLiveness(ProcessLivenessState.DEAD, observed=True)
        if marker is None:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=True,
                pid=pid,
                detail="process identity criterion is unavailable",
            )
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return ProcessLiveness(
                ProcessLivenessState.DEAD,
                observed=True,
                pid=pid,
                marker=marker,
            )
        except OSError as error:
            raise ProcessLivenessProbeError(
                f"cannot probe process group {pid}: {error}"
            ) from error
        observed_marker = self._process_birth_identity(pid)
        if observed_marker is None:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=True,
                pid=pid,
                marker=marker,
                detail="live process identity cannot be read",
            )
        if observed_marker != marker:
            raise ProcessLivenessProbeError(
                f"process identity changed for group {pid}"
            )
        return ProcessLiveness(
            ProcessLivenessState.ALIVE,
            observed=True,
            pid=pid,
            marker=marker,
        )

    def stopped(self) -> bool:
        with self._lock:
            if self._registrations:
                return False
            pid = self._pid
            if pid is None:
                return True
            if self._is_current_locked():
                return False
            if self._group_has_live_members(pid):
                try:
                    probe_process(pid)
                except ProcessLookupError:
                    return False
                except OSError:
                    return False
                observed = self._process_birth_identity(pid)
                if observed is None:
                    return False
            self._pid = None
            self._identity = None
            return True

    def force_close(self) -> None:
        with self._lock:
            self._stop_requested = True
            pid = self._pid
        if pid is None:
            return
        self.signal(pid, signal.SIGTERM)
        if self._wait_gone(pid, PROCESS_FORCE_TERM_SECONDS):
            return
        self.signal(pid, signal.SIGKILL)
        self._wait_gone(pid, PROCESS_FORCE_KILL_SECONDS)

    @classmethod
    def _close_unregistered_group(cls, pid: int) -> None:
        cls._signal_unregistered_group(pid, signal.SIGTERM)
        if cls._wait_unregistered_group_gone(pid, PROCESS_FORCE_TERM_SECONDS):
            return
        cls._signal_unregistered_group(pid, signal.SIGKILL)
        cls._wait_unregistered_group_gone(pid, PROCESS_FORCE_KILL_SECONDS)

    @staticmethod
    def _signal_unregistered_group(pid: int, signum: signal.Signals) -> None:
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            pass

    @classmethod
    def _wait_unregistered_group_gone(cls, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while cls._group_has_live_members(pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.exists(pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        self.release_if_gone(pid)
        return True

    def _is_current_locked(self) -> bool:
        pid = self._pid
        identity = self._identity
        if pid is None or identity is None or not self._group_has_live_members(pid):
            return False
        observed = self._process_birth_identity(pid)
        if observed is not None:
            return observed == identity
        try:
            probe_process(pid)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    @classmethod
    def _group_has_live_members(cls, pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if sys.platform.startswith("linux"):
            live = cls._linux_group_has_live_members(pid)
            if live is not None:
                return live
        elif sys.platform == "darwin":
            live = cls._darwin_group_has_live_members(pid)
            if live is not None:
                return live
        return True
