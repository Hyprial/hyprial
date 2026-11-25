"""Stop, start and readiness of the apply flow through the hyprial service seam.

Decisions pinned here (plan sections 4.1 and 4.3, owner answers Q3, Q4, Q7):
- the daemon is stopped with ``hyprial daemon stop --json`` and started with
  ``hyprial daemon start --json`` of the live CLI, never ``systemctl``;
- a stop answer that says the manager may restart the daemon, or that does
  not name this home's supervisor, fails before anything is installed;
- a stop is proven by the service status (not running, no pid), ``daemon
  status`` (not running) and the old pid being gone: a zombie counts as gone,
  a reused pid (another birth time) counts as gone, a live pid does not;
- ``alreadyRunning: true`` from the start after the candidate install fails and
  rolls back;
- the candidate CLI must still attribute the unit before the start; otherwise
  the rollback runs without starting the candidate;
- readiness needs custody (``supervised`` with the daemon's pid); a bootstrap
  daemon status without ``version`` is read with the ``daemon start`` version;
- a stop that does not complete restarts the old daemon only after the stop
  outcome is known, never on top of a pending stop.
"""

from __future__ import annotations

import json

import pytest

from ..fakes import FIRST_PID, build_apply, check, smoke, summary_of


def test_healthy_upgrade_goes_through_hyprial_commands_only(tmp_path) -> None:
    instance, host = build_apply(tmp_path)

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_OK
    assert host.installs == ["0.2.0"]
    assert host.systemctl_calls() == []
    assert len(host.service_stops()) == 1
    assert len(host.service_starts()) == 1
    assert check(instance, "service.stop").status == smoke.PASS
    assert check(instance, "post.service").status == smoke.PASS
    assert "supervised" in check(instance, "post.service").detail
    assert summary_of(instance)["result"] == "PASS"


@pytest.mark.parametrize("flag", ["stop_may_restart", "stop_without_supervisor"])
def test_a_stop_the_manager_may_undo_fails_before_install(tmp_path, flag) -> None:
    instance, host = build_apply(tmp_path)
    setattr(host, flag, True)

    rc = instance.finish(instance.run_apply())

    assert host.installs == []
    assert check(instance, "service.stop").status == smoke.FAIL
    assert rc == smoke.EXIT_FAILED
    summary = summary_of(instance)
    assert summary["result"] == "FAILED_STOP_TIMEOUT_RESTORED"
    assert summary["rollback"]["mutated"] is False
    assert host.service_starts() == []


@pytest.mark.parametrize("after", ["zombie", "reused"])
def test_a_zombie_or_reused_pid_counts_as_stopped(tmp_path, after) -> None:
    instance, host = build_apply(tmp_path)
    host.pid_after_stop = after

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert check(instance, "service.stop").status == smoke.PASS
    assert any(c[:5] == ["ps", "-o", "stat=", "-o", "lstart="] and c[-1] == str(FIRST_PID)
               for c in host.calls)


def test_a_live_old_pid_is_not_stopped_and_nothing_is_installed(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.pid_after_stop = "alive"

    rc = instance.finish(instance.run_apply())

    assert host.installs == []
    assert check(instance, "service.stop").status == smoke.FAIL
    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert summary_of(instance)["result"] == "FAILED_STOP_TIMEOUT_RESTORE_FAILED"
    assert host.service_starts() == []


def test_the_process_probe_never_asks_for_command_lines(tmp_path) -> None:
    instance, host = build_apply(tmp_path)

    instance.run_apply()

    probes = [command for command in host.calls if command[0] == "ps"]
    assert probes
    for command in probes:
        assert command[:6] == ["ps", "-o", "stat=", "-o", "lstart=", "-p"]
        assert len(command) == 7


def test_already_running_after_the_stop_fails_and_rolls_back(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.revive_before_start = True

    rc = instance.run_apply()

    assert check(instance, "apply-start").status == smoke.FAIL
    assert "alreadyRunning" in check(instance, "apply-start").detail
    assert host.installs == ["0.2.0", "0.1.0"]
    assert instance.rollback.get("attempted") is True
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"


def test_candidate_that_no_longer_attributes_the_unit_rolls_back_without_start(
    tmp_path,
) -> None:
    instance, host = build_apply(tmp_path)
    host.unattributed_versions = {"0.2.0"}

    rc = instance.run_apply()

    assert check(instance, "service.attributed-after-install").status == smoke.FAIL
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.installs == ["0.2.0", "0.1.0"]
    starts = host.service_starts()
    assert len(starts) == 1
    assert host.installed_version == "0.1.0" and host.active is True


def test_bootstrap_daemon_status_without_version_uses_the_start_version(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.bootstrap = True

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    ready = check(instance, "daemon.ready")
    assert ready.status == smoke.PASS
    assert ready.data.get("versionSource") == "daemon start"
    assert ready.data.get("version") == "0.2.0"


def test_readiness_requires_custody(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    original = host.daemon_start

    def start_then_detach(command):
        answer = original(command)
        if host.installed_version == "0.2.0":
            host.detached_pid = 4242
        else:
            host.detached_pid = None
        return answer

    host.daemon_start = start_then_detach

    rc = instance.run_apply()

    ready_checks = [c for c in instance.checks if c.name == "daemon.ready"]
    assert ready_checks[0].status == smoke.FAIL
    assert "supervis" in ready_checks[0].detail
    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert instance.result == "ROLLBACK_FAILED"


def test_a_stop_that_hangs_restarts_the_old_daemon_after_the_stop_ends(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.hang_stop_seconds = 45.0

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_FAILED
    assert host.installs == []
    assert check(instance, "service.stop").status == smoke.FAIL
    assert check(instance, "restore.start").status == smoke.PASS
    assert len(host.service_starts()) == 1
    start_index = host.calls.index(host.service_starts()[0])
    assert host.stopping_since is None
    assert start_index > 0
    summary = summary_of(instance)
    assert summary["result"] == "FAILED_STOP_TIMEOUT_RESTORED"
    assert summary["rollback"]["mutated"] is False
    assert host.active is True and host.installed_version == "0.1.0"


def test_a_stop_that_never_ends_is_manual_work_without_a_start(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.hang_stop_seconds = float("inf")

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert host.service_starts() == []
    assert summary_of(instance)["result"] == "FAILED_STOP_TIMEOUT_RESTORE_FAILED"


def test_a_failed_restart_after_a_hung_stop_exits_three(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.hang_stop_seconds = 45.0
    host.start_fails = True

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert host.installs == []
    assert summary_of(instance)["result"] == "FAILED_STOP_TIMEOUT_RESTORE_FAILED"


def test_the_stop_command_gets_at_least_the_unit_stop_budget(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    seen = {}
    original = host.run

    def run(argv, **kwargs):
        if list(map(str, argv))[1:4] == ["daemon", "stop", "--json"]:
            seen["timeout"] = kwargs.get("timeout")
        return original(argv, **kwargs)

    host.run = run
    instance.run_apply()

    assert seen["timeout"] >= 180


def test_commands_log_records_the_service_status_payload(tmp_path) -> None:
    instance, host = build_apply(tmp_path)

    instance.finish(instance.run_apply())

    commands = json.loads((instance.artifacts_dir / "commands.json").read_text())
    labels = [item["label"] for item in commands]
    assert any(label.startswith("service-status") for label in labels)
    assert "service-stop" in labels and "apply-start" in labels
