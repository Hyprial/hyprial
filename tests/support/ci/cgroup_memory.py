"""Read-only cgroup memory evidence for CI logs.

Decisions:

- A killed pytest worker is only attributable if the log says whether the
  kernel's OOM killer acted, so the ``oom_kill`` counter is read next to the
  worker exit (KP-073).  cgroup v2 keeps it in ``memory.events``; cgroup v1
  keeps it in ``memory/memory.oom_control`` on newer kernels.
- Every read is failure tolerant: a missing file (macOS, no cgroup mount), an
  unreadable file or malformed content yields ``None``, never an exception,
  because a diagnostic must not fail or slow the run it observes.
- ``CI_CGROUP_ROOT`` points the reader at a fake tree for tests; the default is
  ``/sys/fs/cgroup``.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "/sys/fs/cgroup"

_EVENT_FILES = ("memory.events", "memory/memory.oom_control")


def cgroup_root() -> Path:
    """The cgroup mount to read, honouring ``CI_CGROUP_ROOT``."""

    return Path(os.environ.get("CI_CGROUP_ROOT") or DEFAULT_ROOT)


def oom_kill(root: Path | None = None) -> int | None:
    """The cgroup's ``oom_kill`` counter, or ``None`` when it cannot be read."""

    base = root if root is not None else cgroup_root()
    for relative in _EVENT_FILES:
        try:
            text = (base / relative).read_text(encoding="ascii")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "oom_kill":
                try:
                    count = int(parts[1])
                except ValueError:
                    return None
                return count if count >= 0 else None
    return None


def oom_kill_since(start: int | None, root: Path | None = None) -> tuple[int | None, int | None]:
    """The current counter and its growth since ``start`` (``None`` if unknown)."""

    current = oom_kill(root)
    if current is None or start is None:
        return current, None
    return current, current - start
