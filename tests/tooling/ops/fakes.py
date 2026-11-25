"""In-process fake of the host the dev-upgrade smoke drives.

Nothing is executed: no hyprial, systemctl, launchctl, ps or uv.  The fake
answers the commands of the service seam (``hyprial daemon service status``,
``hyprial daemon stop|start|status --json``, ``hyprial version --json``,
``ps -o stat= -o lstart= -p``, ``uv tool install``) and, for the legacy path and
the rollback-only raw stop, ``systemctl --user`` and ``launchctl bootout``.

The service status payload is built the way the product builds it
(``_status_payload`` over ``ServiceManager.inspect``): an installed unit that is
not running reports ``supervision: "lost"``, ``supervised`` needs an attributed,
loaded, running unit whose pid is the serving pid.  CLI errors are
``{"ok": false, "code", "error", "data"}`` on stdout with exit 1.  A baseline
CLI from before ``daemon service`` existed (``legacy``) answers the usage error
as its entry module did then: ``INVALID_ARGUMENT`` JSON with exit 2
(``legacy = "plain"``: exit 2 without JSON, for even older entries).

A slow start (``slow_start_versions``) answers ``DAEMON_START_TIMEOUT`` while
the unit runs with a pid and no daemon answers yet (status ``running: true``,
``servingPid: null``, ``supervision: "lost"``, as measured on a real host),
until that many fake-clock seconds have passed.

A stop that hangs keeps the unit deactivating (``running`` false, ``pid`` set)
until ``hang_stop_seconds`` of fake-clock time have passed, then the unit ends
stopped with no main pid, as systemd does after its stop timeout.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import _dev_upgrade_smoke as smoke  # noqa: E402

SERVICE = "hyprial.service"
BIRTH = "Mon Nov 20 10:00:00 2026"
OTHER_BIRTH = "Tue Nov 21 11:00:00 2026"
FIRST_PID = 123
STARTED_PID = 456
WALL = 1_700_000_000


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.wall_now = float(WALL)
        self.slept = 0.0
        self.hooks: list[Callable[[float], None]] = []

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.wall_now

    def sleep(self, seconds: float) -> None:
        for hook in self.hooks:
            hook(seconds)
        self.now += seconds
        self.slept += seconds


def write_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"hyprial-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: hyprial\nVersion: {version}\n",
        )
        archive.writestr("hyprial/_core/actors.abi3.so", b"\x7fELF")


def result(command: list[str], rc: int, stdout: Any = "", stderr: str = "") -> Any:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return smoke.CommandResult(command, rc, text, stderr)


def cli_error(command: list[str], code: str, data: dict[str, Any] | None = None) -> Any:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": code.lower()}
    if data is not None:
        payload["data"] = data
    return result(command, 1, payload)


class FakeHost:
    def __init__(self, tmp_path: Path, home: Path, *, clock: Any = None) -> None:
        self.tmp_path = tmp_path
        self.home = home
        self.clock = clock
        self.platform = "systemd"
        self.unit = SERVICE
        self.installed = True
        self.attributed = True
        self.loaded = True
        self.active = True
        self.main_pid: int | None = FIRST_PID
        self.installed_version = "0.1.0"
        self.installs: list[str] = []
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.timeouts: list[float | None] = []
        self.legacy = False
        self.legacy_fields_only = False
        self.status_error: tuple[str, dict[str, Any] | None] | None = None
        self.unattributed_versions: set[str] = set()
        self.broken_versions: set[str] = set()
        self.stop_may_restart = False
        self.stop_without_supervisor = False
        self.revive_before_start = False
        self.start_fails = False
        self.hang_stop_seconds: float | None = None
        self.stopping_since: float | None = None
        self.bootstrap = False
        self.detached_pid: int | None = None
        self.pid_after_stop = "dead"
        self.lingering: set[int] = set()
        self.stopped_state = "inactive"
        self.fail_install_version: str | None = None
        self.raw_stops: list[list[str]] = []
        self.daemon_status_override: Any = None
        self.slow_start_versions: dict[str, float] = {}
        self.slow_start_launches = True
        self.starting_since: float | None = None
        self.starting_for = 0.0

    def now(self) -> float:
        return self.clock.monotonic() if self.clock is not None else 0.0

    def settle(self) -> None:
        if self.stopping_since is None or self.hang_stop_seconds is None:
            return
        if self.now() - self.stopping_since >= self.hang_stop_seconds:
            self.stopping_since = None
            self.lingering.discard(self.main_pid or 0)
            self.main_pid = None

    def stopping(self) -> bool:
        self.settle()
        return self.stopping_since is not None

    def still_starting(self) -> bool:
        if self.starting_since is None:
            return False
        if self.now() - self.starting_since >= self.starting_for:
            self.starting_since = None
            return False
        return True

    def answering_pid(self) -> int | None:
        if self.detached_pid is not None:
            return self.detached_pid
        if self.still_starting():
            return None
        if self.active and not self.stopping() and self.main_pid is not None:
            return self.main_pid
        return None

    def cli_attributes(self) -> bool:
        return self.attributed and self.installed_version not in self.unattributed_versions

    def status_payload(self) -> dict[str, Any]:
        stopping = self.stopping()
        running = self.loaded and self.active and not stopping and self.main_pid is not None
        pid = self.main_pid if (self.active or stopping) and self.loaded else None
        serving = self.answering_pid()
        attributed = self.cli_attributes()
        if not self.installed:
            supervision = "lost" if self.loaded else "unsupervised"
        elif attributed and self.loaded and running and serving is not None and pid == serving:
            supervision = "supervised"
        else:
            supervision = "lost"
        payload: dict[str, Any] = {
            "ok": True,
            "platform": self.platform,
            "unit": self.unit,
            "unitPath": str(self.home / "unit-file"),
            "installed": self.installed,
            "loaded": self.loaded,
            "running": running,
            "pid": pid,
            "bootPersistent": True,
            "changed": None,
            "attributed": attributed,
            "templateMatches": True,
            "servingPid": serving,
            "supervision": supervision,
            "relaunchCount": 0,
            "lastExitCode": None,
            "ownershipBusy": False,
            "ownershipReason": None,
            "backupPath": None,
        }
        if self.legacy_fields_only:
            payload.pop("supervision")
            payload.pop("attributed")
        return payload

    def daemon_payload(self) -> dict[str, Any]:
        pid = self.answering_pid()
        if pid is None:
            return {"ok": True, "daemon": {"running": False}}
        daemon: dict[str, Any] = {
            "running": True,
            "pid": pid,
            "socket": str(self.home / "daemon.sock"),
            "lifecycle": {"crashed": False},
        }
        if self.bootstrap:
            daemon["phase"] = "bootstrap"
        else:
            daemon["version"] = self.installed_version
        return {"ok": True, "daemon": daemon}

    def ping(self) -> dict[str, Any]:
        pid = self.answering_pid()
        return {
            "running": True,
            "pid": pid,
            "epoch": "e1",
            "version": self.installed_version,
            "phase": "bootstrap" if self.bootstrap else "serving",
        }

    def service_stops(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:4] == ["daemon", "stop", "--json"]]

    def service_starts(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:4] == ["daemon", "start", "--json"]]

    def status_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:5] == ["daemon", "service", "status", "--json"]]

    def systemctl_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "systemctl"]

    def env_of(self, command_prefix: list[str]) -> dict[str, str] | None:
        for command, env in zip(self.calls, self.envs):
            if command[1 : 1 + len(command_prefix)] == command_prefix:
                return env
        return None

    def timeout_of(self, command_prefix: list[str]) -> float | None:
        for command, timeout in zip(self.calls, self.timeouts):
            if command[1 : 1 + len(command_prefix)] == command_prefix:
                return timeout
        return None

    def stop_daemon(self) -> None:
        old = self.main_pid
        self.starting_since = None
        if self.hang_stop_seconds is not None:
            self.stopping_since = self.now()
            return
        self.active = False
        self.main_pid = None
        if old is not None and self.pid_after_stop == "alive":
            self.lingering.add(old)
        if old is not None and self.pid_after_stop in ("zombie", "reused"):
            self.lingering.add(old)

    def daemon_stop(self, command: list[str]) -> Any:
        supervisor = {"platform": self.platform, "unit": self.unit}
        pid = self.answering_pid()
        if self.stop_may_restart or self.stop_without_supervisor:
            self.main_pid = 789
            answer: dict[str, Any] = {"ok": True, "teardownCompleted": True, "pid": pid}
            if self.stop_may_restart:
                answer["supervisorMayRestart"] = True
                answer["unattributedSupervisor"] = {
                    "platform": self.platform, "unit": self.unit,
                    "attributed": False, "ownershipReason": "foreign",
                }
            return result(command, 0, answer)
        self.stop_daemon()
        return result(
            command, 0,
            {"ok": True, "stopping": pid is not None, "pid": pid,
             "teardownCompleted": True, "supervisor": supervisor},
        )

    def daemon_start(self, command: list[str]) -> Any:
        if self.start_fails:
            return cli_error(command, "DAEMON_START_FAILED")
        if self.revive_before_start:
            self.revive_before_start = False
            self.active = True
            self.main_pid = 789
            return result(command, 0, {"ok": True, "started": False,
                                       "alreadyRunning": True, **self.ping()})
        if self.answering_pid() is not None:
            return result(command, 0, {"ok": True, "started": False,
                                       "alreadyRunning": True, **self.ping()})
        slow = self.slow_start_versions.get(self.installed_version)
        if slow is not None:
            if self.slow_start_launches:
                self.active = True
                self.main_pid = STARTED_PID
                self.stopping_since = None
                self.starting_since = self.now()
                self.starting_for = slow
            return cli_error(command, "DAEMON_START_TIMEOUT")
        self.active = True
        self.main_pid = STARTED_PID
        self.stopping_since = None
        return result(command, 0, {"ok": True, "started": True,
                                   "alreadyRunning": False, **self.ping()})

    def ps(self, command: list[str]) -> Any:
        pid = int(command[-1])
        if pid in self.lingering:
            if self.pid_after_stop == "zombie":
                return result(command, 0, f"Z    {BIRTH}\n")
            if self.pid_after_stop == "reused":
                return result(command, 0, f"Ss   {OTHER_BIRTH}\n")
            return result(command, 0, f"Ss   {BIRTH}\n")
        if pid == self.answering_pid() or (pid == self.main_pid and self.stopping()):
            return result(command, 0, f"Ssl  {BIRTH}\n")
        return result(command, 1, "")

    def systemctl(self, command: list[str]) -> Any:
        verb = command[2]
        if verb == "--version":
            return result(command, 0, "systemd 259\n")
        if verb == "show":
            if self.active and not self.stopping():
                state, sub = "active", "running"
            elif self.stopping():
                state, sub = "deactivating", "stop-sigterm"
            else:
                state = "failed" if self.stopped_state.startswith("failed") else self.stopped_state
                sub = "dead"
            main = self.main_pid or 0
            return result(
                command, 0,
                f"ActiveState={state}\nSubState={sub}\nMainPID={main}\n"
                f"NRestarts=0\nResult=success\n"
                f"Environment=HYPRIAL_HOME={self.home} SECRET_TOKEN=do-not-log\n",
            )
        if verb == "stop":
            if command[3] != self.unit:
                return result(command, 5, "", "Unit not loaded.")
            if not self.legacy:
                self.raw_stops.append(command)
            if self.hang_stop_seconds is not None:
                self.stopping_since = self.now()
                return result(command, 0)
            self.active = False
            if self.stopped_state != "failed-live":
                self.main_pid = None
            return result(command, 0)
        if verb == "start":
            if self.start_fails:
                return result(command, 1, "", "start failed")
            self.active = True
            self.main_pid = STARTED_PID
            self.stopping_since = None
            return result(command, 0)
        return result(command, 127, "", "not faked")

    def launchctl(self, command: list[str]) -> Any:
        if command[1] == "bootout":
            self.raw_stops.append(command)
            self.active = False
            self.loaded = False
            self.main_pid = None
            return result(command, 0)
        return result(command, 127, "", "not faked")

    def install(self, command: list[str]) -> Any:
        version = Path(command[-1]).name.split("-")[1]
        if version == self.fail_install_version:
            return result(command, 1, "", "no space")
        self.installed_version = version
        self.installs.append(version)
        return result(command, 0)

    def hyprial(self, command: list[str]) -> Any:
        if self.installed_version in self.broken_versions:
            return result(command, 1, "", "Traceback (most recent call last):\nImportError: boom\n")
        verb = command[1:]
        if verb[:4] == ["daemon", "service", "status", "--json"]:
            if self.legacy == "plain":
                return result(command, 2, "", "Error: No such command 'service'.\n")
            if self.legacy:
                return result(
                    command, 2,
                    {"ok": False, "code": "INVALID_ARGUMENT",
                     "error": "No such command 'service'."},
                )
            if self.status_error is not None:
                return cli_error(command, *self.status_error)
            return result(command, 0, self.status_payload())
        if verb[:3] == ["daemon", "stop", "--json"]:
            return self.daemon_stop(command)
        if verb[:3] == ["daemon", "start", "--json"]:
            return self.daemon_start(command)
        if verb[:3] == ["daemon", "status", "--json"]:
            if self.daemon_status_override is not None:
                return self.daemon_status_override(command)
            return result(command, 0, self.daemon_payload())
        if verb[:2] == ["version", "--json"]:
            origin = self.tmp_path / f"hyprial-{self.installed_version}-py3-none-any.whl"
            return result(
                command, 0,
                {"packageVersion": self.installed_version, "installOrigin": origin.as_uri()},
            )
        return result(command, 0)

    def dispatch(self, command: list[str]) -> Any:
        if command[0] == "systemctl":
            return self.systemctl(command)
        if command[0] == "launchctl":
            return self.launchctl(command)
        if command[0] == "ps":
            return self.ps(command)
        if command[:3] == ["uv", "tool", "install"]:
            return self.install(command)
        if command[0].endswith("hyprial"):
            return self.hyprial(command)
        return result(command, 0)

    def run(self, argv: Sequence[str], **kwargs: Any) -> Any:
        command = [str(item) for item in argv]
        self.calls.append(command)
        self.envs.append(kwargs.get("env"))
        self.timeouts.append(kwargs.get("timeout"))
        return self.dispatch(command)


def build_apply(
    tmp_path: Path,
    host_class: type = FakeHost,
    extra: Sequence[str] = (),
    *,
    mode: str = "apply",
    host_args: Sequence[Any] = (),
) -> tuple[Any, Any]:
    home = tmp_path / "custom-home"
    state = tmp_path / "custom-state"
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    clock = FakeClock()
    host = host_class(tmp_path, home, *host_args)
    host.clock = clock
    new_wheel = tmp_path / "hyprial-0.2.0-py3-none-any.whl"
    old_wheel = tmp_path / "hyprial-0.1.0-py3-none-any.whl"
    write_wheel(new_wheel, "0.2.0")
    write_wheel(old_wheel, "0.1.0")
    live_cli = tmp_path / "hyprial"
    live_cli.write_text("#!/bin/sh\nexit 0\n")
    live_cli.chmod(0o755)
    argv = [
        mode,
        "--new-wheel", str(new_wheel),
        "--old-wheel", str(old_wheel),
        "--wheelhouse", str(tmp_path),
        "--expect-host", "smoke-host",
        "--ready-timeout", "0.05",
        "--stop-timeout", "0.05",
        "--artifacts-dir", str(tmp_path / "artifacts"),
        "--home", str(home),
        "--state-dir", str(state),
        "--tool-dir", str(tmp_path / "tool"),
        "--bin-dir", str(tmp_path),
        "--python", sys.executable,
        *extra,
    ]
    if mode == "apply":
        argv.insert(argv.index("--ready-timeout"), "--yes")
    args = smoke.build_parser().parse_args(argv)
    instance = smoke.Smoke(args, runner=host)
    instance.clock = clock
    instance.hostname = "smoke-host"
    instance.tool_dir = tmp_path / "tool"
    instance.bin_dir = tmp_path
    instance.live_cli = live_cli
    instance.python = Path(sys.executable)
    instance.home = home
    instance.state_dir = state
    instance.new_info = {"version": "0.2.0"}
    instance.old_info = {"version": "0.1.0"}
    instance.old_wheel = old_wheel
    instance.new_wheel = new_wheel
    return instance, host


def check(instance: Any, name: str) -> Any:
    matches = [item for item in instance.checks if item.name == name]
    assert matches, f"no check {name!r} in {[c.name for c in instance.checks]}"
    return matches[-1]


def summary_of(instance: Any) -> dict[str, Any]:
    return json.loads((instance.artifacts_dir / "summary.json").read_text())
