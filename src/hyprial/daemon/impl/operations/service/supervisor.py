"""Supervisor command invocation, probing, and error classification."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from hyprial.daemon.impl.bootstrap.user_bus import (
    USER_BUS_ADDRESS_UNREACHABLE_REASON as USER_BUS_ADDRESS_UNREACHABLE_REASON,
    USER_BUS_UNAVAILABLE_REASON as USER_BUS_UNAVAILABLE_REASON,
    session_bus_environment as session_bus_environment,
    user_bus_state as user_bus_state,
)
from hyprial.daemon.impl.configuration.launchd_jobs import (
    argv_runs_daemon as _argv_runs_daemon,
    environment_matches_home as _environment_matches_home,
    read_launchd_plist as _read_launchd_plist,
)

from . import identity
from .types import (
    ServiceCommandError,
    ServiceStatus,
)


class SupervisorCommands:
    """Commands that address the platform supervisor for one home."""

    def _launchd_jobs(self) -> list[tuple[str, Path]]:
        """Find jobs whose parsed argv runs this executable's daemon, including wrappers.

        A candidate job is claimed only when its executable and argument prefix
        match this config AND its plist ``EnvironmentVariables.HYPRIAL_HOME``
        resolves to this home (a job without the entry serves the default home
        and belongs to the default home's manager only).
        """

        if self.platform != "launchd":
            return []
        canonical = self.unit_path
        jobs: list[tuple[str, Path]] = []
        if canonical.is_file():
            jobs.append((identity.launchd_label(self.config), canonical))
        try:
            candidates = sorted(canonical.parent.glob("*.plist"))
        except OSError:
            candidates = []
        executable = identity.program_path(self.config)
        default_home = self.config.home / ".hyprial"
        for path in candidates:
            if path == canonical:
                continue
            parsed = _read_launchd_plist(path)
            if parsed is None:
                continue
            label, argv, program, environment = parsed
            if not _argv_runs_daemon(
                argv, executable, program, daemon_args=self.config.daemon_args
            ):
                continue
            if not _environment_matches_home(
                environment, self.config.hyprial_home, default_home=default_home
            ):
                continue
            jobs.append((label or path.stem, path))
        return jobs

    def _launchd_loaded_status(
        self,
        label: str,
        path: Path,
        result: subprocess.CompletedProcess[str],
        *,
        owned: bool = True,
    ) -> ServiceStatus:
        match = re.search(r"\bpid\s*=\s*(\d+)", result.stdout)
        pid = int(match.group(1)) if match else None
        runs_match = re.search(r"\bruns\s*=\s*(\d+)", result.stdout)
        exit_match = re.search(r"\blast exit code\s*=\s*(-?\d+)", result.stdout)
        running = owned and bool(
            re.search(r"\bstate\s*=\s*running\b", result.stdout) or (pid or 0) > 0
        )
        return ServiceStatus(
            "launchd",
            label,
            path,
            path.is_file(),
            owned,
            running,
            pid if owned else None,
            relaunch_count=int(runs_match.group(1)) if runs_match else None,
            last_exit_code=int(exit_match.group(1)) if exit_match else None,
            foreign_loaded=not owned,
        )

    def _reports_not_loaded(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        label: str | None = None,
    ) -> bool:
        """Whether the manager definitively answered "this unit is absent".

        launchd: exit 113 plus the exact per-label line. systemd: exit 4 plus a
        show output that carries no loaded unit (``LoadState=not-found`` or no
        non-empty ``FragmentPath``). The systemd rule is locale-independent:
        non-English ``systemctl`` builds answer exit 4 with a translated stderr
        sentence, which is still a definitive absence (G-M2), while exit 4 with
        a real loaded ``FragmentPath`` stays a failure.
        """

        if self.platform == "launchd":
            expected = (
                f'Could not find service "{label or identity.launchd_label(self.config)}" in domain '
                f"for user gui: {self.uid}"
            )
            return result.returncode == 113 and expected in result.stderr.splitlines()
        if result.returncode != 4:
            return False
        fields = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if fields.get("LoadState") == "not-found":
            return True
        return not fields.get("FragmentPath")

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
        self._require(
            ("systemctl", "--user", "enable", "--now", identity.systemd_unit(self.config))
        )

    def _unload(self) -> None:
        if self.platform == "launchd":
            label = identity.launchd_label(self.config)
            self._require(("launchctl", "bootout", f"gui/{self.uid}/{label}"))
            for _attempt in range(180):
                if not self.status().loaded:
                    return
                self.sleep(0.25)
            raise ServiceCommandError(
                f"timed out waiting for launchd to unload {label}",
                reason="manager-command-timeout",
            )
        self._require(
            ("systemctl", "--user", "disable", "--now", identity.systemd_unit(self.config))
        )

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

    def _raise_command(
        self, command: Sequence[str], result: subprocess.CompletedProcess[str]
    ) -> None:
        stderr = result.stderr.strip()
        if self.platform == "systemd" and command[:2] == ("systemctl", "--user"):
            state = user_bus_state(self.bus_environment(), uid=self.uid)
            if state == "no-session":
                raise ServiceCommandError(
                    "the per-user systemd bus is not reachable from this session "
                    "(no DBUS_SESSION_BUS_ADDRESS, and no /run/user/<uid>/bus): "
                    f"{' '.join(command)}: {stderr}. "
                    "Enable lingering for the daemon account "
                    "(`loginctl enable-linger <user>`) so its user manager runs "
                    "without a login session, then retry with "
                    "XDG_RUNTIME_DIR=/run/user/<uid>.",
                    reason=USER_BUS_UNAVAILABLE_REASON,
                )
            if state == "address-unreachable":
                raise ServiceCommandError(
                    "DBUS_SESSION_BUS_ADDRESS is set but its socket does not exist, "
                    f"so the user bus cannot be reached: {' '.join(command)}: {stderr}. "
                    "Fix or unset DBUS_SESSION_BUS_ADDRESS (lingering does not help: "
                    "the address is explicit).",
                    reason=USER_BUS_ADDRESS_UNREACHABLE_REASON,
                )
        raise ServiceCommandError(
            f"service command failed ({result.returncode}): {' '.join(command)}: "
            f"{stderr}",
            reason="manager-command-failed",
        )

    def bus_environment(self) -> dict[str, str]:
        """The environment used for supervisor commands (computed once)."""

        if self._bus_environment is None:
            self._bus_environment = session_bus_environment()
        return self._bus_environment

    def _run(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        identity.require_account_home(self.config, self.service_home)
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=15.0 if timeout is None else timeout,
            env=self.bus_environment(),
        )
