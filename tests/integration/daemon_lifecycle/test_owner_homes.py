"""Homes that already have an owner keep the normal daemon path.

Decision: daemon-first-z2-owner-homes-20261116; updated for L3 single-account
home binding (retired cross-account owner_migration).  The ownerless lifecycle
must not capture a home whose owner the daemon itself resolves: ``HYPRIAL_OWNER``
first, then ``settings.json`` (``identity_record``).  Both homes are started,
inspected and stopped through the real CLI.

After same-account logout, re-login, and restart, the home holds its existing
agents and one user agent for that owner.  The daemon ensures it on its own
thread after each start, so the test waits until ``ps`` reports
``userAgent.state == "present"`` (bounded) before it reads any listing: a
default ``agent list`` read earlier would hide nothing and prove nothing.  Only
then does it check that the default listing carries no user agent row and that
the two runs' default listings agree.
"""

from __future__ import annotations

import json
import signal
import time

import pytest

from hyprial.daemon import DaemonStateOwnershipFence
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME
from hyprial.kernel import ipc_errors

from .conftest import pytestmark  # noqa: F401


@pytest.mark.parametrize("source", ["environment", "settings"])
def test_an_owned_home_starts_serves_lists_agents_and_stops(lifecycle, source):
    owner = f"z2-{source}-owner@example.test"
    if source == "environment":
        lifecycle.env["HYPRIAL_OWNER"] = owner
    else:
        (lifecycle.home / "settings.json").write_text(json.dumps({"version": 1, "owner": owner}))

    code, started = lifecycle.cli_json("daemon", "start", "--json")
    assert code == 0 and started["ok"] is True, started
    assert started["phase"] != "bootstrap" and started["owner"] == owner
    pid = started["pid"]
    marker = json.loads((lifecycle.state / "daemon.json").read_text(encoding="utf-8"))
    assert marker["pid"] == pid and "phase" not in marker and marker["epoch"]

    code, status = lifecycle.cli_json("daemon", "status", "--json")
    assert code == 0 and status["daemon"]["running"] is True and status["daemon"]["pid"] == pid

    code, agents = lifecycle.cli_json("agent", "list", "--json")
    assert code == 0 and agents["ok"] is True, agents

    code, stopped = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 0 and stopped["ok"] is True, stopped
    lifecycle.assert_nothing_left(pid)


ISSUER = "https://issuer.example.test"
LOGIN_OWNER = "z2-owner@example.test"


def _write_login_owned_settings(lifecycle) -> None:
    (lifecycle.home / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "owner": LOGIN_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": ISSUER,
            }
        ),
        encoding="utf-8",
    )


def _logout(lifecycle, key: str) -> None:
    try:
        lifecycle.call("auth.logout", {"confirm": True, "operationKey": key})
    except ipc_errors.TransientDaemonError:
        pass


def _opens(lifecycle) -> int:
    return len([item for item in lifecycle.records() if item["event"] == "open"])


def test_an_owned_start_stays_alive_across_logout_and_serves_the_next_login(lifecycle):
    _write_login_owned_settings(lifecycle)
    child = lifecycle.spawn()
    serving = lifecycle.serving(child)
    assert serving["pid"] == child.pid and serving["owner"] == LOGIN_OWNER

    _logout(lifecycle, "z6pre-logout-key-0001")
    bootstrap = lifecycle.wait_phase({"bootstrap"}, child, budget=60)
    assert bootstrap["pid"] == child.pid and bootstrap["owner"] is None
    assert bootstrap["epoch"] != serving["epoch"]
    assert child.poll() is None, lifecycle.child_log(child)
    assert json.loads((lifecycle.home / "settings.json").read_text()).get("owner") is None
    assert _opens(lifecycle) == 1

    lifecycle.login_and_initialize("z6pre0001")
    again = lifecycle.serving(child)
    assert again["pid"] == child.pid and again["owner"] == LOGIN_OWNER
    assert again["epoch"] not in {serving["epoch"], bootstrap["epoch"]}

    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)
    with DaemonStateOwnershipFence.acquire(lifecycle.state, timeout=0):
        pass


@pytest.mark.parametrize("stopper", ["daemon-stop", "sigterm"])
def test_an_owned_start_that_is_stopped_exits_and_never_opens_bootstrap(lifecycle, stopper):
    _write_login_owned_settings(lifecycle)
    child = lifecycle.spawn()
    lifecycle.serving(child)

    if stopper == "daemon-stop":
        code, stopped = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and stopped["ok"] is True, stopped
    else:
        child.send_signal(signal.SIGTERM)

    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    assert _opens(lifecycle) == 0
    assert lifecycle.ping() is None
    lifecycle.assert_nothing_left(child.pid)


def _agent_view(listing):
    return sorted((item["agentId"], item["owner"]) for item in listing["agents"])


def _wait_user_agent_present(lifecycle, budget=30.0):
    deadline = time.monotonic() + budget
    while True:
        block = lifecycle.call("ps").get("userAgent") or {}
        if block.get("state") == "present" or time.monotonic() >= deadline:
            assert block.get("state") == "present", block
            return block
        time.sleep(0.2)


def _default_listing(lifecycle):
    code, listing = lifecycle.cli_json("agent", "list", "--json")
    assert code == 0 and listing["ok"] is True, listing
    assert not [row for row in listing["agents"]
                if row.get("userAgent") or row.get("actor") == USER_AGENT_NAME], listing
    return listing


def _user_agent_rows(lifecycle, budget=30.0):
    deadline = time.monotonic() + budget
    while True:
        code, listing = lifecycle.cli_json("agent", "list", "--include-user-agent", "--json")
        assert code == 0 and listing["ok"] is True, listing
        rows = [item for item in listing["agents"] if item.get("userAgent") is True]
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(0.2)


def test_same_account_logs_in_to_the_same_process_after_an_owned_logout_preserving_agents(lifecycle):
    _write_login_owned_settings(lifecycle)
    child = lifecycle.spawn()
    serving = lifecycle.serving(child)
    assert serving["owner"] == LOGIN_OWNER
    code, created = lifecycle.cli_json("agent", "create", "--name", "firstowner", "--json")
    assert code == 0 and created["ok"] is True, created
    assert "firstowner" in json.dumps(lifecycle.cli_json("agent", "list", "--json")[1])

    _logout(lifecycle, "z6pre-logout-key-0002")
    lifecycle.wait_phase({"bootstrap"}, child, budget=60)
    lifecycle.login_and_initialize("z6pre0002")
    again = lifecycle.serving(child)

    assert again["pid"] == child.pid and again["owner"] == LOGIN_OWNER
    settings = json.loads((lifecycle.home / "settings.json").read_text())
    assert settings["owner"] == LOGIN_OWNER
    _wait_user_agent_present(lifecycle)
    agents = _default_listing(lifecycle)
    [user_agent] = _user_agent_rows(lifecycle)
    assert user_agent["owner"] == LOGIN_OWNER, user_agent
    assert child.poll() is None, lifecycle.child_log(child)

    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)

    fresh = lifecycle.spawn()
    fresh_ping = lifecycle.serving(fresh)
    assert fresh_ping["owner"] == LOGIN_OWNER
    _wait_user_agent_present(lifecycle)
    fresh_agents = _default_listing(lifecycle)
    assert _agent_view(agents) == _agent_view(fresh_agents)
    user_agents = _user_agent_rows(lifecycle)
    assert [(row["agentId"], row["owner"]) for row in user_agents] == [
        (user_agent["agentId"], LOGIN_OWNER)
    ], user_agents
    fresh.send_signal(signal.SIGTERM)
    assert fresh.wait(timeout=90) == 0, lifecycle.child_log(fresh)
    lifecycle.assert_nothing_left(fresh.pid)

