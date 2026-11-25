"""``hyprial login`` and ``hyprial init`` against a real, controlled bootstrap daemon.

Decision: daemon-first-z5-cli-process-20261119.  The daemon is the one the tests
of Z-2 and Z-5 PR 1 use (``daemon_lifecycle/child_entry.py``): the shipped
lifecycle with exactly three controlled ports (identity worker, issuer profile,
device provisioner).  Every test first proves that this daemon's pid answers
``ping`` on the home's socket, so the CLI under test can never start a daemon of
its own; the real issuer, a real browser (``--no-open``) and a real Tailcat sidecar
are not exercised, and nothing here claims they are.  The CLI processes are the
real ``python -m hyprial.cli``; a Ctrl-C is a real SIGINT to the one process the
test started.  Everything is reaped by the fixture, also on failure.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys

from tests.integration.daemon_lifecycle.test_same_pid_lifecycle import OWNER

from .conftest import pytestmark  # noqa: F401

PUBLISHED_KEYS = {"ok", "identity", "device", "daemon", "migration", "pending", "verificationUri", "userCode"}


def _bootstrap(lifecycle, **environment):
    child = lifecycle.spawn(**environment)
    first = lifecycle.wait_phase({"bootstrap"}, child)
    assert first["pid"] == child.pid and first["owner"] is None
    return child, first


def test_login_is_one_daemon_operation_and_the_same_process_serves(lifecycle):
    child, first = _bootstrap(lifecycle)

    code, payload = lifecycle.cli_json("login", "--json", "--no-open")

    assert code == 0 and payload["ok"] is True, payload
    assert set(payload) <= PUBLISHED_KEYS | {"nextSteps"} and PUBLISHED_KEYS <= set(payload)
    assert payload["identity"]["owner"] == OWNER and payload["identity"]["committed"] is True
    assert payload["device"]["ready"] is True and payload["device"]["deviceId"] == "z2-node-device"
    assert payload["daemon"]["pid"] == child.pid and payload["daemon"]["started"] is False
    assert payload["daemon"]["running"] is True
    assert payload["userCode"] == "Z2-CODE" and payload["verificationUri"].endswith("/device")
    ping = lifecycle.serving(child)
    assert ping["pid"] == child.pid and ping["epoch"] != first["epoch"] and ping["owner"] == OWNER
    status = lifecycle.call("onboarding.status")
    assert (status["phase"], status["deviceReady"], status["initialized"]) == ("serving", True, True)
    assert child.poll() is None, lifecycle.child_log(child)


def test_a_device_that_cannot_be_registered_is_exit_zero_and_the_daemon_is_left_alone(lifecycle):
    child, first = _bootstrap(lifecycle, Z5_BOOTSTRAP_DEVICE="fail", Z3_SERVING_DEVICE="fail")

    code, payload = lifecycle.cli_json("login", "--json", "--no-open")

    assert code == 0 and payload["ok"] is True, payload
    device = payload["device"]
    assert device["ready"] is False and device["retry"] == "serving-start"
    assert device["error"]["code"] == "DEVICE_ENSURE_FAILED"
    assert "once at each daemon start" in device["nextStep"]
    assert payload["identity"]["committed"] is True
    ping = lifecycle.serving(child)
    assert ping["pid"] == child.pid and ping["owner"] == OWNER
    assert child.poll() is None, lifecycle.child_log(child)


def test_init_reports_bootstrap_and_the_next_step_then_login_succeeds(lifecycle):
    child, first = _bootstrap(lifecycle)

    code, initialized = lifecycle.cli_json("init", "--json", "--no-service")

    assert code == 0 and initialized["ok"] is True, initialized
    assert initialized["phase"] == "bootstrap" and initialized["owner"] is None
    assert initialized["pid"] == child.pid and initialized["nextSteps"] == ["hyprial login"]
    for key in ("identity", "device", "verificationUri", "userCode"):
        assert key not in initialized

    code, payload = lifecycle.cli_json("login", "--json", "--no-open")
    assert code == 0 and payload["identity"]["owner"] == OWNER, payload
    assert payload["daemon"]["pid"] == child.pid
    assert lifecycle.serving(child)["pid"] == child.pid


def test_a_serving_daemon_on_a_home_with_no_owner_is_refused_by_the_daemon(lifecycle):
    child, _first = _bootstrap(lifecycle)
    lifecycle.login_and_initialize("conflict")
    lifecycle.serving(child)
    (lifecycle.home / "settings.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

    code, refused = lifecycle.cli_json("login", "--json", "--no-open")

    assert code == 1 and refused["ok"] is False and refused["code"] == "IDENTITY_CONFLICT", refused
    assert lifecycle.ping()["pid"] == child.pid


def test_ctrl_c_sends_one_cancel_and_leaves_the_daemon_in_bootstrap_for_the_next_login(lifecycle):
    child, first = _bootstrap(lifecycle, Z5_BOOTSTRAP_HOLD="1")
    process = subprocess.Popen(
        [sys.executable, "-m", "hyprial.cli", "login", "--json", "--no-open"],
        env=lifecycle.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(lifecycle.root), start_new_session=True,
    )
    try:
        event = json.loads(process.stderr.readline())
        assert event["event"] == "device" and event["userCode"] == "Z2-CODE"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert process.returncode == 1 and payload["code"] == "INTERRUPTED", (stdout, stderr)
    ping = lifecycle.wait_phase({"bootstrap"}, child)
    assert ping["pid"] == child.pid and ping["epoch"] == first["epoch"]
    assert lifecycle.call("onboarding.status")["authenticated"] is False
    begun = lifecycle.call("auth.login.begin", {"intent": "login", "operationKey": "z5-after-ctrl-c-0001"})
    assert begun["state"] in {"pending", "authenticating"}
