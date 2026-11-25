"""A business command on an ownerless home starts the bootstrap daemon and refuses once (Z-4).

Decision: daemon-first-z4-ownerless-ensure-20261117.  Real CLI, native engine,
isolated short-path home; ``HYPRIAL_DAEMON_AUTOSTART`` is removed from the
child environment only (``HYPRIAL_SERVICE_AUTOINSTALL=0`` stays, so no unit is
ever installed).  No login is performed; the identity service is not involved.
"""

from __future__ import annotations

import pytest

from .conftest import pytestmark  # noqa: F401

DAEMON_TEXT = "this daemon has no account yet; sign in first"


def test_a_business_command_starts_the_daemon_and_ends_with_account_required(lifecycle):
    lifecycle.env.pop("HYPRIAL_DAEMON_AUTOSTART", None)
    assert lifecycle.env["HYPRIAL_SERVICE_AUTOINSTALL"] == "0"

    code, refused = lifecycle.cli_json("agent", "list", "--json")
    assert code == 1 and refused["ok"] is False, refused
    assert refused["code"] == "ACCOUNT_REQUIRED" and refused["error"] == DAEMON_TEXT, refused
    assert refused["data"]["nextSteps"] == ["hyprial login"]

    code, status = lifecycle.cli_json("daemon", "status", "--json")
    assert code == 0 and status["daemon"]["running"] is True
    assert status["daemon"]["phase"] == "bootstrap"
    pid = status["daemon"]["pid"]

    code, again = lifecycle.cli_json("agent", "list", "--json")
    assert code == 1 and again["code"] == "ACCOUNT_REQUIRED"
    code, status = lifecycle.cli_json("daemon", "status", "--json")
    assert status["daemon"]["pid"] == pid

    code, stopped = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 0 and stopped["ok"] is True, stopped
    lifecycle.assert_nothing_left(pid)


@pytest.mark.parametrize("arguments", [["org", "list"], ["user", "list"], ["adapter", "list"]])
def test_other_command_groups_end_with_the_daemons_refusal(lifecycle, arguments):
    lifecycle.env.pop("HYPRIAL_DAEMON_AUTOSTART", None)

    code, refused = lifecycle.cli_json(*arguments, "--json")
    assert code == 1, refused
    assert refused["code"] == "ACCOUNT_REQUIRED" and refused["error"] == DAEMON_TEXT, refused

    code, stopped = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 0, stopped


def test_the_opt_out_keeps_the_unavailable_refusal_and_starts_nothing(lifecycle):
    assert lifecycle.env["HYPRIAL_DAEMON_AUTOSTART"] == "0"

    result = lifecycle.cli("agent", "list", "--json")

    socket = lifecycle.state / "daemon.sock"
    assert result.returncode == 1
    assert result.stdout == (
        '{"ok":false,"code":"DAEMON_UNAVAILABLE","error":"cannot connect to the Harness daemon socket '
        f'{socket}: [Errno 2] No such file or directory"}}\n'
    )
    assert result.stderr == ""
    assert lifecycle.recorded_pids() == set() and not socket.exists()


def test_a_status_probe_never_starts_the_daemon(lifecycle):
    lifecycle.env.pop("HYPRIAL_DAEMON_AUTOSTART", None)

    code, status = lifecycle.cli_json("daemon", "status", "--json")

    assert code == 0 and status["daemon"]["running"] is False, status
    assert lifecycle.recorded_pids() == set()


def test_a_status_probe_and_a_human_failure_with_the_opt_out_print_the_unchanged_text(lifecycle):
    socket = lifecycle.state / "daemon.sock"
    result = lifecycle.cli("agent", "list")

    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == (
        f"hyprial: cannot connect to the Harness daemon socket {socket}: [Errno 2] No such file or directory\n"
    )
    assert lifecycle.recorded_pids() == set()
