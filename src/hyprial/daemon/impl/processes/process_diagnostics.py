# Decision record (W2B-1, 2026-11-21):
# - Delegates platform CPU time and liveness sampling to
#   hyprial.kernel.{process_cpu_sample, probe_process} so Linux, macOS, and
#   Windows (GetProcessTimes) share one kernel seam with zero platform branches
#   in daemon/processes.
"""Small, side-effect-free process readings for failure diagnostics."""

from __future__ import annotations

from hyprial.kernel import (
    probe_process,
    process_cpu_sample as _kernel_process_cpu_sample,
)


def process_cpu_sample(pid: int) -> tuple[bool | None, float | None]:
    """Return liveness and one CPU sample without waiting on or signalling ``pid``."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None, None
    sampled = _kernel_process_cpu_sample(pid)
    if sampled is not None:
        return True, sampled[1]
    try:
        probe_process(pid)
    except ProcessLookupError:
        return False, None
    except OSError:
        return None, None
    return True, None


def process_cpu_seconds(pid: int) -> float | None:
    """Return only the CPU value from :func:`process_cpu_sample`."""

    return process_cpu_sample(pid)[1]
