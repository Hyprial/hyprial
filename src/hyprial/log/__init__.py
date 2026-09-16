"""Shared structured logging for every Harness Bridge component.

``Logger`` owns the JSONL schema, redaction, permissions, routing, and append
serialization.  Components choose a scope once and emit events; they do not
open log files themselves.

Rotation is intentionally a policy hook for now.  ``rotation_hook`` is called
while the destination is exclusively locked, immediately before each append.
It may rotate the file and return; the logger then reopens and appends the
record to the canonical route.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

LogLevel = Literal["debug", "info", "warn", "error"]
LogRoute = Literal["daemon", "adapter", "worker", "component"]
RotationHook = Callable[[Path], None]

_LEVELS = frozenset({"debug", "info", "warn", "error"})
_RESERVED_FIELDS = frozenset({"ts", "level", "component", "name", "event"})
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")
_SENSITIVE_KEY = re.compile(
    r"(?:secret|token|password|passwd|authorization|credential|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_URL = re.compile(r"\b(?:https?|wss?)://[^\s<>'\"]+", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"(\bauthorization\s*[:=]\s*)[^\r\n,;]+", re.IGNORECASE
)
_BEARER_OR_TOKEN = re.compile(
    r"\b(bearer|token)\s+[^\s,;]+", re.IGNORECASE
)
_NAMED_CREDENTIAL = re.compile(
    r"((?:[\"']?)[a-z0-9_-]*(?:api[_-]?key|access[_-]?key|token|secret|"
    r"password|passwd|credential|authorization)[a-z0-9_-]*(?:[\"']?)"
    r"\s*[:=]\s*)"
    r'(?:"(?:\\.|[^"\\\r\n])*"|\'(?:\\.|[^\'\\\r\n])*\'|[^\s,;]+)',
    re.IGNORECASE,
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)

_thread_locks_guard = threading.Lock()
_thread_locks: dict[Path, threading.RLock] = {}

PRE_TRAJECTORY_ARCHIVE = "archive-pre-trajectory"
PRE_TRAJECTORY_MARKER = ".migrated"
_PRE_TRAJECTORY_PLAN = ".migration-in-progress"


@dataclass(frozen=True, slots=True)
class LogMigrationResult:
    """One completed, marker-backed pre-trajectory log migration."""

    archive: Path
    marker: Path
    files: tuple[str, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)


def _reset_thread_locks_after_fork() -> None:
    global _thread_locks_guard, _thread_locks
    _thread_locks_guard = threading.Lock()
    _thread_locks = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_thread_locks_after_fork)


def _thread_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.RLock())


def _safe_filename(value: str) -> str:
    sanitized = _UNSAFE_FILENAME.sub("_", value).strip(".")
    return sanitized or "unnamed"


def _redact_text(value: str) -> str:
    value = _URL.sub("[REDACTED_URL]", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = _BEARER_OR_TOKEN.sub(r"\1 [REDACTED]", value)
    value = _NAMED_CREDENTIAL.sub(r"\1[REDACTED]", value)
    return _JWT.sub("[REDACTED_TOKEN]", value)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively remove credentials and URL-shaped values from log data."""

    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return {
            _redact_text(str(item_key)): redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def route_path(
    state_dir: Path,
    *,
    route: LogRoute,
    component: str,
    name: str,
    runtime: str | None = None,
) -> Path:
    """Return the one canonical destination for a logger scope."""

    logs = Path(state_dir) / "logs"
    if route == "daemon":
        return logs / "daemon.jsonl"
    if route == "adapter":
        return logs / "lark-gateway.jsonl"
    if route == "worker":
        if not runtime:
            raise ValueError("worker log routing requires a runtime")
        return logs / "workers" / f"{_safe_filename(runtime)}-{_safe_filename(name)}.jsonl"
    return logs / f"{_safe_filename(component)}.jsonl"


def _migration_timestamp() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write_migration_metadata(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _migration_plan_files(plan: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid pre-trajectory migration plan {plan}") from error
    raw_files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw_files, list) or not all(
        isinstance(item, str) for item in raw_files
    ):
        raise ValueError(f"invalid pre-trajectory migration plan {plan}")
    files = tuple(raw_files)
    for item in files:
        relative = Path(item)
        if (
            not item
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix != ".jsonl"
        ):
            raise ValueError(f"invalid pre-trajectory migration plan {plan}")
    return files


def migrate_pre_trajectory_logs(state_dir: Path) -> LogMigrationResult | None:
    """Archive every active JSONL once before the upgraded daemon logs.

    The daemon's process lock serializes this startup operation.  A missing
    marker always completes the migration, including on a fresh home with no
    logs, so a later restart can never mistake new-contract logs for legacy
    input.  If an interrupted attempt moved only some files, the absent marker
    makes the next startup resume and record the complete archive inventory.
    """

    logs_dir = Path(state_dir) / "logs"
    archive = logs_dir / PRE_TRAJECTORY_ARCHIVE
    marker = archive / PRE_TRAJECTORY_MARKER
    plan = archive / _PRE_TRAJECTORY_PLAN
    if marker.exists():
        return None

    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(logs_dir, 0o700)
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(archive, 0o700)
    resuming = plan.exists()
    if resuming:
        files = _migration_plan_files(plan)
    else:
        files = tuple(
            path.relative_to(logs_dir).as_posix()
            for path in sorted(logs_dir.rglob("*.jsonl"))
            if path.is_file() and archive not in path.parents
        )
        collisions = tuple(
            relative_name
            for relative_name in files
            if (archive / relative_name).exists()
        )
        if collisions:
            raise FileExistsError(
                "refusing to overwrite existing archived logs: "
                + ", ".join(collisions)
            )
        _write_migration_metadata(
            plan,
            {"schemaVersion": 1, "startedAt": _migration_timestamp(), "files": files},
        )

    for relative_name in files:
        relative = Path(relative_name)
        source = logs_dir / relative
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if destination.exists():
            if resuming:
                # A failed startup may already have archived the old file and
                # then recreated this active path with new-contract records.
                # The persisted plan makes that new file out of scope.
                continue
            raise FileExistsError(
                f"refusing to overwrite existing archived log {destination}"
            )
        if not source.is_file():
            raise FileNotFoundError(f"planned log disappeared before migration: {source}")
        source.replace(destination)

    payload = {
        "schemaVersion": 1,
        "migratedAt": _migration_timestamp(),
        "files": list(files),
    }
    _write_migration_metadata(marker, payload)
    plan.unlink(missing_ok=True)
    return LogMigrationResult(archive=archive, marker=marker, files=files)


class Logger:
    """A component/name scoped, secret-safe JSONL logger.

    Prefer :meth:`daemon`, :meth:`adapter`, and :meth:`worker`; they make the
    routing policy explicit at the call site while keeping every path decision
    in this module.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        component: str,
        name: str,
        route: LogRoute = "component",
        runtime: str | None = None,
        rotation_hook: RotationHook | None = None,
    ) -> None:
        if not component or not name:
            raise ValueError("logger component and name are required")
        self.state_dir = Path(state_dir)
        self.component = component
        self.name = name
        self.route = route
        self.runtime = runtime
        self.rotation_hook = rotation_hook
        self.path = route_path(
            self.state_dir,
            route=route,
            component=component,
            name=name,
            runtime=runtime,
        )

    @classmethod
    def daemon(
        cls, state_dir: Path, *, name: str, rotation_hook: RotationHook | None = None
    ) -> Self:
        return cls(
            state_dir,
            component="daemon",
            name=name,
            route="daemon",
            rotation_hook=rotation_hook,
        )

    @classmethod
    def adapter(
        cls, state_dir: Path, *, name: str, rotation_hook: RotationHook | None = None
    ) -> Self:
        return cls(
            state_dir,
            component="lark-adapter",
            name=name,
            route="adapter",
            rotation_hook=rotation_hook,
        )

    @classmethod
    def worker(
        cls,
        state_dir: Path,
        *,
        runtime: str,
        name: str,
        rotation_hook: RotationHook | None = None,
    ) -> Self:
        return cls(
            state_dir,
            component=f"{runtime}-worker",
            name=name,
            route="worker",
            runtime=runtime,
            rotation_hook=rotation_hook,
        )

    def bind(self, *, component: str | None = None, name: str | None = None) -> Self:
        """Create another scope that retains this logger's file route."""

        return type(self)(
            self.state_dir,
            component=component or self.component,
            name=name or self.name,
            route=self.route,
            runtime=self.runtime,
            rotation_hook=self.rotation_hook,
        )

    def log(self, level: LogLevel, event: str, **fields: Any) -> None:
        if level not in _LEVELS:
            raise ValueError(f"unsupported log level: {level}")
        if not event:
            raise ValueError("log event is required")
        overlap = _RESERVED_FIELDS.intersection(fields)
        if overlap:
            raise ValueError(
                f"reserved log fields cannot be overridden: {sorted(overlap)}"
            )
        entry = {
            "ts": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": level,
            "component": _redact_text(self.component),
            "name": _redact_text(self.name),
            "event": _redact_text(event),
            **{key: redact(value, key=key) for key, value in fields.items()},
        }
        if self.runtime is not None and "runtime" not in entry:
            entry["runtime"] = _redact_text(self.runtime)
        payload = (
            json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        self._append(payload)

    def debug(self, event: str, **fields: Any) -> None:
        self.log("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.log("info", event, **fields)

    def warn(self, event: str, **fields: Any) -> None:
        self.log("warn", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.log("error", event, **fields)

    def _append(self, payload: bytes) -> None:
        path = self.path
        lock_path = path.with_name(f".{path.name}.lock")
        with _thread_lock(path):
            logs_dir = self.state_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(logs_dir, 0o700)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            lock_fd = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                os.chmod(lock_path, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                if self.rotation_hook is not None:
                    self.rotation_hook(path)
                descriptor = os.open(
                    path,
                    os.O_APPEND
                    | os.O_CREAT
                    | os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    os.chmod(path, 0o600)
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("log append made no progress")
                        view = view[written:]
                finally:
                    os.close(descriptor)
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)


__all__ = [
    "LogLevel",
    "Logger",
    "LogMigrationResult",
    "PRE_TRAJECTORY_ARCHIVE",
    "PRE_TRAJECTORY_MARKER",
    "RotationHook",
    "migrate_pre_trajectory_logs",
    "redact",
    "route_path",
]
