"""A start that outlives the CLI's own readiness window, and honest deadlines.

Measured on a real host (2026-11-21, heavy unrelated load): ``hyprial daemon
start --json`` answered ``DAEMON_START_TIMEOUT`` (rc 1) after its own 30 s idle
window while the unit was running and the daemon was still in a startup phase
that took 107 s; the status meanwhile read ``running: true``, a pid,
``servingPid: null``, ``supervision: "lost"``.

Decisions pinned here:
- ``DAEMON_START_TIMEOUT`` is "launched, not confirmed": when the service
  status shows the unit running with a pid, the script keeps waiting for the
  usual readiness proof within ``--ready-timeout`` (counted from the start
  command), on ``apply-start`` and ``rollback-start``;
- with nothing running it fails at once; every other non-ok answer and
  ``alreadyRunning`` stay failures;
- the readiness wait never sleeps past its deadline and reports the real
  elapsed time.
"""

from __future__ import annotations

from ..fakes import build_apply, check, smoke, summary_of


def test_a_slow_candidate_start_becomes_ready_within_the_deadline(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "180"])
    host.slow_start_versions = {"0.2.0": 107.0}

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_OK
    assert instance.result == "PASS"
    assert host.installs == ["0.2.0"]
    start = check(instance, "apply-start")
    assert start.status == smoke.PASS
    assert "DAEMON_START_TIMEOUT" in start.detail
    assert start.data["answer"]["code"] == "DAEMON_START_TIMEOUT"
    assert check(instance, "daemon.ready").status == smoke.PASS
    assert instance.rollback == {}
    summary = summary_of(instance)
    data = [c["data"] for c in summary["checks"] if c["name"] == "apply-start"][0]
    assert data["answer"]["code"] == "DAEMON_START_TIMEOUT"


def test_a_slow_rollback_start_still_rolls_back(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "180"])
    host.slow_start_versions = {"0.1.0": 107.0}
    host.unattributed_versions = {"0.2.0"}

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.installs == ["0.2.0", "0.1.0"]
    assert check(instance, "rollback-start").status == smoke.PASS
    assert "DAEMON_START_TIMEOUT" in check(instance, "rollback-start").detail
    assert check(instance, "daemon.ready").status == smoke.PASS


def test_a_slow_start_that_never_becomes_ready_fails_at_the_deadline(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "60"])
    host.slow_start_versions = {"0.2.0": float("inf")}
    marks: dict[str, float] = {}
    original = instance.check

    def recording(name, status, detail, data=None):
        marks.setdefault(f"{name}:{status}", instance.clock.monotonic())
        return original(name, status, detail, data)

    instance.check = recording

    rc = instance.finish(instance.run_apply())

    assert check(instance, "apply-start").status == smoke.PASS
    ready = [c for c in instance.checks if c.name == "daemon.ready"][0]
    assert ready.status == smoke.FAIL
    elapsed = marks["daemon.ready:FAIL"] - marks["apply-start:PASS"]
    assert elapsed <= 60.0
    assert elapsed >= 59.0
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"


def test_a_start_timeout_with_nothing_running_fails_at_once(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "180"])
    host.slow_start_versions = {"0.2.0": 10.0}
    host.slow_start_launches = False
    before = {}
    original = instance.check

    def recording(name, status, detail, data=None):
        if name == "apply-start":
            before["at"] = instance.clock.monotonic()
        return original(name, status, detail, data)

    instance.check = recording
    started = instance.clock.monotonic()

    rc = instance.finish(instance.run_apply())

    start = check(instance, "apply-start")
    assert start.status == smoke.FAIL
    assert "DAEMON_START_TIMEOUT" in start.detail
    assert start.data["answer"]["code"] == "DAEMON_START_TIMEOUT"
    assert before["at"] - started < 5
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"


def test_another_start_error_stays_a_failure(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "180"])
    host.start_fails = True

    rc = instance.finish(instance.run_apply())

    assert check(instance, "apply-start").status == smoke.FAIL
    assert check(instance, "apply-start").data["answer"]["code"] == "DAEMON_START_FAILED"
    assert rc == smoke.EXIT_ROLLBACK_FAILED


def test_the_ready_deadline_is_honest(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "1"])
    original = host.answering_pid

    def candidate_never_answers():
        if host.installed_version == "0.2.0":
            return None
        return original()

    host.answering_pid = candidate_never_answers
    marks: dict[str, float] = {}
    original_check = instance.check

    def recording(name, status, detail, data=None):
        marks.setdefault(f"{name}:{status}", instance.clock.monotonic())
        return original_check(name, status, detail, data)

    instance.check = recording

    instance.finish(instance.run_apply())

    ready = [c for c in instance.checks if c.name == "daemon.ready"][0]
    assert ready.status == smoke.FAIL
    assert marks["daemon.ready:FAIL"] - marks["apply-start:PASS"] <= 1.0
    assert "1s" in ready.detail


def test_daemon_start_runner_timeout_exceeds_product_hard_cap_while_honoring_ready_timeout(
    tmp_path,
) -> None:
    instance, host = build_apply(tmp_path, extra=["--ready-timeout", "30"])
    host.slow_start_versions = {"0.2.0": 90.0}
    marks: dict[str, float] = {}
    original_check = instance.check

    def recording(name, status, detail, data=None):
        marks.setdefault(f"{name}:{status}", instance.clock.monotonic())
        return original_check(name, status, detail, data)

    instance.check = recording

    rc = instance.finish(instance.run_apply())

    start_timeout = host.timeout_of(["daemon", "start", "--json"])
    assert start_timeout is not None and start_timeout >= 150.0
    assert check(instance, "apply-start").status == smoke.PASS
    ready = [c for c in instance.checks if c.name == "daemon.ready"][0]
    assert ready.status == smoke.FAIL
    elapsed = marks["daemon.ready:FAIL"] - marks["apply-start:PASS"]
    assert 29.0 <= elapsed <= 30.0
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
