"""Small, side-effect-free process readings for failure diagnostics."""

from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
import sys

from hyprial.contracts.lifecycle_budgets import PROCESS_CPU_PROBE_TIMEOUT_SECONDS

_PS_CPUTIME = re.compile(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)")


def _parse_ps_cputime(value: str) -> float | None:
    """Parse ``[[days-]hours:]minutes:seconds`` from macOS ``ps``."""

    raw = value.strip()
    if not raw:
        return None
    try:
        # A ps clock, not an address: matched as a whole rather than split.
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


def process_cpu_sample(pid: int) -> tuple[bool | None, float | None]:
    """Return liveness and one CPU sample without waiting on or signalling ``pid``.

    Linux performs one bounded read of ``/proc/<pid>/stat`` and converts its
    utime+stime ticks with ``SC_CLK_TCK``.  macOS performs one
    ``ps -o cputime= -p <pid>`` probe capped at one second.  There is no retry,
    child wait, or signal; a gone child, unsupported platform, malformed
    answer, timeout, or other probe failure returns a null CPU reading.
    Liveness is ``None`` when that single probe cannot distinguish a gone
    process from a probe failure.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None, None
    if sys.platform.startswith("linux"):
        try:
            with (Path("/proc") / str(pid) / "stat").open("rb") as stream:
                raw = stream.read(4096)
            # comm is parenthesised and may contain spaces or ')' characters;
            # the last ')' is the only safe boundary before field 3.
            close = raw.rfind(b")")
            fields = raw[close + 1 :].split() if close >= 0 else []
            if len(fields) <= 12:
                return True, None
            ticks = int(fields[11]) + int(fields[12])
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
            if ticks < 0 or ticks_per_second <= 0:
                return True, None
            return True, ticks / ticks_per_second
        except FileNotFoundError:
            return False, None
        except (OSError, TypeError, ValueError):
            return None, None
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["ps", "-o", "cputime=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=PROCESS_CPU_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None, None
        if completed.returncode != 0:
            return False, None
        return True, _parse_ps_cputime(completed.stdout)
    return None, None


def process_cpu_seconds(pid: int) -> float | None:
    """Return only the CPU value from :func:`process_cpu_sample`."""

    return process_cpu_sample(pid)[1]
