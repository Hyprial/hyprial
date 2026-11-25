"""Logout through the host: the daemon returns to bootstrap in its own process.

The host follows in place (same origin and process, the view's next generation has
no owner), stays a forwarder and never takes a lock; the retired generation is
refused.  The daemon here was started on an ownerless home; the owned start is
covered by ``test_owned_logout_reattach.py``.  A logout before initialize
(bootstrap to bootstrap) rotates the generation the same way, which the deleted
child-hosted lifecycle test used to prove.
"""

from __future__ import annotations

from .conftest import pytestmark  # noqa: F401
from .support import OWNER, HostGui, alive, reach_serving


def test_logout_returns_the_daemon_to_bootstrap_and_the_host_reattaches(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        reach_serving(lifecycle, daemon, session, "logout01")
        serving_ping = lifecycle.ping()
        retired = session.headers()
        serving = dict(session.opened)

        session.call("auth.logout", {"confirm": True, "operationKey": "z6a-logout-key-0001"})
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
        assert lifecycle.recorded_pids() == {daemon.pid}
        assert gui.process.poll() is None and alive(gui.host_pid), gui.logs()

        assert gui.stop() == 0, gui.logs()
    assert lifecycle.ping()["pid"] == daemon.pid


def test_a_logout_before_initialize_republishes_the_bootstrap_entry(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        before = dict(session.opened)
        retired = session.headers()
        begun = session.request("auth.login.begin", {"intent": "login", "operationKey": "z6b-login-key-pre01"})
        got = session.request("auth.operation.get", {"operationId": begun["operationId"]})
        assert got["operation"]["state"] == "authenticated", got
        first_ping = lifecycle.ping()

        session.call("auth.logout", {"confirm": True, "operationKey": "z6b-logout-key-pre01"})
        session.wait_changed()
        reopened = session.refresh()
        assert reopened["view"] == before["view"] and reopened["generation"] != before["generation"]
        again = lifecycle.wait_phase({"bootstrap"}, daemon, budget=60)
        assert again["pid"] == daemon.pid and again["owner"] is None
        assert again["epoch"] != first_ping["epoch"]
        assert reopened["principal"]["kind"] == "anonymous-bootstrap"
        status = session.request("onboarding.status")
        assert status["phase"] == "bootstrap" and status["authenticated"] is False
        refused, body = session.call("onboarding.status", headers=retired)
        assert refused == 401 and body["dispatch"] == "not-dispatched", body
        assert lifecycle.recorded_pids() == {daemon.pid}

        reach_serving(lifecycle, daemon, session, "pre02")
        assert session.opened["principal"]["owner"] == OWNER
        assert session.request("onboarding.status")["initialized"] is True
        assert gui.process.poll() is None, gui.logs()

        assert gui.stop() == 0, gui.logs()
    assert lifecycle.ping()["pid"] == daemon.pid
