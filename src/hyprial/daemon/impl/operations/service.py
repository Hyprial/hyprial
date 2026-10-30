"""Packaged launchd/systemd templates used by the CLI service manager.

A supervisor status probe has one authoritative "not loaded" shape per manager.
``launchctl print gui/$UID/com.hyprial.hyprial.daemon`` returns 113 and includes
``Could not find service "com.hyprial.hyprial.daemon" in domain for user gui: $UID``.
``systemctl --user show hyprial.service`` returns 4 and exactly
``Unit hyprial.service could not be found.`` on stderr. Every other non-zero
result is a manager command failure, including launchctl's exit-5 I/O failure
and systemctl's exit-5 bus failure.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol
from xml.sax.saxutils import escape as xml_escape

from hyprial.daemon.impl.configuration.launchd_jobs import (
    argv_runs_daemon as _argv_runs_daemon,
    read_launchd_plist as _read_launchd_plist,
    text_program_argv_runs_daemon as _text_program_argv_runs_daemon,
)
from hyprial.kernel import current_uid

LAUNCHD_LABEL = "com.hyprial.hyprial.daemon"
ServicePlatform = Literal["launchd", "systemd"]
SupervisionState = Literal["supervised", "unsupervised", "lost"]


class ServiceOwnershipError(RuntimeError):
    """The configured supervisor file is not owned by this Hyprial install."""

    def __init__(self, message: str, *, reason: str = "service-file-unattributed") -> None:
        super().__init__(message)
        self.reason = reason


class ServiceCommandError(RuntimeError):
    """A supervisor command could not produce a trustworthy result."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ServiceCommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    platform: ServicePlatform
    unit: str
    unit_path: Path
    installed: bool
    loaded: bool
    running: bool
    pid: int | None = None
    boot_persistent: bool | None = None
    changed: bool | None = None
    attributed: bool | None = None
    template_matches: bool | None = None
    serving_pid: int | None = None
    supervision: SupervisionState = "unsupervised"
    relaunch_count: int | None = None
    last_exit_code: int | None = None
    ownership_busy: bool = False
    ownership_reason: str | None = None
    backup_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    executable: Path
    home: Path
    hyprial_home: Path
    state_dir: Path
    # Includes ~/.local/bin (user-level installs: uv tools, pipx, codex shim)
    # for the same reason the autoupdate timer config does -- a service
    # environment without it cannot spawn those harness binaries.
    path_env: str = field(
        default_factory=lambda: (
            str(Path.home() / ".local" / "bin")
            + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        )
    )


class ServiceTemplates:
    def render_launchd(self, config: ServiceConfig) -> str:
        template = (
            files("hyprial.daemon.impl")
            .joinpath("services/launchd.plist.template")
            .read_text(encoding="utf-8")
        )
        replacements = {
            "LABEL": LAUNCHD_LABEL,
            "EXECUTABLE": str(config.executable.resolve()),
            "HOME": str(config.home.resolve()),
            "PATH": config.path_env,
            "HYPRIAL_HOME": str(config.hyprial_home.resolve()),
            "STATE_DIR": str(config.state_dir.resolve()),
            "STDOUT_LOG": str(
                (config.state_dir / "daemon-service.stdout.log").resolve()
            ),
            "STDERR_LOG": str(
                (config.state_dir / "daemon-service.stderr.log").resolve()
            ),
        }
        return self._replace(template, replacements, self._escape_xml)

    def render_systemd(self, config: ServiceConfig) -> str:
        template = (
            files("hyprial.daemon.impl")
            .joinpath("services/systemd.service.template")
            .read_text(encoding="utf-8")
        )
        replacements = {
            "EXECUTABLE_ARG": self._quote_systemd(str(config.executable.resolve())),
            "HOME_ENV": self._quote_systemd(f"HOME={config.home.resolve()}"),
            "PATH_ENV": self._quote_systemd(f"PATH={config.path_env}"),
            "HYPRIAL_HOME_ENV": self._quote_systemd(
                f"HYPRIAL_HOME={config.hyprial_home.resolve()}"
            ),
            "STATE_DIR_ENV": self._quote_systemd(
                f"HARNESS_STATE_DIR={config.state_dir.resolve()}"
            ),
            "STDOUT_ARG": self._quote_systemd(
                str((config.state_dir / "daemon-service.stdout.log").resolve())
            ),
            "STDERR_ARG": self._quote_systemd(
                str((config.state_dir / "daemon-service.stderr.log").resolve())
            ),
        }
        return self._replace(template, replacements, lambda value: value)

    @staticmethod
    def _replace(
        template: str,
        replacements: dict[str, str],
        escape: Callable[[str], str],
    ) -> str:
        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in replacements:
                raise ValueError(f"unknown service template token {key}")
            return escape(replacements[key])

        rendered = re.sub(r"\{\{([A-Z0-9_]+)\}\}", substitute, template)
        remaining = re.findall(r"\{\{[^}]+\}\}", rendered)
        if remaining:
            raise ValueError(f"unresolved service template tokens: {remaining}")
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


class ServiceManager:
    """Idempotent user-service integration matching the TS #45 contract."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        platform: ServicePlatform,
        service_home: Path | None = None,
        runner: ServiceCommandRunner | None = None,
        handoff_standalone: Callable[[], None] | None = None,
        uid: int | None = None,
        username: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        parent_pid: Callable[[int], int | None] | None = None,
    ) -> None:
        self.config = config
        self.platform = platform
        self.service_home = Path(service_home or config.home)
        self.runner = runner or self._run
        self.handoff_standalone = handoff_standalone or (lambda: None)
        self.uid = current_uid() if uid is None else uid
        self.username = username or os.environ.get("USER", "")
        self.sleep = sleep
        self.parent_pid = parent_pid or self._parent_pid
        self.templates = ServiceTemplates()

    @property
    def unit_path(self) -> Path:
        if self.platform == "launchd":
            return (
                self.service_home
                / "Library"
                / "LaunchAgents"
                / f"{LAUNCHD_LABEL}.plist"
            )
        return self.service_home / ".config" / "systemd" / "user" / "hyprial.service"

    def _launchd_jobs(self) -> list[tuple[str, Path]]:
        """LaunchAgents jobs that run THIS install's daemon, not just our label.

        Production ran an operator-written job named ``com.hyprial.daemon``
        (``bash -lc "ulimit -n 65536; exec .../hyprial daemon run --json"``)
        while our own label was never loaded, so keying the supervisor identity
        to the label made every restart spawn an unsupervised daemon.

        The identity is the parsed argv, matched exactly: the executable resolves
        to this install's executable and the subcommand is exactly ``daemon run``.
        A ``bash -lc`` wrapper is unwrapped first (the wrapper exists to raise the
        descriptor limit, not to run another program). Substring matching would
        claim ``<exe>-other-install``, ``daemon status run``, or any plist that
        merely mentions the path in metadata.
        """

        if self.platform != "launchd":
            return []
        canonical = self.unit_path
        jobs: list[tuple[str, Path]] = []
        if canonical.is_file():
            jobs.append((LAUNCHD_LABEL, canonical))
        try:
            candidates = sorted(canonical.parent.glob("*.plist"))
        except OSError:
            candidates = []
        executable = Path(str(self.config.executable.resolve()))
        for path in candidates:
            if path == canonical:
                continue
            parsed = _read_launchd_plist(path)
            if parsed is None:
                continue
            label, argv, program = parsed
            if not _argv_runs_daemon(argv, executable, program):
                continue
            jobs.append((label or path.stem, path))
        return jobs

    def _launchd_loaded_status(
        self, label: str, path: Path, result: subprocess.CompletedProcess[str]
    ) -> ServiceStatus:
        match = re.search(r"\bpid\s*=\s*(\d+)", result.stdout)
        pid = int(match.group(1)) if match else None
        runs_match = re.search(r"\bruns\s*=\s*(\d+)", result.stdout)
        exit_match = re.search(r"\blast exit code\s*=\s*(-?\d+)", result.stdout)
        running = bool(
            re.search(r"\bstate\s*=\s*running\b", result.stdout)
            or (pid is not None and pid > 0)
        )
        return ServiceStatus(
            "launchd",
            label,
            path,
            path.is_file(),
            True,
            running,
            pid,
            relaunch_count=int(runs_match.group(1)) if runs_match else None,
            last_exit_code=int(exit_match.group(1)) if exit_match else None,
        )

    def status(self, *, deadline: float | None = None) -> ServiceStatus:
        installed = self.unit_path.is_file()
        if self.platform == "launchd":
            jobs = self._launchd_jobs()
            for label, path in jobs:
                target = f"gui/{self.uid}/{label}"
                result = self._invoke(("launchctl", "print", target), deadline=deadline)
                if result.returncode != 0:
                    if not self._reports_not_loaded(result, label=label):
                        self._raise_command(("launchctl", "print", target), result)
                    continue
                return self._launchd_loaded_status(label, path, result)
            if jobs:
                # Files exist but none of them is loaded: report the job we
                # found (installed, not loaded) instead of the canonical label,
                # so callers can name the supervisor that owns the state.
                label, path = jobs[0]
                return ServiceStatus("launchd", label, path, True, False, False)
            # No attributable job file at all: keep the canonical "not loaded"
            # answer so install() can decide what to do.
            target = f"gui/{self.uid}/{LAUNCHD_LABEL}"
            result = self._invoke(("launchctl", "print", target), deadline=deadline)
            if result.returncode != 0 and not self._reports_not_loaded(result):
                self._raise_command(("launchctl", "print", target), result)
            if result.returncode == 0:
                return self._launchd_loaded_status(LAUNCHD_LABEL, self.unit_path, result)
            return ServiceStatus(
                "launchd", LAUNCHD_LABEL, self.unit_path, installed, False, False
            )
        result = self._invoke(
            (
                "systemctl",
                "--user",
                "show",
                "hyprial.service",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=NRestarts",
                "--property=ExecMainStatus",
            ),
            deadline=deadline,
        )
        if result.returncode != 0 and not self._reports_not_loaded(result):
            self._raise_command(
                (
                    "systemctl",
                    "--user",
                    "show",
                    "hyprial.service",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=MainPID",
                    "--property=NRestarts",
                    "--property=ExecMainStatus",
                ),
                result,
            )
        linger = self._invoke(
            ("loginctl", "show-user", self.username, "--property=Linger", "--value"),
            deadline=deadline,
        )
        boot_persistent = linger.returncode == 0 and linger.stdout.strip() == "yes"
        if result.returncode != 0:
            return ServiceStatus(
                "systemd",
                "hyprial.service",
                self.unit_path,
                installed,
                False,
                False,
                boot_persistent=boot_persistent,
            )
        fields = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        loaded = fields.get("LoadState") == "loaded"
        running = (
            fields.get("ActiveState") == "active"
            and fields.get("SubState") == "running"
        )
        raw_pid = fields.get("MainPID", "0")
        pid = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else None
        raw_restarts = fields.get("NRestarts", "")
        raw_exit = fields.get("ExecMainStatus", "")
        return ServiceStatus(
            "systemd",
            "hyprial.service",
            self.unit_path,
            installed,
            loaded,
            running,
            pid,
            boot_persistent,
            relaunch_count=(int(raw_restarts) if raw_restarts.isdigit() else None),
            last_exit_code=(int(raw_exit) if raw_exit.lstrip("-").isdigit() else None),
        )

    def inspect(
        self,
        *,
        serving_pid: int | None = None,
        deadline: float | None = None,
    ) -> ServiceStatus:
        """Combine supervisor, ownership, template, and serving-process facts."""

        base = self.status(deadline=deadline)
        # The loaded job decides which file describes the supervisor: a job the
        # operator named differently still runs OUR daemon (see _launchd_jobs).
        existing = self._existing_text_at(base.unit_path)
        ownership_reason = self._ownership_reason(
            existing, loaded=base.loaded, path=base.unit_path
        )
        attributed = (
            existing is not None
            and self._is_attributed(existing, label=base.unit)
            and ownership_reason is None
        )
        expected = self._rendered()
        template_matches = existing == expected if existing is not None else False
        if not base.installed:
            supervision: SupervisionState = "lost" if base.loaded else "unsupervised"
        elif (
            attributed
            and base.loaded
            and base.running
            and serving_pid is not None
            and self._matches_supervisor(base.pid, serving_pid)
        ):
            supervision = "supervised"
        else:
            supervision = "lost"
        return self._copy_status(
            base,
            attributed=attributed,
            template_matches=template_matches,
            serving_pid=serving_pid,
            supervision=supervision,
            ownership_busy=self._ownership_busy(),
            ownership_reason=ownership_reason,
        )

    def restart(
        self,
        serving_pid: Callable[..., int | None],
        *,
        attempts: int = 40,
        timeout_seconds: float | None = None,
    ) -> ServiceStatus:
        """Restart an owned service through its supervisor and verify custody.

        ``attempts`` preserves the ordinary service-command contract. Upgrade
        verification supplies ``timeout_seconds`` as one absolute wall-clock
        budget. Every probe, supervisor command, and sleep receives only the
        remaining part of that budget.
        """

        deadline = (
            time.monotonic() + max(0.0, timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        if timeout_seconds is not None:
            attempts = max(1, int(timeout_seconds / 0.25))
        if deadline is None:
            observed_pid = serving_pid()
        else:
            observed_pid = serving_pid(max(0.0, min(1.0, deadline - time.monotonic())))
        before = self.inspect(serving_pid=observed_pid, deadline=deadline)
        if not before.installed or not before.attributed:
            return before
        if self.platform == "launchd":
            self._require(
                (
                    "launchctl",
                    "kickstart",
                    "-k",
                    f"gui/{self.uid}/{before.unit}",
                ),
                deadline=deadline,
            )
        else:
            self._require(
                ("systemctl", "--user", "restart", "hyprial.service"),
                deadline=deadline,
            )
        return self.wait_for_custody(
            serving_pid,
            attempts=attempts,
            previous_job_pid=before.pid,
            deadline=deadline,
            initial_status=before,
        )

    def wait_for_custody(
        self,
        serving_pid: Callable[..., int | None],
        *,
        attempts: int = 40,
        previous_job_pid: int | None = None,
        deadline: float | None = None,
        initial_status: ServiceStatus | None = None,
    ) -> ServiceStatus:
        """Wait until the serving socket and supervisor agree on process custody."""

        latest = initial_status
        for _attempt in range(attempts):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                observed_pid = serving_pid(min(1.0, remaining))
                if time.monotonic() >= deadline:
                    break
            else:
                observed_pid = serving_pid()
            try:
                latest = self.inspect(serving_pid=observed_pid, deadline=deadline)
            except ServiceCommandError:
                if deadline is None or time.monotonic() < deadline:
                    raise
                break
            fresh_job = previous_job_pid is None or latest.pid != previous_job_pid
            exact_job = latest.pid is not None and latest.serving_pid == latest.pid
            if latest.supervision == "supervised" and fresh_job and exact_job:
                return latest
            delay = 0.25
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.monotonic()))
                if delay <= 0:
                    break
            self.sleep(delay)
        if latest is None:
            raise ServiceCommandError(
                "service custody wait reached its wall-clock deadline",
                reason="supervisor-start-hard-cap",
            )
        return self._copy_status(latest, supervision="lost")

    def reconcile(
        self,
        serving_pid: Callable[[], int | None],
        *,
        stop_stray: Callable[[int], None],
    ) -> ServiceStatus:
        """Repair owned template/custody drift without adopting foreign files."""

        current = self.inspect(serving_pid=serving_pid())
        if (current.installed or current.loaded) and not current.attributed:
            detail = (
                f"foreign or unsafe service file {self.unit_path}"
                if current.installed
                else f"loaded service without owned file {self.unit_path}"
            )
            raise ServiceOwnershipError(
                f"refusing to modify {detail}",
                reason=current.ownership_reason or "service-file-unattributed",
            )
        stray = current.serving_pid if current.supervision == "lost" else None
        if isinstance(stray, int):
            stop_stray(stray)
        backup: Path | None = None
        if current.installed and current.template_matches is False:
            backup = self._backup_existing()
            self.install()
        elif current.installed and not current.loaded:
            self.install()
        elif current.installed and current.supervision == "lost":
            restarted = self.restart(serving_pid)
            return self._copy_status(restarted, backup_path=backup)
        fixed = self.wait_for_custody(serving_pid)
        return self._copy_status(fixed, backup_path=backup)

    def install(self) -> ServiceStatus:
        before = self.status()
        existing = self._existing_text()
        ownership_reason = self._ownership_reason(existing, loaded=before.loaded)
        if before.loaded and existing is None:
            raise ServiceOwnershipError(
                f"refusing to replace loaded service without owned file {self.unit_path}",
                reason="supervisor-file-missing",
            )
        if ownership_reason is not None:
            raise ServiceOwnershipError(
                f"refusing to replace unsafe service file {self.unit_path}",
                reason=ownership_reason,
            )
        if existing is not None and not self._is_attributed(existing):
            raise ServiceOwnershipError(
                f"refusing to replace foreign service file {self.unit_path}"
            )
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.platform == "systemd" and before.boot_persistent is not True:
            self._require(("loginctl", "enable-linger", self.username))
        rendered = self._rendered()
        changed_file = existing != rendered
        if before.loaded and changed_file:
            self._unload()
        if changed_file:
            self._atomic_write(rendered)
            written_reason = self._ownership_reason(rendered, loaded=False)
            if written_reason is not None:
                raise ServiceOwnershipError(
                    f"refusing to load unsafe service file {self.unit_path}",
                    reason=written_reason,
                )
        if not before.loaded or changed_file:
            if not before.loaded:
                self.handoff_standalone()
            self._load()
        after = self.status()
        return ServiceStatus(
            **{
                field: getattr(after, field)
                for field in (
                    "platform",
                    "unit",
                    "unit_path",
                    "installed",
                    "loaded",
                    "running",
                    "pid",
                    "boot_persistent",
                )
            },
            changed=(
                changed_file
                or not before.loaded
                or (self.platform == "systemd" and before.boot_persistent is not True)
            ),
        )

    def uninstall(self) -> ServiceStatus:
        before = self.status()
        existing = self._existing_text()
        ownership_reason = self._ownership_reason(existing, loaded=before.loaded)
        if before.loaded and existing is None:
            raise ServiceOwnershipError(
                f"refusing to unload service without owned file {self.unit_path}",
                reason="supervisor-file-missing",
            )
        if ownership_reason is not None:
            raise ServiceOwnershipError(
                f"refusing to remove unsafe service file {self.unit_path}",
                reason=ownership_reason,
            )
        if existing is not None and not self._is_attributed(existing):
            raise ServiceOwnershipError(
                f"refusing to remove foreign service file {self.unit_path}"
            )
        if before.loaded:
            self._unload()
        self.unit_path.unlink(missing_ok=True)
        if self.platform == "systemd" and (before.loaded or before.installed):
            self._require(("systemctl", "--user", "daemon-reload"))
        after = self.status()
        return ServiceStatus(
            **{
                field: getattr(after, field)
                for field in (
                    "platform",
                    "unit",
                    "unit_path",
                    "installed",
                    "loaded",
                    "running",
                    "pid",
                    "boot_persistent",
                )
            },
            changed=before.loaded or before.installed,
        )

    def _rendered(self) -> str:
        return (
            self.templates.render_launchd(self.config)
            if self.platform == "launchd"
            else self.templates.render_systemd(self.config)
        )

    def _existing_text(self) -> str | None:
        return self._existing_text_at(self.unit_path)

    @staticmethod
    def _existing_text_at(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _ownership_reason(
        self, text: str | None, *, loaded: bool, path: Path | None = None
    ) -> str | None:
        unit_path = self.unit_path if path is None else path
        try:
            directory_stat = unit_path.parent.stat()
        except FileNotFoundError:
            return "supervisor-file-missing" if loaded else None
        if directory_stat.st_uid != os.getuid():
            return "unit-directory-owner-mismatch"
        if directory_stat.st_mode & 0o022:
            return "unit-directory-permissions-unsafe"
        if text is None:
            return "supervisor-file-missing" if loaded else None
        try:
            unit_stat = unit_path.stat()
        except FileNotFoundError:
            return "supervisor-file-missing" if loaded else None
        if unit_stat.st_uid != os.getuid():
            return "unit-owner-mismatch"
        if unit_stat.st_mode & 0o022:
            return "unit-permissions-unsafe"
        return None

    def _reports_not_loaded(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        label: str | None = None,
    ) -> bool:
        if self.platform == "launchd":
            expected = (
                f'Could not find service "{label or LAUNCHD_LABEL}" in domain '
                f"for user gui: {self.uid}"
            )
            return result.returncode == 113 and expected in result.stderr.splitlines()
        return (
            result.returncode == 4
            and result.stderr.strip() == "Unit hyprial.service could not be found."
        )

    def _is_attributed(self, text: str, *, label: str | None = None) -> bool:
        executable = str(self.config.executable.resolve())
        if self.platform == "launchd":
            # Attribution is about WHAT the job runs. A job the operator named
            # differently but pointed at our executable is still our supervisor;
            # requiring our own label here is what pushed production into an
            # unsupervised spawn while launchd idled.
            label_ok = label in (None, LAUNCHD_LABEL) or (
                f"<string>{label}</string>" in text
            )
            if not label_ok:
                return False
            return _text_program_argv_runs_daemon(text, Path(executable))
        quoted = ServiceTemplates._quote_systemd(executable)
        return (
            f"ExecStart={quoted} daemon run" in text
            and 'Environment="HYPRIAL_SERVICE_UNIT=hyprial.service"' in text
        )

    def _backup_existing(self) -> Path | None:
        existing = self._existing_text()
        if existing is None:
            return None
        backup = self.unit_path.with_name(
            f"{self.unit_path.name}.backup.{time.time_ns()}"
        )
        backup.write_text(existing, encoding="utf-8")
        os.chmod(backup, 0o600)
        return backup

    def _matches_supervisor(self, job_pid: int | None, serving_pid: int) -> bool:
        return job_pid is not None and serving_pid == job_pid

    def _ownership_busy(self) -> bool:
        path = self.config.state_dir / "daemon-service.stderr.log"
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 64 * 1024))
                return b"DAEMON_OWNERSHIP_BUSY" in stream.read()
        except OSError:
            return False

    @staticmethod
    def _copy_status(status: ServiceStatus, **changes: object) -> ServiceStatus:
        fields = {
            name: getattr(status, name) for name in ServiceStatus.__dataclass_fields__
        }
        fields.update(changes)
        return ServiceStatus(**fields)  # type: ignore[arg-type]

    @staticmethod
    def _parent_pid(pid: int) -> int | None:
        try:
            result = subprocess.run(
                ("ps", "-o", "ppid=", "-p", str(pid)),
                text=True,
                capture_output=True,
                check=False,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        return int(value) if result.returncode == 0 and value.isdigit() else None

    def _load(self) -> None:
        if self.platform == "launchd":
            command = (
                "launchctl",
                "bootstrap",
                f"gui/{self.uid}",
                str(self.unit_path),
            )
            for attempt in range(5):
                result = self._invoke(command)
                if result.returncode == 0:
                    return
                transient = (
                    result.returncode == 5 or "Input/output error" in result.stderr
                )
                if not transient or attempt == 4:
                    self._raise_command(command, result)
                self.sleep(0.25 * (attempt + 1))
            return
        self._require(("systemctl", "--user", "daemon-reload"))
        self._require(("systemctl", "--user", "enable", "--now", "hyprial.service"))

    def _unload(self) -> None:
        if self.platform == "launchd":
            self._require(("launchctl", "bootout", f"gui/{self.uid}/{LAUNCHD_LABEL}"))
            for _attempt in range(180):
                if not self.status().loaded:
                    return
                self.sleep(0.25)
            raise ServiceCommandError(
                f"timed out waiting for launchd to unload {LAUNCHD_LABEL}",
                reason="manager-command-timeout",
            )
        self._require(("systemctl", "--user", "disable", "--now", "hyprial.service"))

    def _atomic_write(self, rendered: str) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.unit_path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(rendered, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, self.unit_path)

    def _invoke(
        self,
        command: Sequence[str],
        *,
        deadline: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        timeout: float | None = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceCommandError(
                    f"service command exceeded restart deadline: {' '.join(command)}",
                    reason="supervisor-start-hard-cap",
                )
            timeout = min(15.0, remaining)
        try:
            return (
                self.runner(command)
                if timeout is None
                else self.runner(command, timeout=timeout)
            )
        except ServiceCommandError:
            raise
        except subprocess.TimeoutExpired as error:
            reason = (
                "supervisor-start-hard-cap"
                if deadline is not None and time.monotonic() >= deadline
                else "manager-command-timeout"
            )
            raise ServiceCommandError(
                f"service command timed out: {' '.join(command)}",
                reason=reason,
            ) from error
        except FileNotFoundError as error:
            raise ServiceCommandError(
                f"service command is unavailable: {' '.join(command)}: {error}",
                reason="manager-command-unavailable",
            ) from error
        except OSError as error:
            raise ServiceCommandError(
                f"service command could not run: {' '.join(command)}: {error}",
                reason="manager-command-failed",
            ) from error

    def _require(
        self,
        command: Sequence[str],
        *,
        deadline: float | None = None,
    ) -> None:
        result = self._invoke(command, deadline=deadline)
        if result.returncode != 0:
            self._raise_command(command, result)

    @staticmethod
    def _raise_command(
        command: Sequence[str], result: subprocess.CompletedProcess[str]
    ) -> None:
        raise ServiceCommandError(
            f"service command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip()}",
            reason="manager-command-failed",
        )

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=15.0 if timeout is None else timeout,
        )
