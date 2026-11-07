from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.operations import service as service_module
from hyprial.daemon.impl.operations.service import (
    USER_BUS_ADDRESS_UNREACHABLE_REASON,
    USER_BUS_UNAVAILABLE_REASON,
    ServiceCommandError,
    ServiceConfig,
    ServiceManager,
    ServiceOwnershipError,
    session_bus_environment,
    user_bus_state,
)


def completed(command, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(tuple(command), returncode, stdout, stderr)


class LaunchdSupervisor:
    def __init__(self, *, job_pid: int = 4202, runs: int = 1) -> None:
        self.job_pid = job_pid
        self.runs = runs
        self.loaded = True
        self.commands: list[tuple[str, ...]] = []

    def __call__(
        self, command, *, timeout: float | None = None
    ):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[:2] == ("launchctl", "print"):
            if not self.loaded:
                return completed(
                    command,
                    113,
                    stderr=(
                        'Could not find service "com.hyprial.hyprial.daemon" '
                        "in domain for user gui: 501"
                    ),
                )
            return completed(
                command,
                stdout=(
                    "state = running\n"
                    f"\tpid = {self.job_pid}\n"
                    f"\truns = {self.runs}\n"
                    "\tlast exit code = 0\n"
                ),
            )
        if command[:2] == ("launchctl", "kickstart"):
            self.job_pid += 1
            self.runs += 1
        if command[:2] == ("launchctl", "bootout"):
            self.loaded = False
        if command[:2] == ("launchctl", "bootstrap"):
            self.loaded = True
        return completed(command)


def manager(tmp_path: Path, runner: LaunchdSupervisor) -> ServiceManager:
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path,
        hyprial_home=tmp_path / ".hyprial",
        state_dir=tmp_path / "state",
    )
    value = ServiceManager(
        config,
        platform="launchd",
        service_home=tmp_path,
        runner=runner,
        uid=501,
        sleep=lambda _seconds: None,
    )
    value.unit_path.parent.mkdir(parents=True)
    value.unit_path.write_text(value.templates.render_launchd(config), encoding="utf-8")
    return value


def test_supervised_restart_uses_launchd_and_verifies_serving_pid(
    tmp_path: Path,
) -> None:
    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)

    status = service.restart(lambda: runner.job_pid)

    assert status.supervision == "supervised"
    assert status.pid == 4203
    assert (
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.hyprial.hyprial.daemon",
    ) in runner.commands


def test_supervised_restart_can_use_a_longer_custody_timeout(
    tmp_path: Path,
) -> None:
    """Upgrade can wait beyond the ordinary 40 custody polls."""

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    probes = 0

    def serving_pid(_timeout: float = 1.0) -> int:
        nonlocal probes
        probes += 1
        return runner.job_pid if probes > 45 else 9999

    status = service.restart(serving_pid, timeout_seconds=12.0)

    assert probes > 40
    assert status.supervision == "supervised"
    assert status.pid == 4203


def test_supervised_restart_wall_clock_cap_bounds_full_timeout_probes(
    tmp_path: Path,
) -> None:
    """A 1s cap cannot become six seconds of probes plus sleeps."""

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)

    def slow_probe(timeout: float = 1.0) -> None:
        time.sleep(timeout)
        return None

    started = time.monotonic()
    with pytest.raises(ServiceCommandError) as caught:
        service.restart(slow_probe, timeout_seconds=1.0)
    elapsed = time.monotonic() - started

    assert caught.value.reason == "supervisor-start-hard-cap"
    assert elapsed <= 1.5


def test_supervised_restart_reports_lost_when_socket_pid_is_not_job_or_child(
    tmp_path: Path,
) -> None:
    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)

    status = service.restart(lambda: 9999)

    assert status.supervision == "lost"
    assert status.serving_pid == 9999
    assert status.pid == 4203


def test_service_status_detects_template_and_runtime_drift(tmp_path: Path) -> None:
    runner = LaunchdSupervisor(runs=12)
    service = manager(tmp_path, runner)
    service.unit_path.write_text(
        service.unit_path.read_text(encoding="utf-8").replace(
            "ThrottleInterval", "OldThrottleInterval"
        ),
        encoding="utf-8",
    )
    (service.config.state_dir / "daemon-service.stderr.log").parent.mkdir(parents=True)
    (service.config.state_dir / "daemon-service.stderr.log").write_text(
        "DAEMON_OWNERSHIP_BUSY: another daemon holds daemon.lock\n", encoding="utf-8"
    )

    status = service.inspect(serving_pid=9001)

    assert status.supervision == "lost"
    assert status.template_matches is False
    assert status.relaunch_count == 12
    assert status.ownership_busy is True


def test_reconcile_stops_stray_reinstalls_with_backup_and_verifies_pid(
    tmp_path: Path,
) -> None:
    runner = LaunchdSupervisor(runs=12)
    service = manager(tmp_path, runner)
    original = service.unit_path.read_text(encoding="utf-8")
    service.unit_path.write_text(
        original.replace("ThrottleInterval", "OldThrottleInterval"), encoding="utf-8"
    )
    serving = [9001]
    stops: list[int] = []

    def stop_stray(pid: int) -> None:
        stops.append(pid)
        serving[0] = runner.job_pid

    fixed = service.reconcile(lambda: serving[0], stop_stray=stop_stray)

    assert fixed.supervision == "supervised"
    assert fixed.template_matches is True
    assert stops == [9001]
    backups = list(service.unit_path.parent.glob(service.unit_path.name + ".backup.*"))
    assert len(backups) == 1
    assert "OldThrottleInterval" in backups[0].read_text(encoding="utf-8")


def test_systemd_restart_rejects_a_direct_child_of_the_manager_job(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    job_pid = [5202]

    def runner(command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        commands.append(command)
        if command[:3] == ("systemctl", "--user", "show"):
            return completed(
                command,
                stdout=(
                    "LoadState=loaded\nActiveState=active\nSubState=running\n"
                    f"MainPID={job_pid[0]}\nNRestarts=2\nExecMainStatus=0\n"
                ),
            )
        if command[:3] == ("systemctl", "--user", "restart"):
            job_pid[0] = 5203
        if command[:2] == ("loginctl", "show-user"):
            return completed(command, stdout="yes\n")
        return completed(command)

    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path,
        hyprial_home=tmp_path / ".hyprial",
        state_dir=tmp_path / "state",
    )
    service = ServiceManager(
        config,
        platform="systemd",
        service_home=tmp_path,
        runner=runner,
        username="allen",
        parent_pid=lambda pid: job_pid[0] if pid == 6203 else None,
        sleep=lambda _seconds: None,
    )
    service.unit_path.parent.mkdir(parents=True)
    service.unit_path.write_text(
        service.templates.render_systemd(config), encoding="utf-8"
    )

    observed = service.restart(lambda: 6203)

    assert observed.supervision == "lost"
    assert observed.pid == 5203
    assert ("systemctl", "--user", "restart", "hyprial.service") in commands


def test_restart_rejects_an_unchanged_manager_job_pid(tmp_path: Path) -> None:
    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    before_pid = runner.job_pid

    def unchanged(command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        if command[:2] == ("launchctl", "kickstart"):
            runner.commands.append(command)
            runner.runs += 1
            return completed(command)
        return runner(command)

    service.runner = unchanged
    observed = service.restart(lambda: before_pid)

    assert observed.supervision == "lost"
    assert observed.pid == before_pid
    assert observed.serving_pid == before_pid


@pytest.mark.parametrize(
    ("scope", "failure_reason"),
    [
        ("unit-owner", "unit-owner-mismatch"),
        ("unit-mode", "unit-permissions-unsafe"),
        ("directory-owner", "unit-directory-owner-mismatch"),
        ("directory-mode", "unit-directory-permissions-unsafe"),
    ],
)
def test_service_custody_requires_safe_filesystem_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
    failure_reason: str,
) -> None:
    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    target = service.unit_path.parent if scope.startswith("directory") else service.unit_path
    if scope.endswith("mode"):
        target.chmod(0o775 if target.is_dir() else 0o664)
    else:
        original_stat = Path.stat

        def stat_with_foreign_owner(
            path: Path, *, follow_symlinks: bool = True
        ) -> os.stat_result:
            observed = original_stat(path, follow_symlinks=follow_symlinks)
            if path == target:
                values = list(observed)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return observed

        monkeypatch.setattr(Path, "stat", stat_with_foreign_owner)

    observed = service.inspect(serving_pid=runner.job_pid)

    assert observed.attributed is False
    assert observed.supervision == "lost"
    assert observed.ownership_reason == failure_reason


def test_reconcile_never_adopts_a_loaded_label_without_owned_file(
    tmp_path: Path,
) -> None:
    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    stops: list[int] = []

    with pytest.raises(ServiceOwnershipError):
        service.reconcile(lambda: 9001, stop_stray=stops.append)

    assert stops == []
    assert not service.unit_path.exists()


@pytest.mark.parametrize(
    ("unsafe_scope", "failure_reason"),
    [
        ("mode", "unit-directory-permissions-unsafe"),
        ("owner", "unit-directory-owner-mismatch"),
    ],
)
def test_first_install_refuses_unsafe_existing_unit_directory_before_write_or_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_scope: str,
    failure_reason: str,
) -> None:
    runner = LaunchdSupervisor()
    runner.loaded = False
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    if unsafe_scope == "mode":
        service.unit_path.parent.chmod(0o777)
    else:
        target = service.unit_path.parent
        original_stat = Path.stat

        def stat_with_foreign_owner(
            path: Path, *, follow_symlinks: bool = True
        ) -> os.stat_result:
            observed = original_stat(path, follow_symlinks=follow_symlinks)
            if path == target:
                values = list(observed)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return observed

        monkeypatch.setattr(Path, "stat", stat_with_foreign_owner)

    with pytest.raises(ServiceOwnershipError) as caught:
        service.install()

    assert caught.value.reason == failure_reason
    assert not service.unit_path.exists()
    assert not any(command[:2] == ("launchctl", "bootstrap") for command in runner.commands)


def test_foreign_service_file_is_never_replaced_or_removed(tmp_path: Path) -> None:
    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    foreign = service.unit_path.read_text(encoding="utf-8").replace(
        "/opt/hyprial/bin/hyprial", "/tmp/foreign/hyprial"
    )
    service.unit_path.write_text(foreign, encoding="utf-8")

    with pytest.raises(ServiceOwnershipError):
        service.install()
    with pytest.raises(ServiceOwnershipError):
        service.uninstall()

    assert service.unit_path.read_text(encoding="utf-8") == foreign

def test_a_foreign_labelled_launchd_job_is_found_and_restarted(
    tmp_path: Path,
) -> None:
    """The supervisor is identified by what it runs, not by our own label.

    Production (2026-10-29) runs an operator-wrapped job named
    ``com.hyprial.daemon`` whose ProgramArguments shell out to
    ``hyprial daemon run``. Our label was never loaded, so inspect() reported an
    uninstalled supervisor and the restart spawned an unsupervised daemon
    (parent 1) while launchd's process idled at 0% CPU. Detection must follow
    the command line, and the restart must go through launchd.
    """

    runner = LaunchdSupervisor(job_pid=72472, runs=2900)
    service = manager(tmp_path, runner)
    # Keep our own service file out of the picture: production never loaded it.
    service.unit_path.unlink()
    foreign_label = "com.hyprial.daemon"
    foreign_plist = service.unit_path.parent / f"{foreign_label}.plist"
    foreign_plist.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<plist version=\"1.0\"><dict>\n"
        f"  <key>Label</key><string>{foreign_label}</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        "    <string>/bin/bash</string><string>-lc</string>\n"
        "    <string>ulimit -n 65536; exec /opt/hyprial/bin/hyprial daemon run --json</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "</dict></plist>\n",
        encoding="utf-8",
    )

    status = service.status()

    assert (status.installed, status.loaded, status.running) == (True, True, True), (
        "a job that runs our daemon must count as our supervisor: " f"{status}"
    )
    assert status.unit == foreign_label, status
    assert status.unit_path == foreign_plist, status
    inspected = service.inspect(serving_pid=runner.job_pid)
    assert inspected.supervision == "supervised", inspected

    restarted = service.restart(lambda: runner.job_pid)

    assert (
        "launchctl",
        "kickstart",
        "-k",
        f"gui/501/{foreign_label}",
    ) in runner.commands, runner.commands
    assert restarted.supervision == "supervised", restarted

def test_a_supervised_daemon_waits_for_the_lock_instead_of_exiting(
    tmp_path: Path,
) -> None:
    """Launchd's KeepAlive turned "lock busy" into 2,900 restarts.

    Allen's ruling (2026-10-29): block while a healthy daemon owns the state and
    take over when it exits; do not exit, because exiting is what asks launchd
    to start the same losing race again. The wait must be observable
    (waiting-for-lock with the holder pid), and a lock held with NO answering
    daemon must still fail fast -- that state is broken, not busy.
    """

    from hyprial.daemon import DaemonOwnershipBusy, wait_for_state_ownership

    attempts = {"count": 0}
    fence = object()

    def acquire(_state_dir):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise DaemonOwnershipBusy("held")
        return fence

    events: list[dict[str, object]] = []
    sleeps: list[float] = []

    taken = wait_for_state_ownership(
        tmp_path,
        acquire=acquire,
        probe_holder=lambda: 72472,
        sleep=sleeps.append,
        log=events.append,
        poll_seconds=0.5,
    )

    assert taken is fence
    assert attempts["count"] == 3, attempts
    assert sleeps == [0.5, 0.5], sleeps
    assert [event["event"] for event in events] == [
        "waiting-for-lock",
        "waiting-for-lock",
    ], events
    assert {event["holder_pid"] for event in events} == {72472}, events

    with pytest.raises(DaemonOwnershipBusy):
        wait_for_state_ownership(
            tmp_path,
            acquire=lambda _state_dir: (_ for _ in ()).throw(
                DaemonOwnershipBusy("held with no daemon answering")
            ),
            probe_holder=lambda: None,
            sleep=lambda _seconds: None,
            log=events.append,
        )

def test_daemon_run_waits_instead_of_failing_when_the_lock_is_busy(
    tmp_path: Path,
) -> None:
    """The CLI wiring: a busy lock with a healthy daemon is a wait, not an exit.

    This is the shape production hit: launchd started the daemon, the previous
    process still held the state lock, `daemon run` failed immediately and
    KeepAlive restarted it (runs=2900). The wiring must report
    waiting-for-lock on every poll and only fail when nothing answers.
    """

    from hyprial.shell.impl.cli.commands.daemon import commands as daemon_commands
    from hyprial.daemon import DaemonOwnershipBusy, DaemonStateOwnershipFence

    attempts = {"count": 0}
    fence = object()

    def fake_acquire(_path, *, timeout: float = 0.0):
        attempts["count"] += 1
        if timeout > 0:
            raise DaemonOwnershipBusy("held by the outgoing daemon")
        if attempts["count"] <= 3:
            raise DaemonOwnershipBusy("held by the outgoing daemon")
        return fence

    events: list[dict[str, object]] = []
    service = SimpleNamespace(
        _daemon_probe=lambda timeout=0.5: {"daemon": {"pid": 72472}},
        CliError=type("CliError", (Exception,), {}),
    )
    original = DaemonStateOwnershipFence.acquire
    DaemonStateOwnershipFence.acquire = staticmethod(fake_acquire)  # type: ignore[assignment]
    try:
        taken = daemon_commands._acquire_daemon_fence(
            service,
            tmp_path,
            timeout=0.1,
            sleep=lambda _seconds: None,
            log=events.append,
        )
    finally:
        DaemonStateOwnershipFence.acquire = original  # type: ignore[assignment]

    assert taken is fence, taken
    assert [event["event"] for event in events] == [
        "waiting-for-lock",
        "waiting-for-lock",
    ], events


# --- headless user-bus derivation -------------------------------------------
#
# ``systemctl --user`` needs XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS. A process
# started outside a login session (systemd unit, cron, plain ssh command) has
# neither, and reporting that as a broken service sends the reader to the wrong
# place: the remedy is enabling lingering, not reinstalling the unit.


def test_session_bus_environment_fills_missing_runtime_dir(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "user" / "1000"
    runtime.mkdir(parents=True)
    (runtime / "bus").write_text("")
    monkey = {"XDG_RUNTIME_DIR": str(runtime)}

    environment = session_bus_environment(monkey, uid=1000)

    assert environment["XDG_RUNTIME_DIR"] == str(runtime)
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"


def test_session_bus_environment_respects_explicit_bus(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bus").write_text("")
    explicit = "unix:path=/somewhere/else"

    environment = session_bus_environment(
        {"XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": explicit},
        uid=1000,
    )

    assert environment["DBUS_SESSION_BUS_ADDRESS"] == explicit


def test_session_bus_environment_leaves_absent_socket_alone(
    tmp_path: Path,
) -> None:
    # No bus socket on disk: inventing an address would turn "no session" into
    # "connection refused", which reads like a different bug.
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    environment = session_bus_environment({"XDG_RUNTIME_DIR": str(runtime)}, uid=1000)

    assert "DBUS_SESSION_BUS_ADDRESS" not in environment


def test_systemd_no_session_reports_enable_linger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _systemd_manager(
        lambda command: subprocess.CompletedProcess(
            tuple(command), 5, "", "Failed to connect to user scope bus"
        )
    )
    monkeypatch.setattr(
        service_module, "user_bus_state", lambda *a, **k: "no-session"
    )

    with pytest.raises(ServiceCommandError) as error:
        manager._require(("systemctl", "--user", "show", "hyprial.service"))

    assert getattr(error.value, "reason", None) == USER_BUS_UNAVAILABLE_REASON
    assert "loginctl enable-linger" in str(error.value)


def test_systemd_explicit_address_unreachable_does_not_suggest_linger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An explicit DBUS_SESSION_BUS_ADDRESS that points nowhere is a different
    # fault: lingering cannot fix it, so the message must not claim it would.
    manager = _systemd_manager(
        lambda command: subprocess.CompletedProcess(
            tuple(command), 1, "", "Failed to connect to bus: No such file or directory"
        )
    )
    monkeypatch.setattr(
        service_module, "user_bus_state", lambda *a, **k: "address-unreachable"
    )

    with pytest.raises(ServiceCommandError) as error:
        manager._require(("systemctl", "--user", "show", "hyprial.service"))

    assert (
        getattr(error.value, "reason", None)
        == USER_BUS_ADDRESS_UNREACHABLE_REASON
    )
    assert "loginctl enable-linger" not in str(error.value)


def test_user_bus_state_classifies_without_reading_output(tmp_path: Path) -> None:
    # ready: configured address whose socket exists
    socket_path = tmp_path / "bus"
    socket_path.write_text("")
    assert (
        user_bus_state({"DBUS_SESSION_BUS_ADDRESS": f"unix:path={socket_path}"})
        == "ready"
    )
    # address-unreachable: configured address whose socket is missing
    assert (
        user_bus_state(
            {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path / 'gone'}"}
        )
        == "address-unreachable"
    )
    # no-session: nothing configured, and the uid has no runtime socket
    assert user_bus_state({}, uid=4294967290) == "no-session"
    # ready: nothing configured, but the uid's runtime socket exists
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "bus").write_text("")
    assert user_bus_state({"XDG_RUNTIME_DIR": str(runtime)}) == "ready"


def _systemd_manager(runner):
    return ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=Path("/home/hyprial"),
            hyprial_home=Path("/home/hyprial/.hyprial"),
            state_dir=Path("/home/hyprial/.hyprial/state"),
        ),
        platform="systemd",
        service_home=Path("/home/hyprial"),
        runner=runner,
        uid=1000,
        username="hyprial",
    )
