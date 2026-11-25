"""A business command on an owned home starts the serving daemon and succeeds (Z-4).

Decision: daemon-first-z4-owned-ensure-20261117.  The home is owned by writing
``settings.json`` as the lifecycle owner-home tests do; nothing signs in.
"""

from __future__ import annotations

import json

from .conftest import pytestmark  # noqa: F401


def _own(lifecycle) -> None:
    (lifecycle.home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "z4-owner@example.test"}), encoding="utf-8"
    )


def test_the_command_starts_the_daemon_and_returns_its_result(lifecycle):
    _own(lifecycle)
    lifecycle.env.pop("HYPRIAL_DAEMON_AUTOSTART", None)

    code, agents = lifecycle.cli_json("agent", "list", "--json")
    assert code == 0 and agents["ok"] is True, agents

    code, status = lifecycle.cli_json("daemon", "status", "--json")
    assert code == 0 and status["daemon"]["running"] is True
    pid = status["daemon"]["pid"]

    code, stopped = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 0, stopped
    lifecycle.assert_nothing_left(pid)


def test_the_opt_out_keeps_the_unavailable_refusal_and_starts_nothing(lifecycle):
    _own(lifecycle)
    assert lifecycle.env["HYPRIAL_DAEMON_AUTOSTART"] == "0"

    code, refused = lifecycle.cli_json("agent", "list", "--json")

    assert code == 1 and refused["code"] == "DAEMON_UNAVAILABLE", refused
    assert lifecycle.recorded_pids() == set()
