"""Forwarding redial: a grown slot set is dialed without losing the others.

Step 3 of the forwarding defaults plan (§C): ``_reconcile_forwarding_endpoints``
rebuilds the Zenoh session through ``ZenohTransport.reconfigure_connect`` (step
2, whose real-session behaviour is pinned in test_zenoh_reconfigure.py) when the
connect set gains an endpoint the session does not dial. With the fixed slot
pool the set changes only on pool growth (the one remaining rebuild; design §5),
so these drive growth and pin the daemon side: the connect set it asks for
(startup order preserved), when ``dialed`` advances, and that a failed rebuild
is neither hidden nor retried on every tick.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

CONFIGURED = ("tcp/10.0.0.9:7447",)
DISCOVERED = ("tcp/100.64.29.9:7447",)
SLOT0 = "unixsock-stream/s/slot-00"
SLOT1 = "unixsock-stream/s/slot-01"


class _Controller:
    """A trivial slot control surface: status answers, nothing is mapped.

    The redial mechanics live in ``_redial_forwarding``; the connect set
    is driven by the slots callable (pool growth), so this controller
    only has to answer ``status`` without raising.
    """

    peers_reported = True

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]:
        return (), {}

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        return slot

    def unmap_peer(self, peer: str) -> None:
        pass

    def close(self) -> None:
        pass


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = False
        self.endpoint_peers: dict[str, str] = {}
        self.map_peer_remap_count = 0

    def set_endpoint_peers(self, peers: dict[str, str]) -> None:
        current = {endpoint: peer for peer, endpoint in peers.items()}
        for endpoint, peer in current.items():
            previous = next(
                (known for known, owner in self.endpoint_peers.items() if owner == peer),
                None,
            )
            if previous is not None and previous != endpoint:
                self.map_peer_remap_count += 1
        self.endpoint_peers = current

    def link_diagnostic_counters(self) -> dict[str, int]:
        return {"map_peer_remap": self.map_peer_remap_count}

    def reconfigure_connect(self, endpoints: tuple[str, ...]) -> None:
        self.calls.append(tuple(endpoints))
        if self.fail:
            raise RuntimeError("new session would not open")


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    # The slot set this incarnation dials; growth appends to it (the org
    # directory is empty here, so the slot reconcile is a no-op and this
    # callable alone drives the connect set).
    slots = [SLOT0]
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="redial-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=ForwardingEndpoints(
            _Controller(), slots=lambda: tuple(slots)
        ),
    **_deep_module_daemon_dependencies())
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        application,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    # The session opened when only the first slot was dialed, alongside a
    # configured peer and a command-directory one (startup composes all three).
    first = application._discover_peer_endpoints()
    assert first == (SLOT0,)
    application._connect_configured = CONFIGURED
    application._connect_discovered = DISCOVERED
    application.zenoh_connect = CONFIGURED + first + DISCOVERED
    transport = _Transport()
    transport.set_endpoint_peers({"100.64.20.2": first[0]})
    application._transport = transport  # type: ignore[assignment]
    try:
        yield application, slots, transport, events
    finally:
        application._transport = None
        application._close()


def _named(events, name):
    return [fields for event, fields in events if event == name]


def test_a_grown_slot_is_dialed_without_dropping_the_others(app) -> None:
    application, slots, transport, events = app
    slots.append(SLOT1)  # pool growth

    application._reconcile_forwarding_endpoints()

    wanted = (SLOT0, SLOT1)
    # Configured first, forwarding next, discovery after: startup order,
    # so the redial never drops a configured or discovered peer.
    assert transport.calls == [CONFIGURED + wanted + DISCOVERED]
    status = application._forwarding_status_json()
    assert status["dialed"] == list(wanted)
    assert status["restartRequired"] is False
    assert application.zenoh_connect == CONFIGURED + wanted + DISCOVERED
    assert _named(events, "zenoh.forwarding.changed")[-1]["restartRequired"] is False
    assert _named(events, "zenoh.forwarding.redialed")[-1]["dialed"] == list(wanted)

    # Steady state: nothing new to dial, no second rebuild.
    application._reconcile_forwarding_endpoints()
    assert len(transport.calls) == 1


def test_map_peer_remap_counter_only_advances_when_a_peers_endpoint_changes(app) -> None:
    application, _slots, transport, _events = app
    assert application._forwarding_status_json()["counters"] == {
        "map_peer_remap": 0
    }

    transport.set_endpoint_peers({"100.64.20.2": SLOT1})
    transport.set_endpoint_peers({"100.64.20.2": SLOT1})

    assert application._forwarding_status_json()["counters"] == {
        "map_peer_remap": 1
    }


def test_a_failed_rebuild_stays_visible_and_is_not_retried_every_tick(app) -> None:
    application, slots, transport, events = app
    transport.fail = True
    slots.append(SLOT1)

    application._reconcile_forwarding_endpoints()
    application._reconcile_forwarding_endpoints()

    assert len(transport.calls) == 1
    status = application._forwarding_status_json()
    assert status["dialed"] == [SLOT0]
    assert status["restartRequired"] is True
    assert application.zenoh_connect == CONFIGURED + (SLOT0,) + DISCOVERED
    assert len(_named(events, "zenoh.forwarding.redial_failed")) == 1
    assert _named(events, "zenoh.forwarding.changed")[-1]["restartRequired"] is True

    # A later change in the set is a new attempt.
    transport.fail = False
    slots.append("unixsock-stream/s/slot-02")
    application._reconcile_forwarding_endpoints()
    assert len(transport.calls) == 2
    assert application._forwarding_status_json()["restartRequired"] is False


def test_a_pure_shrink_does_not_rebuild(app) -> None:
    """A connect set that only loses endpoints must not cost a session
    interruption: dialing a momentarily dead slot is harmless."""

    application, slots, transport, _events = app
    slots.clear()  # the backend now reports nothing new

    application._reconcile_forwarding_endpoints()

    assert transport.calls == []
    status = application._forwarding_status_json()
    assert status["dialed"] == [SLOT0]


def test_without_a_session_the_gap_is_reported_as_before(app) -> None:
    application, slots, _transport, events = app
    application._transport = None
    slots.append(SLOT1)

    application._reconcile_forwarding_endpoints()

    assert application._forwarding_status_json()["restartRequired"] is True
    assert _named(events, "zenoh.forwarding.redialed") == []
