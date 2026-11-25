"""Generation-scoped native session directories and OS writer locks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from hyprial.kernel import FileLockBusy, ProcessFileLock, harden_private_path


class SessionWriterHeld(RuntimeError):
    pass


def _path_segment(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\0" in value
        or len(value.encode()) > 255
    ):
        raise ValueError(f"invalid {label}")
    return value


def native_session_directory(
    state_dir: str | Path,
    kind: str,
    instance_id: str,
    generation: int,
) -> Path:
    if generation < 1:
        raise ValueError("generation must be positive")
    return (
        Path(state_dir)
        / "adapters"
        / _path_segment(kind, "kind")
        / _path_segment(instance_id, "instance_id")
        / str(generation)
        / "native-session"
    )


class NativeSessionWriterLock:
    """One process-owned flock held for a native session generation."""

    def __init__(
        self,
        state_dir: str | Path,
        kind: str,
        instance_id: str,
        generation: int,
    ) -> None:
        self.directory = native_session_directory(
            state_dir, kind, instance_id, generation
        )
        self.lock_path = self.directory / ".writer.lock"
        self._lock: ProcessFileLock | None = None

    @property
    def locked(self) -> bool:
        return self._lock is not None

    def acquire(self, *, blocking: bool = False) -> NativeSessionWriterLock:
        if blocking:
            raise ValueError("native session writer only supports nonblocking acquisition")
        if self._lock is not None:
            return self
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        harden_private_path(self.directory, mode=0o700)
        lock = ProcessFileLock(self.lock_path)
        try:
            lock.acquire(timeout=0.0)
        except FileLockBusy as error:
            raise SessionWriterHeld(f"native session writer held: {self.directory}") from error
        self._lock = lock
        return self

    def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def __enter__(self) -> NativeSessionWriterLock:
        return self.acquire(blocking=False)

    def __exit__(self, *_error: object) -> None:
        self.release()


def acquire_session_writer(value: Mapping[str, object]) -> dict[str, object]:
    """C0 state-transition seam; OS enforcement is NativeSessionWriterLock."""

    current_generation = value.get("currentGeneration")
    request_generation = value.get("requestGeneration")
    current_writer = value.get("currentWriter")
    request_writer = value.get("requestWriter")
    if current_writer and (
        request_writer != current_writer or request_generation != current_generation
    ):
        return {
            "ok": False,
            "code": "ADAPTER_SESSION_WRITER_HELD",
            "writers": 1,
            "generation": current_generation,
        }
    return {
        "ok": True,
        "code": None,
        "writers": 1,
        "generation": request_generation,
    }


__all__ = [
    "NativeSessionWriterLock",
    "SessionWriterHeld",
    "acquire_session_writer",
    "native_session_directory",
]
