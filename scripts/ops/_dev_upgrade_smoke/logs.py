"""Copies of the daemon's service logs for the upgrade window.

Decisions (plan section 4.2, owner-approved 2026-11-20):
- Portable: the files both service managers write under the state directory
  (``daemon-service.stdout.log``, ``daemon-service.stderr.log``), the launch
  capture ``daemon-launch.log`` and the start-failure record
  ``daemon-start-failures.json``.  No ``journalctl`` and no ``log show``.
- Read-only on the home: files are opened for reading only; nothing is
  truncated, rotated or deleted.
- Positions are marked just before the stop, so only the upgrade window is
  copied.  Each capture copies what was appended since the previous capture
  and moves the mark.  A file whose inode changed (``daemon-launch.log`` is
  re-linked per launch, the failure record is replaced atomically) or that
  shrank is copied from its start.
- Each copy keeps at most ``MAX_SERVICE_LOG_BYTES`` (the tail) and is passed
  through ``redact_auth_material`` so device-authorization URLs and one-time
  codes never reach the artifacts.
- Without a resolved state directory nothing is marked and nothing is copied;
  there is no fallback to a default home.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import redact_auth_material

MAX_SERVICE_LOG_BYTES = 256 * 1024
SERVICE_LOG_NAMES = (
    "daemon-service.stdout.log",
    "daemon-service.stderr.log",
    "daemon-launch.log",
    "daemon-start-failures.json",
)


def position(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_size


class ServiceLogs:
    def __init__(self, target: Path):
        self.target = target
        self.state_dir: Path | None = None
        self.marks: dict[str, tuple[int, int] | None] | None = None
        self.copies: list[dict[str, Any]] = []

    def mark(self, state_dir: Path | None) -> None:
        if state_dir is None:
            return
        self.state_dir = state_dir
        self.marks = {name: position(state_dir / name) for name in SERVICE_LOG_NAMES}

    def capture(self, step: str) -> None:
        if self.marks is None or self.state_dir is None:
            return
        for name in SERVICE_LOG_NAMES:
            path = self.state_dir / name
            now = position(path)
            if now is None:
                continue
            before = self.marks.get(name)
            start = before[1] if before and before[0] == now[0] and before[1] <= now[1] else 0
            self.marks[name] = now
            if now[1] <= start:
                continue
            self.copy(step, name, path, start, now[1])

    def copy(self, step: str, name: str, path: Path, start: int, end: int) -> None:
        begin = max(start, end - MAX_SERVICE_LOG_BYTES)
        try:
            with path.open("rb") as handle:
                handle.seek(begin)
                data = handle.read(end - begin)
        except OSError:
            return
        text = redact_auth_material(data.decode("utf-8", "replace"))
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_SERVICE_LOG_BYTES:
            encoded = encoded[-MAX_SERVICE_LOG_BYTES:]
        truncated = begin > start or len(encoded) < len(text.encode("utf-8"))
        header = (
            f"[{begin - start} earlier bytes of this window not copied]\n".encode()
            if truncated
            else b""
        )
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", step).strip("-") or "step"
        index = len(self.copies) + 1
        target = self.target / f"service-{index:02d}-{slug}-{name}.log"
        target.write_bytes(header + encoded)
        self.copies.append(
            {
                "step": step,
                "source": name,
                "fromByte": start,
                "toByte": end,
                "truncated": truncated,
                "path": str(target),
            }
        )
