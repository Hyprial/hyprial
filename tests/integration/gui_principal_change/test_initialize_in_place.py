"""Z-6c: initialize and logout rebind the host in place.

Real processes on the native engine: an ownerless daemon in bootstrap and the real
``hyprial gui`` host on the same isolated home.  Login and initialize go through the
host's session; the daemon promotes in place and the host keeps its pid, listener
and origin while the view moves to a new generation that carries the owner; logout
returns both to bootstrap the same way.  The headers of every retired generation
are refused after every change.  The daemon's identity port is the controlled
entry (no network issuer).
"""

from __future__ import annotations

from hyprial.kernel import process_birth_identity
from tests.integration.gui_daemon_first.support import (
    OWNER,
    HostGui,
    HostSession,
    alive,
    reach_serving,
)

from .conftest import pytestmark  # noqa: F401


def _refused(session: HostSession, retired: dict[str, str]) -> None:
    assert session.ready(headers=retired) == 401
    status, body = session.call("onboarding.status", headers=retired)
    assert status == 401 and body["dispatch"] == "not-dispatched", body


def test_initialize_and_logout_keep_the_host_listener_and_reissue_the_generation(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        before = dict(session.opened)
        first = session.headers()
        assert before["principal"]["kind"] == "anonymous-bootstrap"
        daemon_pid = lifecycle.ping()["pid"]
        host_pid = gui.host_pid
        host_birth = process_birth_identity(host_pid)
        assert gui.children() == [host_pid] and host_birth is not None
        cookies = {session.cookies}

        reach_serving(lifecycle, daemon, session, "inplace01")
        serving = dict(session.opened)
        second = session.headers()
        cookies.add(session.cookies)
        assert alive(host_pid)
        assert gui.children() == [host_pid]
        assert process_birth_identity(host_pid) == host_birth
        assert session.ready() == 200
        assert serving["view"] == before["view"]
        assert serving["generation"] != before["generation"]
        assert serving["principal"]["kind"] == "serving" and serving["principal"]["owner"] == OWNER
        assert serving["catalog"]["digest"] == before["catalog"]["digest"]
        _refused(session, first)
        status = session.request("onboarding.status")
        assert status["phase"] == "serving" and status["initialized"] is True
        assert lifecycle.ping()["pid"] == daemon_pid

        session.call("auth.logout", {"confirm": True, "operationKey": "z6c-logout-key-0001"})
        again = lifecycle.wait_phase({"bootstrap"}, daemon, budget=60)
        assert again["pid"] == daemon_pid and again["owner"] is None
        session.wait_changed()
        reopened = session.refresh()
        cookies.add(session.cookies)
        assert session.ready() == 200
        assert gui.children() == [host_pid]
        assert process_birth_identity(host_pid) == host_birth
        assert reopened["generation"] not in {before["generation"], serving["generation"]}
        assert reopened["principal"]["kind"] == "anonymous-bootstrap"
        assert reopened["catalog"]["digest"] == before["catalog"]["digest"]
        _refused(session, second)
        _refused(session, first)
        status = session.request("onboarding.status")
        assert status["phase"] == "bootstrap" and status["authenticated"] is False
        assert gui.process.poll() is None, gui.logs()

        csrfs = {before["csrf"], serving["csrf"], reopened["csrf"]}
        cookie_values = {
            part.split("=", 1)[1] for header in cookies for part in header.split("; ") if "=" in part
        }
        logs = gui.logs()
        assert gui.stop() == 0, gui.logs()
        after = lifecycle.ping()
        assert after is not None and after["pid"] == daemon_pid
        logs += gui.logs()
        assert cookie_values and all(cookie_values)
        for secret in (session.grant, *csrfs, *cookie_values):
            assert str(secret) not in logs
