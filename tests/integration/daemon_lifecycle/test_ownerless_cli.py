"""Z-2 on a real ownerless home: start, status, doctor, stop, kill -9 and signals.

Decision: daemon-first-z2-ownerless-cli-20261116.  The daemon here is the
shipped one (native engine, isolated short-path home, no owner override).
``daemon start`` needs no identity port because no login happens; the
``daemon run`` cases use the controlled identity entry.  The tests say what
they exercised: real OIDC and the real Tailcat sidecar are not involved.
"""

from __future__ import annotations

import json
import os
import signal

import pytest

from hyprial.daemon import DaemonStateOwnershipFence

from .conftest import pytestmark  # noqa: F401


def _stop_ok(lifecycle, pid):
    code, payload = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 0 and payload["ok"] is True, payload
    lifecycle.assert_nothing_left(pid)


def test_ownerless_start_status_doctor_stop(lifecycle):
    code, started = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0 and started["ok"] is True, started
    assert started["phase"] == "bootstrap" and started["running"] is True
    assert started["alreadyRunning"] is False and started["owner"] is None
    pid = started["pid"]

    marker = json.loads((lifecycle.state / "daemon.json").read_text(encoding="utf-8"))
    assert marker["phase"] == "bootstrap" and marker["pid"] == pid
    assert marker["launchId"] and marker["birthIdentity"]
    assert "epoch" not in marker and "nodeId" not in marker

    code, status = lifecycle.cli_json("daemon", "status", "--json")
    assert code == 0 and status["ok"] is True, status
    assert status["daemon"]["running"] is True
    assert status["daemon"]["phase"] == "bootstrap"
    assert status["daemon"]["pid"] == pid and status["daemon"]["owner"] is None

    code, again = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0 and again["alreadyRunning"] is True and again["pid"] == pid

    code, refused = lifecycle.cli_json("agent", "list", "--json")
    assert code == 1 and refused["code"] == "ACCOUNT_REQUIRED", refused
    assert refused["error"] == "this daemon has no account yet; sign in first"
    assert refused["data"]["nextSteps"] == ["hyprial login"]

    for argv in (
        ("agent", "grants", "--json"),
        ("message", "outbox", "prune", "--dry-run", "--json"),
        ("message", "ack", "message-0001", "--from", "alice", "--json"),
    ):
        code, refused = lifecycle.cli_json(*argv)
        assert code == 1 and refused["code"] == "ACCOUNT_REQUIRED", (argv, refused)

    result = lifecycle.cli("doctor", "--json")
    assert "Traceback" not in result.stderr and "Traceback" not in result.stdout
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, result.stdout
    report = json.loads(lines[0])
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["daemon"]["status"] == "warn" and "bootstrap" in checks["daemon"]["detail"]
    assert checks["agent-adapter-name-collision"]["status"] == "warn"
    assert checks["agent-adapter-name-collision"]["detail"].startswith("NOT-EVALUABLE")
    failing = {name for name, item in checks.items() if item["status"] == "fail"}
    assert failing <= {"daemon-service"}, failing
    summary = report["summary"]
    expected = 1 if summary["fail"] else 10 if summary["unknown"] else 0
    assert report["exitCode"] == expected == result.returncode

    _stop_ok(lifecycle, pid)
    with DaemonStateOwnershipFence.acquire(lifecycle.state, timeout=0):
        pass


def test_kill9_in_bootstrap_then_daemon_start_reaps_the_dead_markers(lifecycle):
    code, started = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0, started
    first = started["pid"]
    os.kill(first, signal.SIGKILL)
    lifecycle.wait_dead(first)
    assert {"daemon.json", "bootstrap.json"} <= set(lifecycle.state_files())

    code, restarted = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0 and restarted["ok"] is True, restarted
    assert restarted["phase"] == "bootstrap" and restarted["pid"] != first
    record = json.loads((lifecycle.state / "bootstrap.json").read_text(encoding="utf-8"))
    assert record["pid"] == restarted["pid"]
    _stop_ok(lifecycle, restarted["pid"])


def test_a_bootstrap_marker_whose_pid_was_reused_is_reaped_by_daemon_start(lifecycle):
    code, started = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0, started
    first = started["pid"]
    os.kill(first, signal.SIGKILL)
    lifecycle.wait_dead(first)
    for name in ("daemon.json", "bootstrap.json"):
        path = lifecycle.state / name
        record = json.loads(path.read_text(encoding="utf-8"))
        record["pid"] = os.getpid()
        record["birthIdentity"] = "proc:not-this-birth"
        path.write_text(json.dumps(record), encoding="utf-8")

    code, restarted = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0 and restarted["ok"] is True, restarted
    assert restarted["phase"] == "bootstrap" and restarted["pid"] not in {first, os.getpid()}
    _stop_ok(lifecycle, restarted["pid"])


@pytest.mark.parametrize("phase", ["awaiting-auth", "authenticating"])
def test_kill9_through_daemon_run_is_admitted_again(lifecycle, phase):
    first = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, first)
    if phase == "authenticating":
        begun = lifecycle.call(
            "auth.login.begin", {"intent": "login", "operationKey": "z2-login-key-kill9"}
        )
        assert begun["state"] in {"authenticating", "pending"}, begun
    os.kill(first.pid, signal.SIGKILL)
    first.wait(timeout=30)
    assert (lifecycle.state / "daemon.json").exists()

    second = lifecycle.spawn()
    ping = lifecycle.wait_phase({"bootstrap"}, second)
    assert ping["pid"] == second.pid != first.pid
    second.send_signal(signal.SIGTERM)
    assert second.wait(timeout=90) == 0, lifecycle.child_log(second)
    lifecycle.assert_nothing_left(second.pid)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_in_bootstrap_exits_zero_and_cleans_up(lifecycle, signum):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    child.send_signal(signum)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)
    with DaemonStateOwnershipFence.acquire(lifecycle.state, timeout=0):
        pass


def test_the_cli_passes_the_published_process_exit_policy(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0
    launched = [item for item in lifecycle.records() if item["event"] == "lifecycle"]
    assert len(launched) == 1
    assert launched[0]["owns_process_exit"] is None
    assert launched[0]["exit_unless_logout"] is True
    assert launched[0]["has_bind_epoch"] is True


def test_a_daemon_that_was_never_started_stops_with_a_clean_refusal(lifecycle):
    code, payload = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 1 and payload["code"] == "DAEMON_UNAVAILABLE"
