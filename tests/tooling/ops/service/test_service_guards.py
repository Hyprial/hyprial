"""Which service control the smoke uses, and when it refuses before any stop.

Decisions pinned here (plan section 7, owner answers 2026-11-20):
- the service is observed through ``hyprial daemon service status --json`` of
  the live CLI, never ``systemctl show``, on both platforms (Q3);
- a home without an installed, attributable unit, or with a daemon outside the
  manager's custody, is refused with exit 2 before anything stops (Q6);
- ``SERVICE_PLATFORM_UNSUPPORTED`` is BLOCKED, exit 4;
- ``HYPRIAL_DAEMON_AUTOSTART=0`` in the caller's environment is refused (Q6);
- ``--service`` is derived; given and different from the status unit it is
  refused (Q3);
- a baseline CLI whose status lacks ``supervision``/``attributed`` keeps the
  legacy systemctl path on Linux and is refused on macOS (Q5);
- the summary schema is ``hyprial-dev-upgrade-smoke/v2`` with the hyprial
  status object as ``baseline.service``.
"""

from __future__ import annotations

import sys

import pytest

from ..fakes import FakeHost, build_apply, check, smoke, summary_of


def refused_before_stop(instance, host, rc: int, code: int) -> None:
    assert rc == code
    assert host.service_stops() == []
    assert host.systemctl_calls() == []
    assert host.installs == []


@pytest.mark.parametrize(
    ("field", "value", "check_name"),
    [
        ("installed", False, "service.installed"),
        ("attributed", False, "service.attributed"),
    ],
)
def test_apply_refuses_a_home_without_an_attributable_unit(
    tmp_path, field, value, check_name
) -> None:
    instance, host = build_apply(tmp_path)
    setattr(host, field, value)

    rc = instance.run()

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert check(instance, check_name).status == smoke.FAIL
    assert summary_of(instance)["result"] == "ABORTED"


def test_apply_refuses_a_daemon_outside_the_managers_custody(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.detached_pid = 999

    rc = instance.run()

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert check(instance, "service.supervised").status == smoke.FAIL


def test_the_pre_stop_guard_refuses_custody_lost_after_preflight(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.detached_pid = 999

    rc = instance.run_apply()

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert check(instance, "apply.pre-stop").status == smoke.FAIL
    assert instance.result == "ABORTED"


def test_a_stopped_but_attributable_service_is_not_refused(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.active = False
    host.main_pid = None

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert host.installs == ["0.2.0"]
    assert check(instance, "service.stop").status == smoke.PASS
    assert "already stopped" in check(instance, "service.stop").detail


def test_ownership_refused_is_a_guard_refusal_naming_the_reason(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.status_error = (
        "SERVICE_OWNERSHIP_REFUSED",
        {"ownershipReason": "hyprial-home-outside-operating-home"},
    )

    rc = instance.run()

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert "hyprial-home-outside-operating-home" in check(instance, "baseline.service").detail


def test_platform_unsupported_is_blocked(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.status_error = ("SERVICE_PLATFORM_UNSUPPORTED", {"platform": "win32"})

    rc = instance.run()

    refused_before_stop(instance, host, rc, smoke.EXIT_BLOCKED)
    assert check(instance, "baseline.service").status == smoke.BLOCKED
    assert summary_of(instance)["result"] == "BLOCKED"


def test_preflight_reports_the_same_refusal(tmp_path) -> None:
    instance, host = build_apply(tmp_path, mode="preflight")
    host.attributed = False

    rc = instance.run()

    assert rc == smoke.EXIT_USAGE
    assert check(instance, "service.attributed").status == smoke.FAIL
    assert host.service_stops() == []


def test_autostart_opt_out_in_the_callers_environment_is_refused(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "0")
    instance, host = build_apply(tmp_path)

    rc = instance.run()

    assert rc == smoke.EXIT_USAGE
    assert host.calls == []
    assert "HYPRIAL_DAEMON_AUTOSTART" in check(instance, "apply.guard").detail


def test_a_service_flag_that_differs_from_the_derived_unit_is_refused(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--service", "hyprial-0123456789ab.service"])

    rc = instance.run()

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert check(instance, "service.unit").status == smoke.FAIL


def test_a_matching_service_flag_is_accepted_and_the_unit_is_derived(tmp_path) -> None:
    instance, host = build_apply(
        tmp_path, mode="preflight", extra=["--service", "hyprial.service"]
    )

    instance.run()

    assert check(instance, "service.unit").status == smoke.PASS
    summary = summary_of(instance)
    assert summary["service"] == "hyprial.service"
    assert summary["serviceControl"] == "hyprial"


def test_the_service_flag_defaults_to_derived() -> None:
    args = smoke.build_parser().parse_args(["apply", "--new-wheel", "n", "--wheelhouse", "w"])
    assert args.service is None


def test_summary_schema_v2_carries_the_hyprial_status_object(tmp_path) -> None:
    instance, host = build_apply(tmp_path, mode="preflight")

    instance.run()

    summary = summary_of(instance)
    assert summary["schema"] == "hyprial-dev-upgrade-smoke/v2"
    service = summary["baseline"]["service"]
    assert service["platform"] == "systemd"
    assert service["unit"] == "hyprial.service"
    assert service["supervision"] == "supervised"
    assert service["attributed"] is True
    assert host.systemctl_calls() == []
    assert all(item["name"] != "toolchain.systemctl" for item in summary["checks"])


def test_launchd_status_is_handled_by_the_same_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host = build_apply(tmp_path)
    host.platform = "launchd"
    host.unit = "com.hyprial.hyprial.daemon.0123456789ab"

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert host.systemctl_calls() == []
    assert len(host.service_stops()) == 1
    assert len(host.service_starts()) == 1


LEGACY_SHAPES = {
    "no-command-json": (True, False),
    "no-command-plain": ("plain", False),
    "no-fields": (False, True),
}


@pytest.mark.parametrize("shape", sorted(LEGACY_SHAPES))
def test_a_baseline_cli_without_supervision_fields_uses_legacy_systemctl_on_linux(
    tmp_path, shape
) -> None:
    instance, host = build_apply(tmp_path)
    host.legacy, host.legacy_fields_only = LEGACY_SHAPES[shape]

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert instance.service_control_name() == "legacy-systemd"
    assert host.service_stops() == []
    assert ["systemctl", "--user", "stop", "hyprial.service"] in host.calls
    assert host.installs == ["0.2.0"]


@pytest.mark.parametrize("shape", sorted(LEGACY_SHAPES))
def test_a_baseline_cli_without_supervision_fields_is_refused_on_macos(
    tmp_path, monkeypatch, shape
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host = build_apply(tmp_path)
    host.legacy, host.legacy_fields_only = LEGACY_SHAPES[shape]

    rc = instance.run()

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert check(instance, "service.control").status == smoke.FAIL


def test_legacy_preflight_keeps_the_unit_environment_out_of_the_logs(tmp_path) -> None:
    instance, host = build_apply(tmp_path, mode="preflight")
    host.legacy = True

    instance.run()

    assert check(instance, "toolchain.systemctl").status == smoke.PASS
    logs = "".join(
        path.read_text() for path in (instance.artifacts_dir / "logs").iterdir()
    )
    assert "do-not-log" not in logs


def test_another_usage_error_is_not_mistaken_for_a_legacy_cli(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.status_error = ("INVALID_ARGUMENT", None)

    rc = instance.run()

    assert rc == smoke.EXIT_FAILED
    assert check(instance, "baseline.service").status == smoke.FAIL
    assert host.systemctl_calls() == []


def test_a_cli_that_answers_without_json_is_a_failed_baseline_not_legacy(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    host.broken_versions = {"0.1.0"}

    rc = instance.run()

    assert rc == smoke.EXIT_FAILED
    assert check(instance, "baseline.service").status == smoke.FAIL
    assert host.systemctl_calls() == []
    assert host.service_stops() == []


def test_every_live_command_gets_the_hardened_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "1")
    monkeypatch.setenv("HYPRIAL_SERVICE_AUTOINSTALL", "0")
    monkeypatch.setenv("HARNESS_SOCKET_PATH", "/elsewhere.sock")
    monkeypatch.setenv("HYPRIAL_OWNER", "someone")
    instance, host = build_apply(tmp_path)

    assert instance.run_apply() == smoke.EXIT_OK

    for prefix in (["daemon", "start", "--json"], ["daemon", "stop", "--json"],
                   ["daemon", "service", "status", "--json"]):
        env = host.env_of(prefix)
        assert env is not None, prefix
        for key in ("HYPRIAL_DAEMON_AUTOSTART", "HYPRIAL_SERVICE_AUTOINSTALL",
                    "HARNESS_SOCKET_PATH", "HYPRIAL_OWNER"):
            assert key not in env, (prefix, key)
        assert env["PATH"].split(":")[0] == str(instance.bin_dir)
        assert env["HYPRIAL_HOME"] == str(instance.home)
        assert env["HARNESS_STATE_DIR"] == str(instance.state_dir)


def test_a_fake_host_is_reachable_only_through_the_runner(tmp_path) -> None:
    host = FakeHost(tmp_path, tmp_path)
    assert host.status_payload()["supervision"] == "supervised"
    host.active = False
    host.main_pid = None
    assert host.status_payload()["supervision"] == "lost"


@pytest.mark.parametrize(
    ("field", "shifted"),
    [
        ("unit", "other.service"),
        ("platform", "launchd"),
    ],
)
def test_the_pre_stop_guard_refuses_identity_shift_after_baseline(
    tmp_path, field, shifted
) -> None:
    instance, host = build_apply(tmp_path)
    original = host.status_payload
    reads = 0

    def shifting_status():
        nonlocal reads
        reads += 1
        payload = original()
        if reads > 1:
            payload[field] = shifted
        return payload

    host.status_payload = shifting_status

    assert instance.prepare() is True
    rc = instance.finish(instance.run_apply())

    refused_before_stop(instance, host, rc, smoke.EXIT_USAGE)
    assert check(instance, "apply.pre-stop").status == smoke.FAIL
    assert str(shifted) in check(instance, "apply.pre-stop").detail
    assert summary_of(instance)["result"] == "ABORTED"
