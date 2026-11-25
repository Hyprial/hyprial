"""Inspect daemon processes without /proc or signalling, and check mapped libs.

Decisions (plan section 4.3, owner-approved 2026-11-20):
- The script runs as ``uv run --no-project`` and must not import hyprial or the
  test support process table, so this is its own minimal probe.
- One read-only command, the same on Linux and macOS:
  ``ps -o stat= -o lstart= -p <pid>``.  It asks only for the process state and
  its start time, never the command line or arguments of any process.  Two
  ``-o`` options, because POSIX lets a header given with ``=`` run to the end of
  its argument, so ``-o stat=,lstart=`` may differ between ``ps`` versions.
- ``os.kill(pid, 0)`` is not used: every observation goes through the
  ``Runner`` seam so tests fake it and no test can probe a real pid.
- Gone means: ``ps`` finds no such pid (exit 1, no output), the process is a
  zombie (state starts with ``Z``), or the pid now belongs to a process with
  another start time than the one recorded before the stop (pid reuse).
- A ``ps`` that cannot answer (missing, timed out, other exit codes) is
  "unknown", which never counts as gone.
- On macOS, native-actor library loading is inspected read-only via
  ``lsof -nP -p <pid> -Fn`` through ``Runner``, matching exact ``n``-lines or
  ``os.path.realpath`` equivalents so ``/var`` -> ``/private/var`` symlinks
  match without partial-substring false positives.  Any matching ``n``-line
  on a completed ``lsof`` run passes even if ``lsof`` exited non-zero on
  unrelated descriptors; timeouts always report ``BLOCKED`` even if partial
  stdout matched, and missing ``lsof``, non-zero exits without matches, or
  outputs lacking ``p<pid>`` / ``n``-lines report ``BLOCKED`` rather than
  triggering rollback.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Callable, Sequence

from .common import BLOCKED, FAIL, PASS, CommandResult

PS_TIMEOUT_SECONDS = 10.0
ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class Observation:
    status: str
    state: str | None = None
    birth: str | None = None


def parse_lsof_paths(stdout: str, pid: int) -> tuple[bool, list[str]]:
    """Extract whether ``p<pid>`` appeared and all ``n``-prefixed paths for it."""
    target = f"p{pid}"
    saw_pid = False
    active_pid: str | None = None
    paths: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("p"):
            active_pid = line
            if line == target:
                saw_pid = True
        elif line.startswith("n") and active_pid == target and len(line) > 1:
            paths.append(line[1:])
    return saw_pid, paths


def match_mapped_paths(
    installed: Sequence[str], mapped_paths: Sequence[str]
) -> list[str]:
    """Return installed library paths matching exact or realpath-resolved mapped paths."""
    exact = set(mapped_paths)
    resolved = {os.path.realpath(path) for path in mapped_paths}
    return [
        path
        for path in installed
        if path in exact or os.path.realpath(path) in resolved
    ]


class ProcessProbe:
    def __init__(self, run: Callable[..., CommandResult]):
        self.run = run

    def observe(self, pid: int) -> Observation:
        result = self.run(
            f"process-{pid}",
            ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
            timeout=PS_TIMEOUT_SECONDS,
        )
        if result.timed_out or result.returncode not in (0, 1):
            return Observation(UNKNOWN)
        line = next((item for item in result.stdout.splitlines() if item.strip()), "")
        if not line:
            return Observation(GONE) if result.returncode == 1 else Observation(UNKNOWN)
        state, _, rest = line.strip().partition(" ")
        return Observation(ALIVE, state, " ".join(rest.split()) or None)

    def gone(self, pid: int, birth: str | None) -> bool | None:
        seen = self.observe(pid)
        if seen.status == UNKNOWN:
            return None
        if seen.status == GONE:
            return True
        if (seen.state or "").startswith("Z"):
            return True
        if birth is not None and seen.birth != birth:
            return True
        return False

    def check_loaded_libraries(
        self, pid: int, installed: Sequence[str]
    ) -> tuple[str, list[str], str]:
        """Inspect mapped files of ``pid`` with ``lsof`` and return status, matches, detail."""
        result = self.run(
            f"lsof-{pid}",
            ["lsof", "-nP", "-p", str(pid), "-Fn"],
            timeout=PS_TIMEOUT_SECONDS,
        )
        if result.timed_out:
            return BLOCKED, [], f"lsof timed out inspecting pid {pid}"
        if result.returncode == 127:
            detail = result.stderr.strip()[:300] or "lsof not available"
            return BLOCKED, [], f"lsof unavailable (rc=127): {detail}"
        saw_pid, paths = parse_lsof_paths(result.stdout, pid)
        if saw_pid:
            loaded = match_mapped_paths(installed, paths)
            if loaded:
                return PASS, loaded, f"loaded={loaded}"
        if result.returncode != 0:
            detail = result.stderr.strip()[:300]
            suffix = f": {detail}" if detail else ""
            return (
                BLOCKED,
                [],
                f"lsof failed for pid {pid} (rc={result.returncode}){suffix}",
            )
        if not saw_pid:
            return (
                BLOCKED,
                [],
                f"lsof output did not identify target pid {pid}",
            )
        if not paths:
            return (
                BLOCKED,
                [],
                f"lsof returned no mapped paths for pid {pid} "
                "(permission or SIP limit)",
            )
        return (
            FAIL,
            [],
            "native actor library is installed but not mapped in the daemon process",
        )
