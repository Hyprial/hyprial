"""Rollback through the service seam, and the service log tails.

Decisions pinned here (plan sections 4.2 and 4.5, owner answer Q4):
- rollback keeps its order: stop with the live (candidate) CLI, reinstall the
  old wheel, require the old CLI to attribute the unit, start, wait ready;
- only before a candidate start attempt (``not candidate_started``), when the
  candidate CLI cannot answer with a JSON object (or reports a drifted
  identity), the rollback stops the unit recorded at baseline directly
  (``systemctl --user stop <unit>`` / ``launchctl bootout gui/<uid>/<label>``);
  once candidate start has been attempted, unreadable service-status/stop
  responses or baseline identity drift fail ``service.stop`` and end in
  ``ROLLBACK_FAILED`` without calling ``raw_stop``, reinstalling, or claiming
  ``ROLLED_BACK``; this raw stop exists only in the rollback and every start
  still goes through hyprial;
- the old CLI must attribute the unit before the rollback starts it;
- the service logs of the upgrade window are copied into the artifacts,
  bounded and with URLs and one-time codes redacted; the home is only read.
"""

from __future__ import annotations

import os
import sys

from ..fakes import build_apply, check, smoke, summary_of


def failing_candidate(tmp_path):
    instance, host = build_apply(tmp_path)
    original = host.daemon_payload

    def payload():
        answer = original()
        if host.installed_version == "0.2.0" and answer["daemon"].get("running"):
            answer["daemon"]["lifecycle"] = {"crashed": True}
        return answer

    host.daemon_payload = payload
    return instance, host


def test_a_failed_candidate_rolls_back_through_hyprial_commands(tmp_path) -> None:
    instance, host = failing_candidate(tmp_path)

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.installs == ["0.2.0", "0.1.0"]
    assert len(host.service_stops()) == 2
    assert len(host.service_starts()) == 2
    assert host.systemctl_calls() == []
    assert host.installed_version == "0.1.0" and host.active is True
    assert summary_of(instance)["rollback"]["ok"] is True


def test_a_candidate_cli_without_json_is_stopped_with_the_raw_systemd_stop(tmp_path) -> None:
    instance, host = failing_candidate(tmp_path)
    host.broken_versions = {"0.2.0"}

    rc = instance.finish(instance.run_apply())

    assert host.raw_stops == [["systemctl", "--user", "stop", "hyprial.service"]]
    assert check(instance, "rollback.raw-stop").status == smoke.PASS
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.installs == ["0.2.0", "0.1.0"]
    assert host.installed_version == "0.1.0" and host.active is True
    assert not any(c[:3] == ["systemctl", "--user", "start"] for c in host.calls)


def test_a_candidate_cli_without_json_is_booted_out_on_launchd(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host = failing_candidate(tmp_path)
    host.platform = "launchd"
    host.unit = "com.hyprial.hyprial.daemon.0123456789ab"
    host.broken_versions = {"0.2.0"}
    original_start = host.daemon_start

    def start(command):
        host.loaded = True
        return original_start(command)

    host.daemon_start = start

    rc = instance.finish(instance.run_apply())

    expected = ["launchctl", "bootout", f"gui/{os.getuid()}/{host.unit}"]
    assert host.raw_stops == [expected]
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.systemctl_calls() == []


def test_the_raw_stop_is_never_used_outside_the_rollback(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    original = host.daemon_stop

    def no_json(command):
        return smoke.CommandResult(command, 1, "", "Traceback\n")

    host.daemon_stop = no_json

    instance.finish(instance.run_apply())

    assert host.raw_stops == []
    assert host.installs == []
    assert check(instance, "service.stop").status == smoke.FAIL
    host.daemon_stop = original


def test_rollback_requires_the_old_cli_to_attribute_the_unit(tmp_path) -> None:
    instance, host = failing_candidate(tmp_path)
    original_install = host.install

    def install(command):
        answer = original_install(command)
        if host.installs == ["0.2.0", "0.1.0"]:
            host.unattributed_versions = {"0.1.0"}
        return answer

    host.install = install

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert instance.result == "ROLLBACK_FAILED"
    assert check(instance, "rollback.attributed").status == smoke.FAIL
    assert len(host.service_starts()) == 1


def test_rollback_start_that_finds_a_daemon_already_running_fails(tmp_path) -> None:
    instance, host = failing_candidate(tmp_path)
    original_install = host.install

    def install(command):
        answer = original_install(command)
        if host.installed_version == "0.1.0":
            host.revive_before_start = True
        return answer

    host.install = install

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert check(instance, "rollback-start").status == smoke.FAIL


def write(path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def test_service_log_tails_of_the_upgrade_window_are_copied_redacted(tmp_path) -> None:
    instance, host = failing_candidate(tmp_path)
    stdout_log = instance.state_dir / "daemon-service.stdout.log"
    stderr_log = instance.state_dir / "daemon-service.stderr.log"
    write(stdout_log, "before-the-window\n")
    write(stderr_log, "")
    original_stop = host.daemon_stop

    def stop(command):
        write(stdout_log, "stopping now\n")
        write(
            stderr_log,
            "onboarding.device verification_uri=https://example.invalid/device\n"
            "user_code=ABCD-1234\n"
            "  code: SECRET-5678\n"
            '{"verificationUri": "https://camel.invalid/d", "userCode": "JSON-9999"}\n'
            '{"userCode": "JSON-ONLY"}\n'
            "plain line\n",
        )
        return original_stop(command)

    host.daemon_stop = stop

    instance.finish(instance.run_apply())

    copied = sorted((instance.artifacts_dir / "logs").glob("service-*"))
    assert copied
    text = "".join(path.read_text() for path in copied)
    assert "stopping now" in text
    assert "plain line" in text
    assert "before-the-window" not in text
    assert "example.invalid" not in text
    assert "camel.invalid" not in text
    assert "ABCD-1234" not in text
    assert "SECRET-5678" not in text
    assert "JSON-9999" not in text
    assert "JSON-ONLY" not in text
    summary = summary_of(instance)
    assert summary["serviceLogs"]
    assert stdout_log.read_text().startswith("before-the-window")


def test_service_log_copies_are_bounded(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    stdout_log = instance.state_dir / "daemon-service.stdout.log"
    write(stdout_log, "")
    original_stop = host.daemon_stop

    def stop(command):
        write(stdout_log, ("x" * 1023 + "\n") * 2048)
        write(stdout_log, "the-last-line\n")
        return original_stop(command)

    host.daemon_stop = stop

    instance.finish(instance.run_apply())

    copied = sorted((instance.artifacts_dir / "logs").glob("service-*stdout*"))
    assert copied
    biggest = max(path.stat().st_size for path in copied)
    assert biggest <= smoke.MAX_SERVICE_LOG_BYTES + 512
    assert any("the-last-line" in path.read_text() for path in copied)


def test_no_state_dir_means_no_log_capture(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    instance.state_dir = None

    instance.finish(instance.run_apply())

    assert list((instance.artifacts_dir / "logs").glob("service-*")) == []


def test_candidate_unit_shift_after_install_fails_before_start_and_rolls_back_via_raw_stop(
    tmp_path,
) -> None:
    instance, host = build_apply(tmp_path)
    original = host.status_payload

    def shifted_for_candidate():
        payload = original()
        if host.installed_version == "0.2.0":
            payload["unit"] = "shifted.service"
        return payload

    host.status_payload = shifted_for_candidate

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert check(instance, "service.attributed-after-install").status == smoke.FAIL
    assert "shifted.service" in check(instance, "service.attributed-after-install").detail
    assert host.installs == ["0.2.0", "0.1.0"]
    assert len(host.service_starts()) == 1
    assert host.raw_stops == [["systemctl", "--user", "stop", "hyprial.service"]]
    assert check(instance, "rollback.raw-stop").status == smoke.PASS
    assert host.installed_version == "0.1.0" and host.active is True


def test_custody_unit_shift_after_candidate_start_fails_rollback_instead_of_claiming_rolled_back(
    tmp_path,
) -> None:
    instance, host = build_apply(tmp_path)
    candidate_pid = 999
    shift_post = False
    units = {
        "hyprial.service": {"running": True, "pid": 123},
        "shifted-post.service": {"running": False, "pid": None},
    }
    original_status = host.status_payload
    original_run = host.run

    def two_unit_status():
        payload = original_status()
        if shift_post and host.installed_version == "0.2.0":
            drifted = units["shifted-post.service"]
            payload.update(
                {
                    "unit": "shifted-post.service",
                    "running": drifted["running"],
                    "pid": drifted["pid"],
                    "servingPid": drifted["pid"],
                    "supervision": "supervised" if drifted["running"] else "lost",
                }
            )
        return payload

    def tracking_run(argv, **kwargs):
        nonlocal shift_post
        command = [str(item) for item in argv]
        if command[1:4] == ["version", "--json"]:
            shift_post = True
            host.active = False
            host.main_pid = None
            units["hyprial.service"] = {"running": False, "pid": None}
            units["shifted-post.service"] = {"running": True, "pid": candidate_pid}
        return original_run(argv, **kwargs)

    host.status_payload = two_unit_status
    host.run = tracking_run

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert instance.result == "ROLLBACK_FAILED"
    assert instance.rollback["ok"] is False
    assert check(instance, "post.service").status == smoke.FAIL
    assert "shifted-post.service" in check(instance, "post.service").detail
    stop_check = check(instance, "service.stop")
    assert stop_check.status == smoke.FAIL
    assert "hyprial.service" in stop_check.detail
    assert "shifted-post.service" in stop_check.detail
    assert str(candidate_pid) in stop_check.detail
    assert host.raw_stops == []
    assert host.installs == ["0.2.0"]


def test_redact_auth_material_redacts_human_code_lines_and_camelcase_fields() -> None:
    sample = (
        "device authorization required -- open this URL and enter the code:\n"
        "  https://example.invalid/device\n"
        "  code: WXYZ-9876\n"
        '{"verificationUri": "https://example.invalid/v", "userCode": "CAMEL-4321"}\n'
        '{"userCode": "CAMEL-ONLY"}\n'
        "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN\n"
        "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN userCode=LEAK-1111\n"
        "normal log line\n"
    )
    redacted = smoke.redact_auth_material(sample)
    assert "example.invalid" not in redacted
    assert "WXYZ-9876" not in redacted
    assert "CAMEL-4321" not in redacted
    assert "CAMEL-ONLY" not in redacted
    assert "LEAK-1111" not in redacted
    assert "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN\n" in redacted
    assert "normal log line\n" in redacted


def test_non_rollback_stop_refuses_unit_shift_before_stop(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    assert instance.prepare() is True
    original = host.status_payload
    reads = 0

    def shift_on_stop():
        nonlocal reads
        reads += 1
        payload = original()
        if reads >= 2:
            payload["unit"] = "shifted-stop.service"
        return payload

    host.status_payload = shift_on_stop

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert instance.result == "FAILED_STOP_TIMEOUT_RESTORE_FAILED"
    assert check(instance, "service.stop").status == smoke.FAIL
    assert "shifted-stop.service" in check(instance, "service.stop").detail
    assert check(instance, "restore.start").status == smoke.FAIL
    assert host.service_stops() == []
    assert host.raw_stops == []
    assert host.installs == []


def test_post_start_unreadable_service_status_fails_rollback_instead_of_raw_stop(
    tmp_path,
) -> None:
    instance, host = failing_candidate(tmp_path)
    original_hyprial = host.hyprial

    def fail_rollback_status(command: list[str]):
        if instance.rollback.get("attempted") and command[1:5] == [
            "daemon",
            "service",
            "status",
            "--json",
        ]:
            return smoke.CommandResult(command, 1, "not json\n", "status crashed\n")
        return original_hyprial(command)

    host.hyprial = fail_rollback_status

    rc = instance.finish(instance.run_apply())

    assert instance.candidate_started is True
    assert len(host.service_starts()) == 1
    assert host.raw_stops == []
    assert host.installs == ["0.2.0"]
    stop_check = check(instance, "service.stop")
    assert stop_check.status == smoke.FAIL
    assert "after candidate start was attempted" in stop_check.detail
    assert "raw_stop" in stop_check.detail
    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert instance.result == "ROLLBACK_FAILED"
    assert instance.rollback["ok"] is False


def test_post_start_unreadable_daemon_stop_fails_rollback_instead_of_raw_stop(
    tmp_path,
) -> None:
    instance, host = failing_candidate(tmp_path)
    original_daemon_stop = host.daemon_stop

    def fail_rollback_stop(command: list[str]):
        if instance.rollback.get("attempted"):
            return smoke.CommandResult(
                command,
                124,
                "partial non-json\n",
                "timed out\n",
                timed_out=True,
            )
        return original_daemon_stop(command)

    host.daemon_stop = fail_rollback_stop

    rc = instance.finish(instance.run_apply())

    assert instance.candidate_started is True
    assert len(host.service_starts()) == 1
    assert host.raw_stops == []
    assert host.installs == ["0.2.0"]
    stop_check = check(instance, "service.stop")
    assert stop_check.status == smoke.FAIL
    assert "after candidate start was attempted" in stop_check.detail
    assert "raw_stop" in stop_check.detail
    assert "timed_out=True" in stop_check.detail
    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert instance.result == "ROLLBACK_FAILED"
    assert instance.rollback["ok"] is False
