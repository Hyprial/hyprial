"""Z-6c: a host whose daemon is gone stays up, answers DAEMON_UNAVAILABLE and recovers.

Real processes on the native engine.  A serving host and a host attached in
bootstrap lose their daemon (``hyprial daemon stop``); the host process, its
origin and its session survive; every request answers 503 ``DAEMON_UNAVAILABLE``
(not dispatched), ``/daemon/readyz`` too; when a daemon returns the same host
serves again without being restarted: the same generation after a restart under
the same account, a new generation when the daemon returns in another phase.
The daemon of these tests is started by the test harness, not by ``hyprial gui``,
so the harness plays the long-lived spawner and reaps it; that the real launcher
reaps its own daemon is ``test_launcher_reaps_daemon.py``.
Decision: L1 (owner 2026-11-21).  ``hyprial logout`` with the GUI open no longer
makes the daemon absent: it is one daemon ``auth.logout``, the same daemon
process returns to bootstrap and the host follows in place, as the GUI's own
logout does in ``test_initialize_in_place.py``: same host pid and birth, the same
view under a new ``anonymous-bootstrap`` generation, the retired headers refused
with 401.  The fixed wait past the old reopen deadline (the 5 second
``shutdownDrainSeconds`` default, after which the pre-Z-6c child ended itself)
stays: no condition of the host can be polled for, so the test waits past it and
asserts the host and the daemon are both still there and the session current.
"""

from __future__ import annotations

import time

from hyprial.kernel import process_birth_identity
from tests.integration.gui_daemon_first.support import HostGui, HostSession, assert_unavailable, reach_serving

from .conftest import pytestmark  # noqa: F401

HOST_DEADLINE_SECONDS = 8


def _absent(session: HostSession) -> None:
    assert_unavailable(*session.call("onboarding.status"))
    assert session.ready() == 503


def _stop_daemon(lifecycle, daemon) -> None:
    code, payload = lifecycle.cli_json("daemon", "stop", "--json")
    assert code == 0 and payload["ok"] is True, payload
    assert daemon.wait(timeout=90) == 0, lifecycle.child_log(daemon)


def test_a_serving_host_answers_unavailable_and_recovers_with_the_same_generation(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        reach_serving(lifecycle, daemon, session, "absent01")
        assert session.request("daemon.status")
        generation = session.opened["generation"]
        epoch = session.ready_epoch()

        _stop_daemon(lifecycle, daemon)
        _absent(session)
        assert_unavailable(*session.call("daemon.status"))
        assert gui.process.poll() is None, gui.logs()

        again = lifecycle.spawn()
        lifecycle.serving(again)
        assert session.request("daemon.status")["running"] is True
        assert session.current() is True and session.opened["generation"] == generation
        assert session.ready_epoch() != epoch
        assert gui.process.poll() is None, gui.logs()

        assert gui.stop() == 0, gui.logs()


def test_a_bootstrap_host_waits_for_a_daemon_and_rebinds_when_it_returns(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        before = dict(session.opened)
        retired = session.headers()

        _stop_daemon(lifecycle, daemon)
        _absent(session)
        assert gui.process.poll() is None, gui.logs()

        again = lifecycle.spawn()
        lifecycle.wait_phase({"bootstrap"}, again)
        session.wait_changed()
        after = session.refresh()
        assert after["view"] == before["view"] and after["generation"] != before["generation"]
        assert after["principal"]["kind"] == "anonymous-bootstrap"
        assert session.request("onboarding.status")["phase"] == "bootstrap"
        assert session.ready(headers=retired) == 401
        assert gui.process.poll() is None, gui.logs()

        assert gui.stop() == 0, gui.logs()


def _refused(session: HostSession, retired: dict[str, str]) -> None:
    assert session.ready(headers=retired) == 401
    status, body = session.call("onboarding.status", headers=retired)
    assert status == 401 and body["dispatch"] == "not-dispatched", body


def test_a_cli_logout_with_the_gui_open_leaves_the_host_running(lifecycle):
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    with HostGui(lifecycle) as gui:
        gui.read_record()
        session = gui.open_session()
        host_pid = gui.host_pid
        host_birth = process_birth_identity(host_pid)
        assert host_birth is not None
        reach_serving(lifecycle, daemon, session, "absent03")
        retired = session.headers()
        serving = dict(session.opened)
        serving_ping = lifecycle.ping()
        assert serving_ping is not None and serving_ping["pid"] == daemon.pid

        code, payload = lifecycle.cli_json("logout", "--yes", "--json")
        assert code == 0 and payload["status"] == "logged-out", payload
        assert payload["daemon"] == {"stopRequested": True}, payload
        again = lifecycle.wait_phase({"bootstrap"}, daemon, budget=60)
        assert again["pid"] == daemon.pid and again["owner"] is None, again
        assert again["epoch"] != serving_ping["epoch"], again

        session.wait_changed()
        reopened = session.refresh()
        assert reopened["view"] == serving["view"] and reopened["generation"] != serving["generation"]
        assert reopened["principal"]["kind"] == "anonymous-bootstrap"
        status = session.request("onboarding.status")
        assert status["phase"] == "bootstrap" and status["authenticated"] is False
        _refused(session, retired)
        assert gui.children() == [host_pid]
        assert process_birth_identity(host_pid) == host_birth

        time.sleep(HOST_DEADLINE_SECONDS)
        assert daemon.poll() is None, lifecycle.child_log(daemon)
        assert gui.process.poll() is None, gui.logs()
        assert process_birth_identity(host_pid) == host_birth
        assert session.current() is True and session.ready() == 200
        assert session.request("onboarding.status")["phase"] == "bootstrap"
        assert lifecycle.recorded_pids() == {daemon.pid}

        assert gui.stop() == 0, gui.logs()
    assert lifecycle.ping()["pid"] == daemon.pid
