"""Durable keep-list for conservative agent inactivity reporting."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from hyprial.kernel import atomic_json_write


class AgentKeepListError(ValueError):
    """The keep-list document is malformed."""


class AgentKeepList:
    """A small, atomically replaced JSON list under the daemon state root."""

    def __init__(self, path: Path, *, normalize: Callable[[str], str]) -> None:
        self.path = Path(path)
        self._normalize = normalize
        self._lock = threading.RLock()

    def list(self) -> tuple[str, ...]:
        with self._lock:
            if not self.path.exists():
                return ()
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise AgentKeepListError(
                    f"cannot read agent keep-list {self.path}: {error}"
                ) from error
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise AgentKeepListError("agent keep-list must be a JSON string array")
            try:
                return tuple(sorted({self._normalize(item) for item in value}))
            except (ValueError, RuntimeError) as error:
                raise AgentKeepListError(f"invalid agent keep-list entry: {error}") from error

    def add(self, actor: str) -> bool:
        name = self._normalize(actor)
        with self._lock:
            current = set(self.list())
            if name in current:
                return False
            current.add(name)
            atomic_json_write(self.path, sorted(current))
            return True

    def remove(self, actor: str) -> bool:
        name = self._normalize(actor)
        with self._lock:
            current = set(self.list())
            if name not in current:
                return False
            current.remove(name)
            atomic_json_write(self.path, sorted(current))
            return True


__all__ = ["AgentKeepList", "AgentKeepListError"]
