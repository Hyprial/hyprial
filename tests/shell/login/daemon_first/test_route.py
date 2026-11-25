"""Which route ``hyprial login`` takes, and that the first login is one daemon operation.

Decision: daemon-first-z5-cli-route-20261119.  The routing table of the plan is
asserted cell by cell on the scripted daemon: a bootstrap daemon, or no daemon on
a home with no owner, runs the first login through the daemon; a serving daemon on
a home with no owner is refused by the daemon itself (``IDENTITY_CONFLICT``,
surfaced as is); every home with an owner keeps the in-process owned route and
starts nothing.  The identity lock is held by the test throughout one case: the
live defect was that a bootstrap daemon holds it.
"""

from __future__ import annotations

import pytest

from hyprial.identity import IdentityTransactionLock
from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS

from .support import OPERATION_ID, URI

FIRST_LOGIN_CALLS = ["ping", "auth.login.begin", "auth.operation.get", "ping", "org.list"]


@pytest.mark.parametrize(
    ("cell", "phase", "env_owner", "launches"),
    [
        ("missing", None, False, 1),
        ("ownerless", None, False, 1),
        ("ownerless", "bootstrap", False, 0),
        ("owner", "bootstrap", False, 0),
        ("ownerless", "bootstrap", True, 0),
    ],
    ids=[
        "no-daemon-missing-home",
        "no-daemon-ownerless",
        "bootstrap-ownerless",
        "bootstrap-with-owner",
        "bootstrap-env-owner",
    ],
)
def test_the_daemon_runs_the_first_login(make_world, cell, phase, env_owner, launches):
    world = make_world(cell=cell, phase=phase, env_owner=env_owner)

    result = world.login("--no-open")

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(world.launches) == launches
    assert world.methods == FIRST_LOGIN_CALLS
    assert world.owned == []
    assert world.home.is_dir()


def test_a_launch_is_the_one_the_login_already_used_and_comes_before_the_begin(make_world):
    world = make_world(cell="ownerless")

    result = world.login("--no-open")

    assert result.exit_code == 0, result.stderr
    assert world.launches == [{"ready_timeout": DAEMON_START_IDLE_BUDGET_SECONDS, "autoinstall": False}]
    assert world.methods[:2] == ["ping", "auth.login.begin"]


def test_the_init_ready_timeout_environment_bounds_the_bootstrap_launch(make_world, monkeypatch):
    monkeypatch.setenv("HYPRIAL_INIT_READY_TIMEOUT", "2.75")
    world = make_world(cell="ownerless")

    result = world.login("--no-open")

    assert result.exit_code == 0, result.stderr
    assert [launch["ready_timeout"] for launch in world.launches] == [2.75]


def test_a_missing_home_is_created_by_login(make_world):
    world = make_world(cell="missing")
    assert not world.home.exists()

    result = world.login("--no-open")

    assert result.exit_code == 0, result.stderr
    assert world.home.is_dir()


@pytest.mark.parametrize("flag", ("--force", "--switch-account", "--dry-run", "--provider"))
def test_the_retired_login_options_are_gone(make_world, flag: str):
    world = make_world(cell="owner")

    refused = world.login(flag)
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.commands.common.root import app

    helped = CliRunner().invoke(app, ["login", "--help"])

    assert refused.exit_code == 2 and "No such option" in (refused.stderr or refused.output)
    assert flag not in helped.stdout
    assert world.calls == [] and world.launches == []


def test_an_owned_home_runs_one_auth_relogin_and_wait_via_daemon(make_world):
    world = make_world(cell="owner", phase="serving")

    result = world.login("--no-open")

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert world.owned == [] and world.launches == []
    assert world.methods == ["ping", "auth.relogin", "auth.operation.get", "ping", "org.list"]
    begin, wait = world.auth()
    assert begin.params["until"] == "serving"
    assert begin.params["operationKey"].startswith("cli-relogin-")
    assert wait.params == {"operationId": OPERATION_ID, "waitSeconds": 900}
    assert wait.options["timeout"] == 915.0


def test_an_owned_home_with_stopped_daemon_launches_it_for_relogin(make_world):
    world = make_world(cell="owner", phase=None)

    result = world.login("--no-open")

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert world.owned == [] and len(world.launches) == 1
    assert world.methods == ["ping", "auth.relogin", "auth.operation.get", "ping", "org.list"]


def test_a_serving_daemon_on_a_home_without_owner_is_refused_by_the_daemon(make_world):
    world = make_world(cell="ownerless", phase="serving")
    world.answers["auth.login.begin"] = [
        RequestPortError(ipc_errors.IDENTITY_CONFLICT, "this daemon already has an account", None)
    ]

    result = world.login("--no-open")

    payload = world.payload(result)
    assert result.exit_code == 1
    assert payload["ok"] is False and payload["code"] == "IDENTITY_CONFLICT"
    assert payload["error"] == "this daemon already has an account"
    assert world.methods == ["ping", "auth.login.begin"]
    assert world.launches == [] and world.owned == []


def test_a_held_identity_lock_does_not_matter_to_a_login_the_daemon_runs(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    held = IdentityTransactionLock.acquire(world.home, timeout=0)
    try:
        result = world.login("--no-open")
    finally:
        held.close()

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert world.methods == FIRST_LOGIN_CALLS


def test_one_begin_one_wait_and_the_arguments_the_daemon_publishes(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")

    result = world.login("--no-open")

    assert result.exit_code == 0, result.stderr
    begin, wait = world.auth()
    assert begin.params["intent"] == "login" and begin.params["until"] == "serving"
    assert begin.params["operationKey"].startswith("cli-login-")
    assert wait.params == {"operationId": OPERATION_ID, "waitSeconds": 900}
    assert wait.options["timeout"] == 915.0
    assert world.opened == []


def test_the_browser_is_opened_once_unless_no_open(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")

    assert world.login().exit_code == 0
    assert world.opened == [URI]

    again = make_world(cell="ownerless", phase="bootstrap")
    assert again.login("--no-open").exit_code == 0
    assert again.opened == []


def test_the_no_daemon_option_is_gone(make_world):
    world = make_world(cell="owner")

    refused = world.login("--no-daemon")
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.commands.common.root import app

    helped = CliRunner().invoke(app, ["login", "--help"])

    assert refused.exit_code == 2 and "No such option" in (refused.stderr or refused.output)
    assert "--no-daemon" not in helped.stdout
    assert world.calls == [] and world.launches == []


@pytest.mark.parametrize("cell", ["ownerless", "missing"])
def test_a_probe_that_fails_transiently_without_an_owner_is_surfaced_and_launches_nothing(
    make_world, cell
):
    world = make_world(cell=cell, phase="serving")
    world.answers["ping"] = [ipc_errors.IpcTimeoutError("ping timed out")]

    result = world.login("--no-open")

    assert result.exit_code == 1, (result.stdout, result.stderr)
    assert world.payload(result)["ok"] is False
    assert world.launches == [] and world.owned == []
    assert world.methods == ["ping"]
