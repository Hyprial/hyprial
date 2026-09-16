"""Twice-daily latest-tag pull scheduling and legacy timer integration.

The daemon owns the active 03:17/15:17 calendar and executes the existing
``hyprial autoupdate run`` upgrade/restart path from a dedicated worker thread.
Legacy launchd/systemd integration remains here only for the staged cutover:
while its unit still exists, firing it delegates back to the live daemon.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol
from xml.sax.saxutils import escape as xml_escape

from hyprial import updates
from hyprial.home import configured_hyprial_home

AUTOUPDATE_LABEL = "com.hyprial.hyprial.autoupdate"
SYSTEMD_SERVICE_UNIT = "hyprial-autoupdate.service"
SYSTEMD_TIMER_UNIT = "hyprial-autoupdate.timer"
LAST_RUN_FILENAME = "autoupdate-last.json"
LOG_FILENAME = "autoupdate.log"
AUTOUPDATE_CHILD_ENV = "HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD"
AUTOUPDATE_TRIGGER_ENV = "HYPRIAL_AUTOUPDATE_TRIGGER"
CALENDAR_RECHECK_SECONDS = 30.0
# Records that this install enabled systemd lingering itself, so uninstall
# (and a failed install) can restore the prior account policy without touching
# linger that the operator enabled for unrelated services.
LINGER_OWNERSHIP_FILENAME = "autoupdate-linger-owned"

# Fixed twice-daily schedule (local time), deliberately off round hours so
# fleet-wide installs do not stampede the Git remote at the same instant.
SCHEDULE: tuple[tuple[int, int], ...] = ((3, 17), (15, 17))

Platform = Literal["launchd", "systemd"]
Json = dict[str, Any]


class TimerCommandRunner(Protocol):
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
            candidate = day.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate > now:
                return candidate
    raise AssertionError("twice-daily schedule did not produce a future point")


class InProcessAutoUpdateScheduler:
    """Daemon-owned calendar trigger running wholly off the accept flow.

    The worker starts the existing hidden ``autoupdate run`` entry point in a
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
                        self._last_skip_reason = (
                            updates.AUTOUPGRADE_DISABLED_REASON
                        )
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
                    "autoupdate",
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


def detect_platform() -> Platform | None:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return None


@dataclass(frozen=True, slots=True)
class TimerConfig:
    executable: Path
    home: Path
    hyprial_home: Path
    state_dir: Path
    path_env: str


@dataclass(frozen=True, slots=True)
class AutoUpdateStatus:
    platform: Platform
    unit: str
    unit_paths: tuple[Path, ...]
    installed: bool
    loaded: bool
    # systemd only: UnitFileState == enabled.  A timer can be loaded but
    # disabled (manual systemctl disable), in which case it never fires.
    enabled: bool | None = None
    last_run: Json | None = None
    boot_persistent: bool | None = None
    changed: bool | None = None


class TimerTemplates:
    """Package-data launchd/systemd units for the update timer."""

    def render_launchd(self, config: TimerConfig) -> str:
        return self._render(
            self._read("services/launchd.plist.template"),
            {
                "LABEL": AUTOUPDATE_LABEL,
                "EXECUTABLE": str(config.executable.resolve()),
                "HOME": str(config.home.resolve()),
                "PATH": config.path_env,
                "HYPRIAL_HOME": str(config.hyprial_home.resolve()),
                "STDOUT_LOG": str((config.state_dir / LOG_FILENAME).resolve()),
                "STDERR_LOG": str((config.state_dir / LOG_FILENAME).resolve()),
                "HOUR_A": str(SCHEDULE[0][0]),
                "MINUTE_A": f"{SCHEDULE[0][1]:02d}",
                "HOUR_B": str(SCHEDULE[1][0]),
                "MINUTE_B": f"{SCHEDULE[1][1]:02d}",
            },
            self._escape_xml,
        )

    def render_systemd_service(self, config: TimerConfig) -> str:
        return self._render(
            self._read("services/systemd.service.template"),
            {
                "EXECUTABLE_ARG": self._quote_systemd(str(config.executable.resolve())),
                "HOME_ENV": self._quote_systemd(f"HOME={config.home.resolve()}"),
                "PATH_ENV": self._quote_systemd(f"PATH={config.path_env}"),
                "HYPRIAL_HOME_ENV": self._quote_systemd(
                    f"HYPRIAL_HOME={config.hyprial_home.resolve()}"
                ),
                "STDOUT_ARG": self._quote_systemd(
                    str((config.state_dir / LOG_FILENAME).resolve())
                ),
                "STDERR_ARG": self._quote_systemd(
                    str((config.state_dir / LOG_FILENAME).resolve())
                ),
            },
            lambda value: value,
        )

    def render_systemd_timer(self, config: TimerConfig) -> str:
        return self._render(
            self._read("services/systemd.timer.template"),
            {
                "SERVICE_UNIT": SYSTEMD_SERVICE_UNIT,
                "CALENDAR_A": self._calendar(SCHEDULE[0]),
                "CALENDAR_B": self._calendar(SCHEDULE[1]),
            },
            lambda value: value,
        )

    @staticmethod
    def _calendar(point: tuple[int, int]) -> str:
        return f"{point[0]:02d}:{point[1]:02d}:00"

    @staticmethod
    def _read(name: str) -> str:
        return files("hyprial.autoupdate").joinpath(name).read_text(encoding="utf-8")

    @staticmethod
    def _render(
        template: str,
        replacements: dict[str, str],
        escape: Callable[[str], str],
    ) -> str:
        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in replacements:
                raise ValueError(f"unknown template token {key}")
            return escape(replacements[key])

        rendered = re.sub(r"\{\{([A-Z0-9_]+)\}\}", substitute, template)
        remaining = re.findall(r"\{\{[^}]+\}\}", rendered)
        if remaining:
            raise ValueError(f"unresolved template tokens: {remaining}")
        return rendered

    @staticmethod
    def _escape_xml(value: str) -> str:
        return xml_escape(value, {'"': "&quot;", "'": "&apos;"})

    @staticmethod
    def _quote_systemd(value: str) -> str:
        return (
            '"'
            + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )


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


class AutoUpdateManager:
    """Idempotent user-timer integration for the latest-tag pull."""

    def __init__(
        self,
        config: TimerConfig,
        *,
        platform: Platform,
        service_home: Path | None = None,
        runner: TimerCommandRunner | None = None,
        uid: int | None = None,
        username: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.platform = platform
        self.service_home = Path(service_home or config.home)
        self.runner = runner or self._run
        self.uid = os.getuid() if uid is None else uid
        self.username = username or os.environ.get("USER", "")
        self.sleep = sleep
        self.templates = TimerTemplates()

    @property
    def unit_paths(self) -> tuple[Path, ...]:
        if self.platform == "launchd":
            return (
                self.service_home
                / "Library"
                / "LaunchAgents"
                / f"{AUTOUPDATE_LABEL}.plist",
            )
        base = self.service_home / ".config" / "systemd" / "user"
        return (base / SYSTEMD_SERVICE_UNIT, base / SYSTEMD_TIMER_UNIT)

    @property
    def unit(self) -> str:
        return AUTOUPDATE_LABEL if self.platform == "launchd" else SYSTEMD_TIMER_UNIT

    def status(self) -> AutoUpdateStatus:
        installed = all(path.is_file() for path in self.unit_paths)
        loaded = self._loaded()
        return AutoUpdateStatus(
            self.platform,
            self.unit,
            self.unit_paths,
            installed,
            loaded,
            enabled=(
                self._systemd_enabled() if self.platform == "systemd" else None
            ),
            last_run=read_last_run(self.config.state_dir),
            boot_persistent=(
                self._linger_enabled() if self.platform == "systemd" else None
            ),
        )

    def install(self) -> AutoUpdateStatus:
        before = self.status()
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        linger_changed = False
        if self.platform == "systemd" and not before.boot_persistent:
            self._require(("loginctl", "enable-linger", self.username))
            # Record ownership before any later step can fail, so a stranded
            # half-install can still be rolled back (and uninstall can tell
            # linger we own from linger the operator enabled themselves).
            self._write_linger_marker()
            linger_changed = True
        try:
            changed_file = False
            for path, rendered in self._rendered_units():
                existing = (
                    path.read_text(encoding="utf-8") if path.is_file() else None
                )
                if existing != rendered:
                    changed_file = True
                    self._atomic_write(path, rendered)
            if self.platform == "launchd":
                if before.loaded and changed_file:
                    self._unload()
                if not before.loaded or changed_file:
                    self._load()
            elif changed_file:
                if before.loaded:
                    self._unload()
                self._load()
            elif not before.enabled:
                # A manually disabled timer stays loaded; install must repair
                # it, not silently report success.
                self._load()
        except BaseException:
            if linger_changed:
                self._restore_linger()
            raise
        after = self.status()
        repaired = (
            self.platform == "systemd"
            and before.enabled is False
            and after.enabled is True
        )
        return AutoUpdateStatus(
            **{
                field: getattr(after, field)
                for field in (
                    "platform",
                    "unit",
                    "unit_paths",
                    "installed",
                    "loaded",
                    "enabled",
                    "last_run",
                    "boot_persistent",
                )
            },
            changed=changed_file
            or (self.platform == "launchd" and not before.loaded)
            or repaired
            or linger_changed,
        )

    def uninstall(self) -> AutoUpdateStatus:
        before = self.status()
        if before.loaded:
            self._unload()
        for path in self.unit_paths:
            path.unlink(missing_ok=True)
        if self.platform == "systemd" and (before.loaded or before.installed):
            self._require(("systemctl", "--user", "daemon-reload"))
        if self.platform == "systemd" and self._linger_marker_path().is_file():
            self._restore_linger()
        after = self.status()
        return AutoUpdateStatus(
            **{
                field: getattr(after, field)
                for field in (
                    "platform",
                    "unit",
                    "unit_paths",
                    "installed",
                    "loaded",
                    "enabled",
                    "last_run",
                    "boot_persistent",
                )
            },
            changed=before.loaded or before.installed,
        )

    def _rendered_units(self) -> list[tuple[Path, str]]:
        if self.platform == "launchd":
            return [
                (self.unit_paths[0], self.templates.render_launchd(self.config))
            ]
        return [
            (self.unit_paths[0], self.templates.render_systemd_service(self.config)),
            (self.unit_paths[1], self.templates.render_systemd_timer(self.config)),
        ]

    def _loaded(self) -> bool:
        if self.platform == "launchd":
            result = self.runner(("launchctl", "print", f"gui/{self.uid}/{AUTOUPDATE_LABEL}"))
            return result.returncode == 0
        result = self.runner(
            (
                "systemctl",
                "--user",
                "show",
                SYSTEMD_TIMER_UNIT,
                "--property=LoadState",
            )
        )
        return (
            result.returncode == 0
            and "LoadState=loaded" in result.stdout
        )

    def _linger_enabled(self) -> bool:
        result = self.runner(
            ("loginctl", "show-user", self.username, "--property=Linger", "--value")
        )
        return result.returncode == 0 and result.stdout.strip() == "yes"

    def _systemd_enabled(self) -> bool:
        result = self.runner(
            (
                "systemctl",
                "--user",
                "show",
                SYSTEMD_TIMER_UNIT,
                "--property=UnitFileState",
            )
        )
        return (
            result.returncode == 0
            and "UnitFileState=enabled" in result.stdout
        )

    def _linger_marker_path(self) -> Path:
        return self.config.state_dir / LINGER_OWNERSHIP_FILENAME

    def _write_linger_marker(self) -> None:
        path = self._linger_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text("enabled-by-hyprial-autoupdate\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def _restore_linger(self) -> None:
        """Best-effort rollback of linger we enabled.

        The marker is removed only when disable-linger succeeds, so a failed
        rollback can be retried by a later uninstall instead of stranding the
        account-level policy change without any record of it.
        """

        result = self.runner(("loginctl", "disable-linger", self.username))
        if result.returncode == 0:
            self._linger_marker_path().unlink(missing_ok=True)

    def _load(self) -> None:
        if self.platform == "launchd":
            command = (
                "launchctl",
                "bootstrap",
                f"gui/{self.uid}",
                str(self.unit_paths[0]),
            )
            for attempt in range(5):
                result = self.runner(command)
                if result.returncode == 0:
                    return
                transient = (
                    result.returncode == 5 or "Input/output error" in result.stderr
                )
                if not transient or attempt == 4:
                    self._raise_command(command, result)
                self.sleep(0.25 * (attempt + 1))
            return
        # enable-linger starts the user manager asynchronously; on hosts
        # without a login session (headless servers, WSL) the user bus can
        # take a moment to come up, so wait for it briefly before reloading.
        deadline = time.monotonic() + 5.0
        while True:
            result = self.runner(("systemctl", "--user", "daemon-reload"))
            if result.returncode == 0:
                break
            if (
                "Failed to connect to bus" not in result.stderr
                or time.monotonic() >= deadline
            ):
                self._raise_command(("systemctl", "--user", "daemon-reload"), result)
            self.sleep(0.5)
        self._require(
            ("systemctl", "--user", "enable", "--now", SYSTEMD_TIMER_UNIT)
        )

    def _unload(self) -> None:
        if self.platform == "launchd":
            self._require(("launchctl", "bootout", f"gui/{self.uid}/{AUTOUPDATE_LABEL}"))
            return
        self._require(("systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_UNIT))

    def _atomic_write(self, path: Path, rendered: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        temporary.write_text(rendered, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)

    def _require(self, command: Sequence[str]) -> None:
        result = self.runner(command)
        if result.returncode != 0:
            self._raise_command(command, result)

    @staticmethod
    def _raise_command(
        command: Sequence[str], result: subprocess.CompletedProcess[str]
    ) -> None:
        raise RuntimeError(
            f"service command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip()}"
        )

    @staticmethod
    def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as error:
            return subprocess.CompletedProcess(command, 127, "", str(error))
