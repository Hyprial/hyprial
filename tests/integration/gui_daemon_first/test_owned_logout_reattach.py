"""Logout through the host when the daemon was started on a home that already had an owner.

Decision: daemon-first-z6pre-owned-logout-20261117.  The daemon stays in its own
process after the logout and returns to bootstrap; the host follows in place
without an owner, stays a forwarder and never takes a lock, so ``bootstrap.json``
names the daemon's pid.
"""

from __future__ import annotations

import json

from .conftest import pytestmark  # noqa: F401
from .support import ISSUER, HostGui, alive

OWNER = "z2-owner@example.test"


def test_an_owned_start_returns_to_bootstrap_and_the_host_reattaches(lifecycle):
    (lifecycle.home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": OWNER, "identityMode": "casdoor", "identityIssuer": ISSUER}),
        encoding="utf-8",
    )
    daemon = lifecycle.spawn()
    serving_ping = lifecycle.serving(daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        serving = dict(session.opened)
        retired = session.headers()
        assert serving["principal"]["kind"] == "serving" and serving["principal"]["owner"] == OWNER

        session.call("auth.logout", {"confirm": True, "operationKey": "z6pre-logout-key-0003"})
        again = lifecycle.wait_phase({"bootstrap"}, daemon, budget=60)
        assert again["pid"] == daemon.pid and again["owner"] is None
        assert again["epoch"] != serving_ping["epoch"]

        session.wait_changed()
        reopened = session.refresh()
        assert reopened["view"] == serving["view"] and reopened["generation"] != serving["generation"]
        assert reopened["principal"]["kind"] == "anonymous-bootstrap"
        status = session.request("onboarding.status")
        assert status["phase"] == "bootstrap" and status["authenticated"] is False
        refused, body = session.call("onboarding.status", headers=retired)
        assert refused == 401 and body["dispatch"] == "not-dispatched", body
        marker = json.loads((lifecycle.state / "bootstrap.json").read_text(encoding="utf-8"))
        assert marker["pid"] == daemon.pid
        assert lifecycle.recorded_pids() == {daemon.pid}
        assert daemon.poll() is None, lifecycle.child_log(daemon)
        assert gui.process.poll() is None and alive(gui.host_pid), gui.logs()

        assert gui.stop() == 0, gui.logs()
    assert lifecycle.ping()["pid"] == daemon.pid
