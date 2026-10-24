"""Twice-daily latest-tag pull scheduling owned by the daemon.

The daemon owns the active 03:17/15:17 calendar and executes the hidden
``hyprial daemon upgrade run`` path from a dedicated worker thread.  Legacy
launchd/systemd units are retired by the versioned ``autoupdate-timer-v1``
upgrade step, not by daemon startup.
"""

from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import hyprial.daemon.impl.updates as updates
from hyprial.kernel import configured_hyprial_home

AUTOUPDATE_LABEL = "com.hyprial.hyprial.autoupdate"
SYSTEMD_SERVICE_UNIT = "hyprial-autoupdate.service"
SYSTEMD_TIMER_UNIT = "hyprial-autoupdate.timer"
LAST_RUN_FILENAME = "autoupdate-last.json"
LOG_FILENAME = "autoupdate.log"
AUTOUPDATE_CHILD_ENV = "HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD"
AUTOUPDATE_TRIGGER_ENV = "HYPRIAL_AUTOUPDATE_TRIGGER"
CALENDAR_RECHECK_SECONDS = 30.0

# Fixed twice-daily schedule (local time), deliberately off round hours so
# fleet-wide installs do not stampede the Git remote at the same instant.
SCHEDULE: tuple[tuple[int, int], ...] = ((3, 17), (15, 17))

Json = dict[str, Any]


class LegacyTimerCommandRunner(Protocol):
    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


class UpdateCommandRunner(Protocol):
    def __call__(
        self, command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]: ...


def next_scheduled_run(now: datetime) -> datetime:
    """Return the next strict-future 03:17/15:17 point in ``now``'s zone."""

    for day_offset in (0, 1):
        day = now + timedelta(days=day_offset)
        for hour, minute in SCHEDULE:
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                return candidate
    raise AssertionError("twice-daily schedule did not produce a future point")


class InProcessAutoUpdateScheduler:
    """Daemon-owned calendar trigger running wholly off the accept flow.

    The worker starts the hidden ``daemon upgrade run`` entry point in a
    child.  That preserves the proven upgrade/restart implementation while the
    child inherits the daemon's complete environment instead of a separately
    rendered launchd/systemd environment.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        runner: UpdateCommandRunner | None = None,
        now: Callable[[], datetime] | None = None,
        logger: Callable[..., None] | None = None,
        calendar_recheck_seconds: float = CALENDAR_RECHECK_SECONDS,
        hyprial_home: Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.runner = runner or subprocess.run
        # A naive local wall clock intentionally mirrors StartCalendarInterval
        # and systemd OnCalendar.  Re-reading it below makes suspend/resume,
        # DST, and operator clock changes visible instead of freezing today's
        # UTC offset into a twelve-hour monotonic wait.
        self.now = now or datetime.now
        self.logger = logger
        if calendar_recheck_seconds <= 0:
            raise ValueError("calendar_recheck_seconds must be positive")
        self.calendar_recheck_seconds = calendar_recheck_seconds
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active = False
        self._pending = False
        self._next_run: datetime | None = None
        self._last_triggered: datetime | None = None
        self._last_reason: str | None = None
        self._last_exit_code: int | None = None
        self._last_skip_reason: str | None = None
        # Guard 1 (spec autoupdate-isolated-home r1, Allen 2026-09-15):
        # automatic upgrades are OFF unless settings.json explicitly sets
        # ``autoUpgrade: true`` (``hyprial config set autoUpgrade true``).
        # The switch is re-read at every fire (never frozen at
        # construction), so an operator's `config set` takes effect on the
        # next scheduled point without a daemon restart, and turning it off
        # stops runs just as promptly.  The home the settings file is read
        # from: the constructor argument when given, else the environment's
        # configured home -- resolved lazily at fire time so a constructor
        # that forgets the argument cannot silently allow upgrades.
        self._hyprial_home = Path(hyprial_home) if hyprial_home is not None else None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="hyprial-autoupdate",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)

    def trigger(self, reason: str = "manual") -> bool:
        with self._lock:
            if self._active or self._pending or self._stop.is_set():
                return False
            self._pending = True
        try:
            self._queue.put_nowait(reason)
        except queue.Full:
            with self._lock:
                self._pending = False
            return False
        return True

    def status(self) -> Json:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "active": self._active,
                "pending": self._pending,
                "nextRunAt": (
                    self._next_run.isoformat() if self._next_run is not None else None
                ),
                "lastTriggeredAt": (
                    self._last_triggered.isoformat()
                    if self._last_triggered is not None
                    else None
                ),
                "lastTrigger": self._last_reason,
                "lastExitCode": self._last_exit_code,
                "autoUpgradeEnabled": self._auto_upgrade_enabled(),
                "lastSkipReason": self._last_skip_reason,
                "lastRun": read_last_run(self.state_dir),
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            now = self.now()
            scheduled = next_scheduled_run(now)
            with self._lock:
                self._next_run = scheduled
            reason: str | None = None
            while reason is None and not self._stop.is_set():
                remaining = (scheduled - self.now()).total_seconds()
                if remaining <= 0:
                    reason = "schedule"
                    break
                try:
                    reason = self._queue.get(
                        timeout=min(remaining, self.calendar_recheck_seconds)
                    )
                except queue.Empty:
                    # Re-read the local wall clock.  In particular, a machine
                    # that slept through 03:17/15:17 runs once after wake.
                    continue
            if reason is None or self._stop.is_set():
                break
            with self._lock:
                self._pending = False
                if self._active:
                    continue
                self._active = True
                self._last_triggered = self.now()
                self._last_reason = reason
            try:
                if not self._auto_upgrade_enabled():
                    # Guard 1: record the skip loudly and durably, then do
                    # nothing.  No child, no autoupdate.log entry, no
                    # last-run record -- a disabled autoupdate must not
                    # leave any footprint beyond this one event.
                    with self._lock:
                        self._last_exit_code = None
                        self._last_skip_reason = updates.AUTOUPGRADE_DISABLED_REASON
                    self._emit(
                        "warning",
                        "autoupdate.run.skipped",
                        trigger=reason,
                        reason=updates.AUTOUPGRADE_DISABLED_REASON,
                    )
                else:
                    self._emit("info", "autoupdate.run.started", trigger=reason)
                    completed = self._execute(reason)
                    with self._lock:
                        self._last_exit_code = completed.returncode
                    self._emit(
                        "info" if completed.returncode == 0 else "error",
                        "autoupdate.run.completed",
                        trigger=reason,
                        exitCode=completed.returncode,
                    )
            except Exception as error:  # noqa: BLE001 - scheduler must survive
                with self._lock:
                    self._last_exit_code = None
                self._emit(
                    "error",
                    "autoupdate.run.failed",
                    trigger=reason,
                    errorType=type(error).__name__,
                )
            finally:
                with self._lock:
                    self._active = False

    def _auto_upgrade_enabled(self) -> bool:
        """Read the autoUpgrade switch from the home this scheduler serves."""

        home = self._hyprial_home
        if home is None:
            home, _source = configured_hyprial_home()
        return updates.auto_upgrade_enabled(home)

    def _execute(self, reason: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment[AUTOUPDATE_CHILD_ENV] = "1"
        environment[AUTOUPDATE_TRIGGER_ENV] = reason
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = self.state_dir / LOG_FILENAME
        with log_path.open("a", encoding="utf-8") as stream:
            os.chmod(log_path, 0o600)
            return self.runner(
                [
                    sys.executable,
                    "-m",
                    "hyprial.cli",
                    "daemon",
                    "upgrade",
                    "run",
                    "--json",
                ],
                text=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
                env=environment,
            )

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if self.logger is None:
            return
        try:
            self.logger(level, event, **fields)
        except OSError:
            pass


def _run_legacy_timer_command(
    command: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def _require_legacy_timer_command(
    runner: LegacyTimerCommandRunner, command: Sequence[str]
) -> None:
    result = runner(command)
    if result.returncode != 0:
        raise RuntimeError(
            f"legacy timer removal failed ({result.returncode}): "
            f"{' '.join(command)}: {result.stderr.strip()}"
        )


def _collapsed_absolute(path: Path) -> str:
    text = str(path)
    return "/" + text.lstrip("/") if text.startswith("/") else text


def _is_os_temp_alias(supplied: Path, canonical: Path) -> bool:
    if not supplied.is_absolute():
        return False
    text = os.path.normpath(_collapsed_absolute(supplied))
    resolved = str(canonical)
    if text == "/tmp":
        return resolved == "/private/tmp"
    if text.startswith("/tmp/"):
        return resolved == "/private" + text
    if text == "/var":
        return resolved == "/private/var"
    if text.startswith("/var/"):
        return resolved == "/private" + text
    return False


def canonical_timer_home(home: Path) -> tuple[Path, str | None]:
    """Canonicalize HOME once; only the OS temp aliases are accepted."""

    supplied = Path(home)
    if not supplied.is_absolute():
        return Path(os.path.realpath(supplied)), f"relative HOME refused: {supplied}"
    if ".." in supplied.parts:
        return Path(os.path.realpath(supplied)), f"dotdot HOME refused: {supplied}"
    canonical = Path(os.path.realpath(supplied))
    collapsed = Path(_collapsed_absolute(supplied))
    if Path(os.path.abspath(collapsed)) != canonical and not _is_os_temp_alias(
        collapsed, canonical
    ):
        return canonical, f"aliased HOME path: {supplied} resolves to {canonical}"
    return canonical, None


def legacy_timer_path_problem(path: Path, home: Path) -> str | None:
    """Return why an exact timer path is unsafe to touch, or None."""

    candidate = Path(path)
    root, home_problem = canonical_timer_home(home)
    if home_problem is not None:
        return home_problem
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "timer path is outside canonical HOME"
    if ".." in relative.parts:
        return f"aliased timer path: {candidate}"
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as error:
            return f"cannot inspect {current}: {error}"
        if stat.S_ISLNK(info.st_mode):
            return f"symlinked ancestor: {current}"
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"cannot inspect {candidate}: {error}"
    if stat.S_ISLNK(info.st_mode):
        return f"symlink at exact path: {candidate}"
    if not stat.S_ISREG(info.st_mode):
        return f"not a regular file: {candidate}"
    if info.st_nlink != 1:
        return f"multiply linked file ({info.st_nlink} links): {candidate}"
    if info.st_uid != os.getuid():
        return f"not owned by current user: {candidate}"
    if info.st_mode & 0o002:
        return f"world-writable timer file: {candidate}"
    return None


def remove_legacy_timer_units(
    *,
    home: Path,
    platform: Literal["launchd", "systemd"] | None = None,
    runner: LegacyTimerCommandRunner | None = None,
    uid: int | None = None,
) -> tuple[Path, ...]:
    """Unload and remove timer units left by pre-daemon scheduling releases.

    Detection is file-based and therefore has no side effects when no legacy
    installation exists.  Unload happens before unlinking so a failed service
    command leaves the unit files intact for the next startup and for operator
    diagnosis instead of silently stranding an active timer.
    """

    selected = platform
    if selected is None:
        if sys.platform == "darwin":
            selected = "launchd"
        elif sys.platform.startswith("linux"):
            selected = "systemd"
        else:
            return ()
    command_runner = runner or _run_legacy_timer_command
    service_home, home_problem = canonical_timer_home(Path(home))
    if home_problem is not None:
        raise RuntimeError(home_problem)
    if selected == "launchd":
        paths = (
            service_home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist",
        )
    else:
        unit_dir = service_home / ".config" / "systemd" / "user"
        paths = (
            unit_dir / SYSTEMD_SERVICE_UNIT,
            unit_dir / SYSTEMD_TIMER_UNIT,
        )
    for path in paths:
        if path.exists() or path.is_symlink():
            problem = legacy_timer_path_problem(path, service_home)
            if problem is not None:
                raise RuntimeError(problem)
    existing = tuple(
        path for path in paths if path.is_file() and not path.is_symlink()
    )
    if not existing:
        return ()

    if selected == "launchd":
        domain = f"gui/{os.getuid() if uid is None else uid}"
        probe = command_runner(("launchctl", "print", f"{domain}/{AUTOUPDATE_LABEL}"))
        if probe.returncode == 0:
            _require_legacy_timer_command(
                command_runner,
                ("launchctl", "bootout", f"{domain}/{AUTOUPDATE_LABEL}"),
            )
        elif probe.returncode != 113 and "Could not find service" not in probe.stderr:
            raise RuntimeError(
                f"legacy timer removal failed ({probe.returncode}): "
                f"launchctl print {domain}/{AUTOUPDATE_LABEL}: {probe.stderr.strip()}"
            )
    elif paths[1].is_file():
        _require_legacy_timer_command(
            command_runner,
            ("systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_UNIT),
        )

    for path in existing:
        path.unlink()
    if selected == "systemd":
        _require_legacy_timer_command(
            command_runner, ("systemctl", "--user", "daemon-reload")
        )
    return existing


def write_last_run(state_dir: Path, record: Json) -> Path:
    """Atomically persist the most recent timer run result."""

    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / LAST_RUN_FILENAME
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"cannot write last run {path}: {error}") from error
    return path


def read_last_run(state_dir: Path) -> Json | None:
    """Read the last-run record; unreadable/corrupt state becomes a visible
    ``{"ok": false, ...}`` record instead of a silent ``null``."""

    path = Path(state_dir) / LAST_RUN_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        return {"ok": False, "error": f"cannot read last run {path}: {error}"}
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        return {"ok": False, "error": f"last run {path} is not valid JSON: {error}"}
    if not isinstance(record, dict):
        return {
            "ok": False,
            "error": f"last run {path} is not an object: {type(record).__name__}",
        }
    return record
