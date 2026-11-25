"""Scenario channel leases: registration, heartbeat ownership, reconnect.

Covers the session machinery ``contract/e2e/_group_conversation/cli.py`` owns
since T1b moved it out of the deleted E2E-026 fixture: an opaque operational
lease per registration, heartbeats that keep running without business
polling, a failure that surfaces at stop, and a reconnect that re-registers
the same actor with a fresh lease.
"""

from pathlib import Path
import sys
import threading

import pytest

_SCENARIOS = Path(__file__).resolve().parents[5] / "contract/e2e"
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))

from _group_conversation import cli  # noqa: E402
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION  # noqa: E402

ACTOR = "agent:e2e:group-node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"


class ChannelDaemon:
    def __init__(self):
        self.calls = []
        self.heartbeats = 0
        self.several_heartbeats = threading.Event()
        self.failure = None

    def request(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "session.register":
            return {"registered": True}
        assert method == "session.heartbeat"
        self.heartbeats += 1
        if self.heartbeats >= 3:
            self.several_heartbeats.set()
        if self.failure is not None:
            raise self.failure
        return {"alive": True}


def test_registration_uses_current_protocol_and_opaque_lease(tmp_path):
    daemon = ChannelDaemon()
    cli.register_session(daemon, ACTOR, "session-one", tmp_path / "workspace")
    method, registration = daemon.calls[0]
    assert method == "session.register"
    assert registration["actor"] == ACTOR
    assert registration["channelProtocolVersion"] == CHANNEL_PROTOCOL_VERSION
    lease = registration["channelLeaseToken"]
    assert isinstance(lease, str) and 32 <= len(lease) <= 256
    assert daemon.calls[1] == ("session.heartbeat", {
        "actor": ACTOR, "sessionRef": "session-one", "channelLeaseToken": lease,
    })


def test_owned_heartbeat_continues_without_pending_polling(tmp_path, monkeypatch):
    daemon = ChannelDaemon()
    monkeypatch.setattr(cli, "CHANNEL_HEARTBEAT_INTERVAL_SECONDS", 0.005, raising=False)
    cli.register_session(daemon, ACTOR, "session-one", tmp_path / "workspace")
    heartbeat = cli.SessionHeartbeat(daemon)
    try:
        heartbeat.start()
        assert daemon.several_heartbeats.wait(timeout=1)
    finally:
        heartbeat.stop()
    assert heartbeat.thread is None


def test_heartbeat_failure_surfaces_and_thread_is_joined(tmp_path, monkeypatch):
    daemon = ChannelDaemon()
    monkeypatch.setattr(cli, "CHANNEL_HEARTBEAT_INTERVAL_SECONDS", 0.005, raising=False)
    cli.register_session(daemon, ACTOR, "session-one", tmp_path / "workspace")
    heartbeat = cli.SessionHeartbeat(daemon)
    heartbeat.start()
    daemon.failure = AssertionError("heartbeat failed sentinel")
    try:
        assert daemon.several_heartbeats.wait(timeout=1)
    finally:
        with pytest.raises(AssertionError, match="heartbeat failed sentinel"):
            heartbeat.stop()
    assert heartbeat.thread is None


def test_reconnect_reregisters_same_official_actor_with_new_lease(tmp_path):
    daemon = ChannelDaemon()
    workspace = tmp_path / "workspace"
    cli.register_session(daemon, ACTOR, "session-one", workspace)
    heartbeat = cli.SessionHeartbeat(daemon)
    heartbeat.start()
    heartbeat.stop()
    try:
        heartbeat.reconnect()
    finally:
        heartbeat.stop()
    registrations = [params for method, params in daemon.calls if method == "session.register"]
    assert len(registrations) == 2
    before, after = registrations
    assert after["channelLeaseToken"] != before["channelLeaseToken"]
    assert after["actor"] == ACTOR
    assert after["sessionRef"] == "session-one"
    assert after["cwd"] == str(workspace)
    assert after["channelProtocolVersion"] == CHANNEL_PROTOCOL_VERSION
