"""KP-083 regression: the host attaches to a bootstrap daemon instead of taking its locks.

Real processes on the native engine: an ownerless daemon in bootstrap and the real
``hyprial gui`` host on the same isolated home.  Login and initialize go through the
host's session; the daemon promotes in place, the host follows in place (same
origin, same process, a new generation of the same view), and ending the GUI leaves
the daemon running.  Real OIDC and a real Tailcat sidecar are not exercised (the
daemon's identity port is the controlled entry).
"""

from __future__ import annotations

from .conftest import pytestmark  # noqa: F401
from .support import OWNER, HostGui, alive, assert_unavailable, reach_serving


def test_the_host_forwards_to_the_bootstrap_daemon_and_initialize_happens_in_it(lifecycle):
    daemon = lifecycle.spawn()
    first = lifecycle.wait_phase({"bootstrap"}, daemon)
    assert first["pid"] == daemon.pid and first["owner"] is None

    with HostGui(lifecycle) as gui:
        record = gui.read_record()
        assert record["backend_pid"] is None and record["backend_principal"] is None
        session = gui.open_session()
        before = dict(session.opened)
        retired = session.headers()
        assert before["principal"]["kind"] == "anonymous-bootstrap"
        assert session.ready() == 200
        status = session.request("onboarding.status")
        assert status["phase"] == "bootstrap" and status["authenticated"] is False
        assert lifecycle.recorded_pids() == {daemon.pid}
        assert lifecycle.ping()["pid"] == daemon.pid

        reach_serving(lifecycle, daemon, session, "attach001")
        serving = session.opened
        assert serving["view"] == before["view"] and serving["generation"] != before["generation"]
        assert serving["principal"]["kind"] == "serving" and serving["principal"]["owner"] == OWNER
        assert gui.children() == [gui.host_pid] and alive(gui.host_pid)
        assert session.ready(headers=retired) == 401
        status, refused = session.call("onboarding.status", headers=retired)
        assert status == 401 and refused["dispatch"] == "not-dispatched", refused
        status = session.request("onboarding.status")
        assert status["phase"] == "serving" and status["initialized"] is True
        ping = lifecycle.ping()
        assert ping["pid"] == daemon.pid and ping["owner"] == OWNER
        assert gui.process.poll() is None, gui.logs()

        assert gui.stop() == 0, gui.logs()
    after = lifecycle.ping()
    assert after is not None and after["pid"] == daemon.pid and after["epoch"] == ping["epoch"]


def test_a_daemon_stop_leaves_a_serving_host_open_and_it_recovers_when_a_daemon_returns(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        reach_serving(lifecycle, daemon, session, "stop0001")
        assert session.request("daemon.status")
        generation = session.opened["generation"]

        code, payload = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and payload["ok"] is True, payload
        assert daemon.wait(timeout=90) == 0, lifecycle.child_log(daemon)

        assert_unavailable(*session.call("daemon.status"))
        assert gui.process.poll() is None, gui.logs()

        again = lifecycle.spawn()
        lifecycle.serving(again)
        assert session.request("daemon.status")["running"] is True
        assert session.current() is True and session.opened["generation"] == generation
        assert gui.process.poll() is None, gui.logs()

        assert gui.stop() == 0, gui.logs()
