"""Install waits for takeover by startup progress, not by a fixed count (N2).

Every clock here is fake: ``sleep`` advances it, so a 60 s start costs no
wall time. Real launchd/systemd behaviour is not exercised.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import daemon_start
from hyprial.shell.impl.cli.commands.daemon import service as service_cli
from hyprial.shell.impl.cli.commands.daemon import unit_observation
from hyprial.shell.impl.cli.commands.common.services import get_services

UNIT = "com.hyprial.hyprial.daemon"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Job:
    """A launchd fake whose job state is a function of the fake clock."""

    def __init__(self, clock: Clock, unit_path: Path) -> None:
        self.clock = clock
        self.unit_path = unit_path
        self.loaded = False
        self.job_pid = 0
        self.spawned_at = 0.0
        self.exits_at: float | None = None
        self.spawn_delay = 0.0
        self.commands: list[tuple[str, ...]] = []

    def spawn(self, pid: int) -> None:
        self.job_pid = pid
        self.spawned_at = self.clock.now + self.spawn_delay
        self.exits_at = None

    def running(self) -> bool:
        return (
            self.loaded
            and self.clock.now >= self.spawned_at
            and (self.exits_at is None or self.clock.now < self.exits_at)
        )

    def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[:2] == ("launchctl", "print"):
            if not self.loaded:
                return subprocess.CompletedProcess(
                    command, 113, "", f'Could not find service "{UNIT}" in domain for user gui: 501'
                )
            state = (
                f"state = running\n\tpid = {self.job_pid}\n"
                if self.running()
                else "state = spawn scheduled\n"
            )
            return subprocess.CompletedProcess(
                command, 0, f"path = {self.unit_path}\n{state}\truns = 1\n", ""
            )
        if command[:2] == ("launchctl", "bootstrap"):
            self.loaded = True
            self.spawn(self.job_pid + 1)
        if command[:2] == ("launchctl", "bootout"):
            self.loaded = False
        return subprocess.CompletedProcess(command, 0, "", "")

    def mutations(self, verb: str) -> int:
        return sum(1 for c in self.commands if c[:2] == ("launchctl", verb))


def build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    progress_every: float | None,
    start_seconds: float,
) -> tuple[ServiceManager, Job, Clock]:
    clock = Clock()
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path,
        hyprial_home=tmp_path / ".hyprial",
        state_dir=tmp_path / "state",
    )
    manager = ServiceManager(
        config,
        platform="launchd",
        service_home=tmp_path,
        uid=501,
        sleep=clock.sleep,
    )
    job = Job(clock, manager.unit_path)
    manager.runner = job
    job.job_pid = 100
    services = get_services()
    monkeypatch.setattr(
        services, "time", SimpleNamespace(monotonic=clock, sleep=clock.sleep)
    )

    def probe(timeout: float = 0.5) -> dict[str, object]:
        del timeout
        if job.running() and clock.now - job.spawned_at >= start_seconds:
            return {"running": True}
        raise ipc_errors.DaemonUnavailableError("daemon is not ready")

    monkeypatch.setattr(services, "_daemon_probe", probe)

    def phases(_state_dir: Path, *, spawned_at: object) -> dict[str, object]:
        del spawned_at
        if progress_every is None:
            return {"lastStartupPhase": None, "phasesSeen": 0}
        return {
            "lastStartupPhase": "phase",
            "phasesSeen": int(clock.now / progress_every),
        }

    monkeypatch.setattr(daemon_start, "_daemon_startup_phase_summary", phases)
    manager.await_startup = unit_observation._startup_waiter(tmp_path / "state")
    monkeypatch.setattr(
        service_cli, "_serving_daemon_pid", serving_after(job, clock, start_seconds)
    )
    return manager, job, clock


def preinstall(manager: ServiceManager, job: Job, *, previous: bool) -> str | None:
    if not previous:
        return None
    text = manager._rendered().replace("ThrottleInterval", "ThrottleIntervalOld")
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text(text, encoding="utf-8")
    job.loaded = True
    return text


def serving_after(job: Job, clock: Clock, seconds: float) -> Callable[..., int | None]:
    def serving(_timeout: float = 1.0) -> int | None:
        if job.running() and clock.now - job.spawned_at >= seconds:
            return job.job_pid
        return None

    return serving


def lost(caught: pytest.ExceptionInfo, *, reverted: str) -> None:
    assert caught.value.code == "SERVICE_SUPERVISION_LOST"
    assert caught.value.data["install"] == {
        "supervision": "lost",
        "reverted": reverted,
    }


def test_a_start_slower_than_the_old_window_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=5.0, start_seconds=60.0
    )

    payload = service_cli._install_status(manager)

    assert payload["supervision"] == "supervised"
    assert payload["changed"] is True
    assert clock.now >= 60.0
    assert job.mutations("bootout") == 0


def test_a_spawn_delayed_by_eight_seconds_is_not_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=5.0, start_seconds=3.0
    )
    job.spawn_delay = 8.0

    payload = service_cli._install_status(manager)

    assert payload["supervision"] == "supervised"
    assert clock.now >= 11.0
    assert job.mutations("bootout") == 0


def test_a_job_that_never_spawns_is_lost_at_the_idle_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=None, start_seconds=3.0
    )
    job.spawn_delay = 10_000.0

    with pytest.raises(get_services().CliError) as caught:
        service_cli._install_status(manager)

    lost(caught, reverted="removed")
    assert 30.0 <= clock.now < 40.0
    assert not manager.unit_path.exists()


@pytest.mark.parametrize("previous", [False, True])
def test_a_job_that_ran_and_exited_is_lost_within_the_short_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: bool
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=3.0, start_seconds=60.0
    )
    text = preinstall(manager, job, previous=previous)
    real = job.__call__

    def runner(command, *, timeout=None):  # type: ignore[no-untyped-def]
        result = real(command, timeout=timeout)
        if tuple(command[:2]) == ("launchctl", "bootstrap"):
            job.exits_at = clock.now + 1.0
        return result

    manager.runner = runner

    with pytest.raises(get_services().CliError) as caught:
        service_cli._install_status(manager)

    lost(caught, reverted="restored" if previous else "removed")
    assert clock.now <= 8.0
    if previous:
        assert manager.unit_path.read_text(encoding="utf-8") == text
    else:
        assert not manager.unit_path.exists()


def test_an_alive_job_without_progress_is_lost_at_the_idle_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=None, start_seconds=1000.0
    )

    with pytest.raises(get_services().CliError) as caught:
        service_cli._install_status(manager)

    lost(caught, reverted="removed")
    assert 30.0 <= clock.now < 31.0


def test_continuous_progress_still_stops_at_the_hard_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=5.0, start_seconds=10_000.0
    )

    with pytest.raises(get_services().CliError) as caught:
        service_cli._install_status(manager)

    lost(caught, reverted="removed")
    assert 120.0 <= clock.now < 121.0


def test_a_different_daemon_answering_keeps_its_old_meaning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=5.0, start_seconds=0.0
    )
    monkeypatch.setattr(service_cli, "_serving_daemon_pid", lambda: 9999)

    with pytest.raises(get_services().CliError) as caught:
        service_cli._install_status(manager)

    lost(caught, reverted="removed")
    assert clock.now < 11.0


def test_rewriting_a_loaded_unit_succeeds_after_a_14_second_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=3.0, start_seconds=14.0
    )
    previous = preinstall(manager, job, previous=True)

    payload = service_cli._install_status(manager)

    assert payload["supervision"] == "supervised"
    assert payload["changed"] is True
    assert manager.unit_path.read_text(encoding="utf-8") != previous
    assert job.mutations("bootout") == 1
    assert job.mutations("bootstrap") == 1


def test_an_unchanged_install_during_a_start_waits_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job, clock = build(
        tmp_path, monkeypatch, progress_every=3.0, start_seconds=20.0
    )
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text(manager._rendered(), encoding="utf-8")
    job.loaded = True
    job.spawn(300)

    payload = service_cli._install_status(manager)

    assert payload["supervision"] == "supervised"
    assert payload["changed"] is False
    assert job.mutations("bootout") == 0
    assert job.mutations("bootstrap") == 0
