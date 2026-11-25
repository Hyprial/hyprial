"""One process, one PID: bootstrap, login, serving, logout, bootstrap, login, serving, stop.

Decision: daemon-first-z2-same-pid-20261116.  This is the measurement design
section 5.2 left UNKNOWN (a second promotion in the same interpreter on the
shipped native engine) taken through the unmodified ``hyprial daemon run``
path.  The identity port, issuer profile and device provisioner are the only
controlled parts (``child_entry.py``); real OIDC and a real Tailcat sidecar are
not exercised and nothing here claims they are.
"""

from __future__ import annotations

import json
import signal
import time

import pytest

from hyprial.daemon import DaemonStateOwnershipFence
from hyprial.kernel import ipc_errors

from .conftest import pytestmark  # noqa: F401

OWNER = "z2-owner@example.test"


def _opens(lifecycle) -> int:
    return len([item for item in lifecycle.records() if item["event"] == "open"])


def _wait_initialized(lifecycle, child) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = lifecycle.call("onboarding.status")
        if status.get("initialized") is True:
            return status
        assert child.poll() is None, lifecycle.child_log(child)
        time.sleep(0.1)
    raise AssertionError(f"initialize never completed: {status}")


def _logout(lifecycle, key: str) -> None:
    try:
        lifecycle.call("auth.logout", {"confirm": True, "operationKey": key})
    except ipc_errors.TransientDaemonError:
        pass


def _serve_once(lifecycle, child, tag: str) -> dict:
    lifecycle.login_and_initialize(tag)
    ping = lifecycle.serving(child)
    assert ping["pid"] == child.pid and ping["owner"] == OWNER
    status = _wait_initialized(lifecycle, child)
    assert status["authenticated"] is True
    return ping


def test_the_same_process_serves_logs_out_returns_to_bootstrap_and_serves_again(lifecycle):
    child = lifecycle.spawn()
    first = lifecycle.wait_phase({"bootstrap"}, child)
    assert first["pid"] == child.pid and first["owner"] is None

    serving_one = _serve_once(lifecycle, child, "one00001")
    assert serving_one["epoch"] != first["epoch"]

    _logout(lifecycle, "z2-logout-key-00001")
    second = lifecycle.wait_phase({"bootstrap"}, child, budget=60)
    assert second["pid"] == child.pid and second["owner"] is None
    assert second["epoch"] not in {first["epoch"], serving_one["epoch"]}
    assert child.poll() is None, lifecycle.child_log(child)
    assert json.loads((lifecycle.home / "settings.json").read_text()).get("owner") is None
    assert lifecycle.call("onboarding.status")["authenticated"] is False

    serving_two = _serve_once(lifecycle, child, "two00002")
    assert serving_two["epoch"] not in {first["epoch"], serving_one["epoch"], second["epoch"]}
    assert {first["pid"], serving_one["pid"], second["pid"], serving_two["pid"]} == {child.pid}
    assert _opens(lifecycle) == 2

    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)
    with DaemonStateOwnershipFence.acquire(lifecycle.state, timeout=0):
        pass


def test_a_logout_before_initialize_keeps_the_process_and_serves_the_next_login(lifecycle):
    child = lifecycle.spawn()
    first = lifecycle.wait_phase({"bootstrap"}, child)
    begun = lifecycle.call("auth.login.begin", {"intent": "login", "operationKey": "z6b-pre-login-0001"})
    got = lifecycle.call("auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "authenticated", got

    _logout(lifecycle, "z6b-pre-logout-0001")
    deadline = time.monotonic() + 60
    second = None
    while time.monotonic() < deadline:
        assert child.poll() is None, lifecycle.child_log(child)
        ping = lifecycle.ping()
        if ping is not None and ping.get("phase") == "bootstrap" and ping["epoch"] != first["epoch"]:
            second = ping
            break
        time.sleep(0.1)
    assert second is not None, "no second bootstrap epoch"
    assert second["pid"] == child.pid and second["owner"] is None
    assert lifecycle.call("onboarding.status")["authenticated"] is False

    _serve_once(lifecycle, child, "pre00003")
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)


def test_the_cli_logout_is_one_daemon_request_and_the_process_returns_to_bootstrap(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    serving = _serve_once(lifecycle, child, "cli00001")

    code, payload = lifecycle.cli_json("logout", "--yes", "--json")
    assert code == 0 and payload["ok"] is True, payload
    assert payload["status"] == "logged-out"
    assert "settings.owner" in payload["removed"]
    assert payload["daemon"] == {"stopRequested": True}
    back = lifecycle.wait_phase({"bootstrap"}, child, budget=60)
    assert back["pid"] == child.pid and back["epoch"] != serving["epoch"]
    assert json.loads((lifecycle.home / "settings.json").read_text()).get("owner") is None

    code, again = lifecycle.cli_json("logout", "--yes", "--json")
    assert code == 0 and again["ok"] is True, again
    assert again["status"] == "not-logged-in" and again["removed"] == []
    assert again["daemon"] == {"stopRequested": False}
    assert lifecycle.wait_phase({"bootstrap"}, child, budget=60)["pid"] == child.pid
    assert child.poll() is None, lifecycle.child_log(child)

    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)


@pytest.mark.parametrize("stopper", ["daemon-stop", "sigterm", "sigint"])
def test_a_shutdown_after_a_promotion_exits_and_never_reopens_bootstrap(lifecycle, stopper):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    _serve_once(lifecycle, child, "shut0001")
    assert _opens(lifecycle) == 1

    if stopper == "daemon-stop":
        code, payload = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and payload["ok"] is True, payload
    else:
        child.send_signal(signal.SIGTERM if stopper == "sigterm" else signal.SIGINT)

    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    assert _opens(lifecycle) == 1, "bootstrap was reopened after a shutdown"
    assert lifecycle.ping() is None
    lifecycle.assert_nothing_left(child.pid)
    assert json.loads((lifecycle.home / "settings.json").read_text()).get("owner") == OWNER


def test_sigterm_during_the_logout_to_bootstrap_transition_ends_in_exit(lifecycle):
    child = lifecycle.spawn(Z2_SIGTERM_BEFORE_OPEN="2")
    lifecycle.wait_phase({"bootstrap"}, child)
    _serve_once(lifecycle, child, "trans001")

    _logout(lifecycle, "z2-logout-key-00002")

    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    assert _opens(lifecycle) == 2
    assert lifecycle.ping() is None
    lifecycle.assert_nothing_left(child.pid)
    assert json.loads((lifecycle.home / "settings.json").read_text()).get("owner") is None


@pytest.mark.parametrize("failure", ["encode", "registry"])
def test_a_failure_after_an_accepted_initialize_exits_and_the_next_start_is_normal(lifecycle, failure):
    child = lifecycle.spawn(Z2_FAIL_AFTER_ACCEPT=failure)
    lifecycle.wait_phase({"bootstrap"}, child)
    begun = lifecycle.call("auth.login.begin", {"intent": "login", "operationKey": "z2-login-key-fail01"})
    lifecycle.call("auth.operation.get", {"operationId": begun["operationId"]})
    with pytest.raises(Exception) as refused:
        lifecycle.call("onboarding.initialize", {"operationKey": "z2-initialize-fail01"})
    assert getattr(refused.value, "code", None) == ipc_errors.DAEMON_ERROR

    assert child.wait(timeout=90) == 1, lifecycle.child_log(child)
    report = json.loads(lifecycle.child_log(child).strip().splitlines()[-1])
    assert report["ok"] is False and report["code"] == "BOOTSTRAP_INITIALIZE_UNACKNOWLEDGED"
    lifecycle.assert_nothing_left(child.pid)
    with DaemonStateOwnershipFence.acquire(lifecycle.state, timeout=0):
        pass

    next_start = lifecycle.spawn()
    ping = lifecycle.wait_phase({"serving", "reconciled"}, next_start)
    assert ping["owner"] == OWNER and ping["pid"] == next_start.pid
    next_start.send_signal(signal.SIGTERM)
    assert next_start.wait(timeout=90) == 0, lifecycle.child_log(next_start)
    lifecycle.assert_nothing_left(next_start.pid)


@pytest.mark.parametrize("stopper", ["daemon-stop", "sigterm"])
def test_a_lingering_non_daemon_thread_does_not_keep_a_finally_stopped_daemon_alive(lifecycle, stopper):
    child = lifecycle.spawn(Z2_LINGER_THREAD="1")
    lifecycle.wait_phase({"bootstrap"}, child)
    _serve_once(lifecycle, child, "linger01")

    started = time.monotonic()
    if stopper == "daemon-stop":
        code, payload = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and payload["ok"] is True, payload
    else:
        child.send_signal(signal.SIGTERM)

    assert child.wait(timeout=60) == 0, lifecycle.child_log(child)
    assert time.monotonic() - started < 60
    lifecycle.assert_nothing_left(child.pid)


EXIT_BUDGET_SECONDS = 30


def _json_results(log: str) -> list[dict]:
    return [json.loads(line) for line in log.splitlines() if line.startswith("{") and '"ok"' in line]


@pytest.mark.parametrize("stopper", ["daemon-stop", "sigterm"])
@pytest.mark.parametrize("path", ["first-bootstrap", "after-logout"])
def test_a_lingering_non_daemon_thread_does_not_keep_a_daemon_stopped_in_bootstrap_alive(
    lifecycle, path, stopper
):
    child = lifecycle.spawn(Z2_LINGER_THREAD="1")
    lifecycle.wait_phase({"bootstrap"}, child)
    if path == "after-logout":
        _serve_once(lifecycle, child, "lingerb1")
        _logout(lifecycle, "z2-logout-key-lingb1")
        deadline = time.monotonic() + 60
        while _opens(lifecycle) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        lifecycle.wait_phase({"bootstrap"}, child)
        assert child.poll() is None

    started = time.monotonic()
    if stopper == "daemon-stop":
        code, payload = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and payload["ok"] is True, payload
    else:
        child.send_signal(signal.SIGTERM)

    assert child.wait(timeout=EXIT_BUDGET_SECONDS) == 0, lifecycle.child_log(child)
    assert time.monotonic() - started < EXIT_BUDGET_SECONDS
    results = _json_results(lifecycle.child_log(child))
    assert [item["ok"] for item in results] == [True], results
    assert results[0]["stopped"] is True
    lifecycle.assert_nothing_left(child.pid)
