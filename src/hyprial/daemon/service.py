"""Packaged launchd/systemd templates used by the CLI service manager."""

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

LAUNCHD_LABEL = "com.hyprial.hyprial.daemon"
ServicePlatform = Literal["launchd", "systemd"]


class ServiceCommandRunner(Protocol):
    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


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
            files("hyprial.daemon")
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
            files("hyprial.daemon")
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
    ) -> None:
        self.config = config
        self.platform = platform
        self.service_home = Path(service_home or config.home)
        self.runner = runner or self._run
        self.handoff_standalone = handoff_standalone or (lambda: None)
        self.uid = os.getuid() if uid is None else uid
        self.username = username or os.environ.get("USER", "")
        self.sleep = sleep
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

    def status(self) -> ServiceStatus:
        installed = self.unit_path.is_file()
        if self.platform == "launchd":
            target = f"gui/{self.uid}/{LAUNCHD_LABEL}"
            result = self.runner(("launchctl", "print", target))
            if result.returncode != 0:
                return ServiceStatus(
                    "launchd", LAUNCHD_LABEL, self.unit_path, installed, False, False
                )
            match = re.search(r"\bpid\s*=\s*(\d+)", result.stdout)
            pid = int(match.group(1)) if match else None
            running = bool(
                re.search(r"\bstate\s*=\s*running\b", result.stdout)
                or (pid is not None and pid > 0)
            )
            return ServiceStatus(
                "launchd",
                LAUNCHD_LABEL,
                self.unit_path,
                installed,
                True,
                running,
                pid,
            )
        result = self.runner(
            (
                "systemctl",
                "--user",
                "show",
                "hyprial.service",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
            )
        )
        linger = self.runner(
            ("loginctl", "show-user", self.username, "--property=Linger", "--value")
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
        return ServiceStatus(
            "systemd",
            "hyprial.service",
            self.unit_path,
            installed,
            loaded,
            running,
            pid,
            boot_persistent,
        )

    def install(self) -> ServiceStatus:
        before = self.status()
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.platform == "systemd" and before.boot_persistent is not True:
            self._require(("loginctl", "enable-linger", self.username))
        rendered = (
            self.templates.render_launchd(self.config)
            if self.platform == "launchd"
            else self.templates.render_systemd(self.config)
        )
        existing = (
            self.unit_path.read_text(encoding="utf-8")
            if self.unit_path.is_file()
            else None
        )
        changed_file = existing != rendered
        if before.loaded and changed_file:
            self._unload()
        if changed_file:
            self._atomic_write(rendered)
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

    def _load(self) -> None:
        if self.platform == "launchd":
            command = (
                "launchctl",
                "bootstrap",
                f"gui/{self.uid}",
                str(self.unit_path),
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
        self._require(("systemctl", "--user", "daemon-reload"))
        self._require(("systemctl", "--user", "enable", "--now", "hyprial.service"))

    def _unload(self) -> None:
        if self.platform == "launchd":
            self._require(("launchctl", "bootout", f"gui/{self.uid}/{LAUNCHD_LABEL}"))
            for _attempt in range(180):
                if not self.status().loaded:
                    return
                self.sleep(0.25)
            raise RuntimeError(
                f"timed out waiting for launchd to unload {LAUNCHD_LABEL}"
            )
        self._require(("systemctl", "--user", "disable", "--now", "hyprial.service"))

    def _atomic_write(self, rendered: str) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.unit_path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(rendered, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, self.unit_path)

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
