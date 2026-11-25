# Decision record (W1E-2, 2026-11-21):
# - ServiceManager enforces private access on outside-Home state_dir and backup
#   unit files via manysys.secure_private_directory / harden_private_path, and
#   inspects unit ownership via manysys.posix_metadata_enforced, current_uid,
#   and private_metadata_violation instead of raw os.getuid / os.chmod(0o600).
"""Idempotent user-service integration for one home.

Z-8 launchd acceptance fix D1 (2026-11-16, PR #2830): the plan and the
contract were silent on what an install does when the manager loads the unit
but custody of the serving daemon is never established (the measured case: a
unit whose interpreter cannot import hyprial crash-looped under KeepAlive
until an explicit uninstall). Decision: a freshly written unit that never
takes custody is not left behind. ``capture_unit_state`` records the unit
text and loaded flag before ``install``; ``revert_install`` then returns to
that state. No prior unit: the manager unloads the unit and the file is
removed (``uninstall``). A prior unit: it is unloaded, its previous text is
written back and it is loaded again only if it was loaded before. An install
that changed nothing leaves the existing unit as it was. The caller reports
``SERVICE_SUPERVISION_LOST`` and says which of the three happened.

Z-8 launchd acceptance fix N1 (2026-11-16): systemd attribution parses the
``ExecStart=`` line and compares its program through
``same_installation_program`` (launchd_jobs) instead of a literal substring,
so every attribution site shares the one interpreter-equivalence rule.

Z-8 launchd acceptance fix N2 (2026-11-17): the custody wait after install was
a fixed 40 x 0.25 s window (about 10 s). Measured first starts take 6-8 s on a
near-empty home, 13.5-14 s right after a SIGTERMed predecessor, and a real
restore far longer, so a normal install, an unchanged re-install during a
start and a rewrite of a loaded unit were judged lost and (D1) reverted while
the daemon was still coming up. A fixed count measures patience, not health.
Decision: this module owns no start wait, budget or grace. The one owner of
start supervision is the ``daemon start`` wait in the shell
(``_wait_for_daemon``: startup-phase progress resets a 30 s idle budget, a
120 s hard cap). The shell hands the manager ``await_startup``; the manager
contributes only the observation "is the unit's job running"
(``_job_running``) and then keeps its own custody judgement: the serving pid
must equal the fresh job pid, and a different pid that keeps answering stays
lost after the legacy fixed window (``attempts`` x 0.25 s). Revert triggers,
all decided by the shell wait: the job ran and then stayed down for the exit
grace (a crash); no startup progress within the idle budget, which also covers
a job that never spawned (a slow spawn is not an exit); the hard cap.
``await_startup`` applies only to the install-shaped call (no previous job
pid, no initial status, no deadline); restart/reconcile-with-restart and the
upgrade ``timeout_seconds`` contract are unchanged.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.configuration.launchd_jobs import (
    owned_unit_file as _owned_unit_file,
    same_installation_program as _same_installation_program,
    printed_plist_path as _printed_plist_path,
    text_program_argv_runs_daemon as _text_program_argv_runs_daemon,
)
from hyprial.kernel import (
    current_uid,
    harden_private_path,
    posix_metadata_enforced,
    private_metadata_violation,
    secure_private_directory,
)

from . import identity
from .supervisor import SupervisorCommands
from .templates import ServiceTemplates
from .types import (
    ServiceCommandError,
    ServiceCommandRunner,
    ServiceConfig,
    ServiceOwnershipError,
    ServicePlatform,
    ServiceStatus,
    SupervisionState,
)


class ServiceManager(SupervisorCommands):
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
        await_startup: Callable[[Callable[[], bool]], bool] | None = None,
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
        self.await_startup = await_startup
        self.templates = ServiceTemplates()
        self._bus_environment: dict[str, str] | None = None

    @property
    def unit_path(self) -> Path:
        return identity.unit_path(self.platform, self.config, self.service_home)

    def status(self, *, deadline: float | None = None) -> ServiceStatus:
        """Probe the supervisor for this home.

        When unit files exist but none of them is loaded, report the job that
        was found (installed, not loaded) instead of the canonical label, so
        callers can name the supervisor that owns the state. With no
        attributable job file at all, keep the canonical "not loaded" answer so
        install() can decide what to do.
        """

        installed = self.unit_path.is_file()
        if self.platform == "launchd":
            label = identity.launchd_label(self.config)
            jobs = self._launchd_jobs()
            for job_label, path in jobs:
                target = f"gui/{self.uid}/{job_label}"
                result = self._invoke(("launchctl", "print", target), deadline=deadline)
                if result.returncode != 0:
                    if not self._reports_not_loaded(result, label=job_label):
                        self._raise_command(("launchctl", "print", target), result)
                    continue
                return self._launchd_loaded_status(
                    job_label,
                    path,
                    result,
                    owned=_owned_unit_file(_printed_plist_path(result.stdout), path),
                )
            if jobs:
                job_label, path = jobs[0]
                return ServiceStatus("launchd", job_label, path, True, False, False)
            target = f"gui/{self.uid}/{label}"
            result = self._invoke(("launchctl", "print", target), deadline=deadline)
            if result.returncode != 0 and not self._reports_not_loaded(result):
                self._raise_command(("launchctl", "print", target), result)
            if result.returncode == 0:
                return self._launchd_loaded_status(
                    label,
                    self.unit_path,
                    result,
                    owned=_owned_unit_file(
                        _printed_plist_path(result.stdout), self.unit_path
                    ),
                )
            return ServiceStatus(
                "launchd", label, self.unit_path, installed, False, False
            )
        unit = identity.systemd_unit(self.config)
        show_properties = (
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=NRestarts",
            "--property=ExecMainStatus",
            "--property=FragmentPath",
        )
        result = self._invoke(
            ("systemctl", "--user", "show", unit, *show_properties),
            deadline=deadline,
        )
        if result.returncode != 0 and not self._reports_not_loaded(result):
            self._raise_command(
                ("systemctl", "--user", "show", unit, *show_properties),
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
                unit,
                self.unit_path,
                installed,
                False,
                False,
                boot_persistent=boot_persistent,
            )
        fields = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        loaded = fields.get("LoadState") == "loaded" and _owned_unit_file(
            fields.get("FragmentPath"), self.unit_path
        )
        foreign_loaded = fields.get("LoadState") == "loaded" and not loaded
        active = fields.get("ActiveState") == "active"
        running = active and not foreign_loaded and fields.get("SubState") == "running"
        raw_pid = fields.get("MainPID", "0")
        pid = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else None
        raw_restarts = fields.get("NRestarts", "")
        raw_exit = fields.get("ExecMainStatus", "")
        return ServiceStatus(
            "systemd",
            unit,
            self.unit_path,
            installed,
            loaded,
            running,
            None if foreign_loaded else pid,
            boot_persistent,
            relaunch_count=(int(raw_restarts) if raw_restarts.isdigit() else None),
            last_exit_code=(int(raw_exit) if raw_exit.lstrip("-").isdigit() else None),
            foreign_loaded=foreign_loaded,
        )

    def inspect(
        self,
        *,
        serving_pid: int | None = None,
        deadline: float | None = None,
    ) -> ServiceStatus:
        """Combine supervisor, ownership, template, and serving-process facts."""

        base = self.status(deadline=deadline)
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

        self._require_service_home()
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
        self._require_owned_label(before)
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
                ("systemctl", "--user", "restart", identity.systemd_unit(self.config)),
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
        if (
            self.await_startup is not None
            and deadline is None
            and previous_job_pid is None
            and initial_status is None
            and not self.await_startup(self._job_running)
        ):
            latest = self.inspect(serving_pid=serving_pid())
            return self._copy_status(latest, supervision="lost")
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

    def _job_running(self) -> bool:
        return self.status().running

    def reconcile(
        self,
        serving_pid: Callable[[], int | None],
        *,
        stop_stray: Callable[[int], None],
    ) -> ServiceStatus:
        """Repair owned template/custody drift without adopting foreign files."""

        self._require_service_home()
        current = self.inspect(serving_pid=serving_pid())
        self._require_owned_label(current)
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

    def _require_service_home(self) -> None:
        identity.require_service_home(self.config, self.service_home)

    def _manageable_status(self) -> ServiceStatus:
        self._require_service_home()
        current = self.status()
        self._require_owned_label(current)
        return current

    def _require_owned_label(self, current: ServiceStatus) -> None:
        """Refuse to mutate a label another home's unit file already loaded.

        ``status()`` reports it as not this home's supervisor, but writing,
        bootstrapping or kickstarting that label addresses the other job.
        All mutation entries check here before commands, callbacks or file writes.
        """

        if not current.foreign_loaded:
            return
        raise ServiceOwnershipError(
            "refusing to touch the loaded supervisor label of another unit file "
            f"(this home's: {current.unit_path})",
            reason="supervisor-label-foreign"
            if current.installed
            else "supervisor-file-missing",
        )

    def install(self) -> ServiceStatus:
        before = self._manageable_status()
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
        secure_private_directory(self.config.state_dir, create=True, mode=0o700)
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

    def capture_unit_state(self) -> tuple[str | None, bool]:
        """The unit text and loaded flag ``revert_install`` returns to."""

        before = self._manageable_status()
        return self._existing_text(), before.loaded

    def revert_install(self, state: tuple[str | None, bool]) -> str:
        """Undo an install whose custody was never established.

        Returns ``"removed"`` (no unit existed before), ``"restored"`` (the
        previous unit text was written back) or ``"unchanged"`` (the install
        left the previous unit as it was).
        """

        previous, was_loaded = state
        if previous is None:
            self.uninstall()
            return "removed"
        current = self._manageable_status()
        if self._existing_text() == previous and current.loaded == was_loaded:
            return "unchanged"
        if current.loaded:
            self._unload()
        self._atomic_write(previous)
        if was_loaded:
            self._load()
        elif self.platform == "systemd":
            self._require(("systemctl", "--user", "daemon-reload"))
        return "restored"

    def uninstall(self) -> ServiceStatus:
        before = self._manageable_status()
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
        if posix_metadata_enforced():
            if directory_stat.st_uid != current_uid():
                return "unit-directory-owner-mismatch"
            if directory_stat.st_mode & 0o022:
                return "unit-directory-permissions-unsafe"
        else:
            violation = private_metadata_violation(
                unit_path.parent, directory_stat, mode=None
            )
            if violation == "wrong-owner":
                return "unit-directory-owner-mismatch"
            if violation is not None:
                return "unit-directory-permissions-unsafe"
        if text is None:
            return "supervisor-file-missing" if loaded else None
        try:
            unit_stat = unit_path.stat()
        except FileNotFoundError:
            return "supervisor-file-missing" if loaded else None
        if posix_metadata_enforced():
            if unit_stat.st_uid != current_uid():
                return "unit-owner-mismatch"
            if unit_stat.st_mode & 0o022:
                return "unit-permissions-unsafe"
        else:
            violation = private_metadata_violation(unit_path, unit_stat, mode=None)
            if violation == "wrong-owner":
                return "unit-owner-mismatch"
            if violation is not None:
                return "unit-permissions-unsafe"
        return None

    def _is_attributed(self, text: str, *, label: str | None = None) -> bool:
        executable = str(identity.program_path(self.config))
        if self.platform == "launchd":
            expected = identity.launchd_label(self.config)
            label_ok = label in (None, expected) or (
                f"<string>{label}</string>" in text
            )
            if not label_ok:
                return False
            return _text_program_argv_runs_daemon(
                text, Path(executable), daemon_args=self.config.daemon_args
            )
        return self._systemd_exec_runs_daemon(text, executable) and (
            f'Environment="HYPRIAL_SERVICE_UNIT={identity.systemd_unit(self.config)}"'
            in text
        )

    def _systemd_exec_runs_daemon(self, text: str, executable: str) -> bool:
        expected = " ".join(self.config.daemon_args)
        for line in text.splitlines():
            if not line.startswith("ExecStart="):
                continue
            program, rest = self._split_systemd_program(line[len("ExecStart=") :])
            if program is None or not _same_installation_program(program, executable):
                continue
            if rest == expected or rest.startswith(expected + " "):
                return True
        return False

    @staticmethod
    def _split_systemd_program(command: str) -> tuple[str | None, str]:
        if not command.startswith('"'):
            program, _separator, rest = command.partition(" ")
            return program, rest
        chars: list[str] = []
        index = 1
        while index < len(command):
            char = command[index]
            if char == "\\" and index + 1 < len(command):
                chars.append(command[index + 1])
                index += 2
                continue
            if char == '"':
                program = "".join(chars).replace("%%", "%")
                return program, command[index + 1 :].lstrip(" ")
            chars.append(char)
            index += 1
        return None, ""

    def _backup_existing(self) -> Path | None:
        existing = self._existing_text()
        if existing is None:
            return None
        backup = self.unit_path.with_name(
            f"{self.unit_path.name}.backup.{time.time_ns()}"
        )
        backup.write_text(existing, encoding="utf-8")
        harden_private_path(backup, mode=0o600)
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

    def _atomic_write(self, rendered: str) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.unit_path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(rendered, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, self.unit_path)
