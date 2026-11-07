from __future__ import annotations

import errno
import socket
import time

import pytest


import hyprial.daemon.impl.network.peer_reachability as peer_reachability
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.network.peer_reachability import (
    PEER_CONNECT_START_TIMEOUT_SECONDS,
    classify_peer_reachability,
    tcp_probe,
)


def _incident_peer() -> dict[str, object]:
    """The peer shape the deleted tailnet projection used to produce."""

    return {
        "name": "relay-peer",
        "dnsName": "relay-peer.example.ts.net",
        "addresses": ["192.0.2.1"],
        "listenAddress": "192.0.2.1:7447",
        "online": True,
        "path": "relayed",
        "homeDerp": "sfo",
        "lastHandshakeAgeS": 900.0,
    }


def test_mesh_nodes_degrade_when_presence_has_no_raw_actor_accessor() -> None:
    application = object.__new__(DaemonApplication)
    application._presence = object()

    assert application._online_host_nodes() == ()


def test_tcp_probe_reports_open_and_ignores_proxy_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = listener.getsockname()

        result = tcp_probe(host, port, timeout=0.2)

    assert result == {"result": "open", "errno": None}


def test_tcp_probe_reports_refused_for_a_real_closed_loopback_port() -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        host, port = reserved.getsockname()

    result = tcp_probe(host, port, timeout=0.2)

    assert result["result"] == "refused"
    assert result["errno"] == errno.ECONNREFUSED


def test_tcp_probe_reports_timeout_for_test_net_blackhole() -> None:
    started = time.monotonic()
    result = tcp_probe("192.0.2.1", 7447, timeout=0.05)
    elapsed = time.monotonic() - started

    assert result["result"] == "timeout"
    assert result["result"] != "refused"
    assert elapsed >= 0.04, "TEST-NET control must exercise the real timeout path"


def test_incident_peer_timeout_is_data_plane_down() -> None:
    peer = _incident_peer()

    finding = classify_peer_reachability(peer, {"result": "timeout", "errno": None})

    assert finding == {
        "peer": "relay-peer",
        "verdict": "data_plane_down",
        "probe": {"result": "timeout", "errno": None},
        "path": "relayed",
        "homeDerp": "sfo",
        "lastHandshakeAgeS": 900.0,
    }


def test_refused_and_timeout_are_distinct_verdicts() -> None:
    peer = _incident_peer()

    refused = classify_peer_reachability(peer, {"result": "refused", "errno": 61})
    timed_out = classify_peer_reachability(peer, {"result": "timeout", "errno": 35})

    assert refused is not None and refused["verdict"] == "refused"
    assert timed_out is not None and timed_out["verdict"] == "data_plane_down"


def test_direct_fresh_open_peer_has_no_verdict() -> None:
    peer = {
        "name": "healthy-peer",
        "online": True,
        "path": "direct",
        "homeDerp": "fra",
        "lastHandshakeAgeS": 1.0,
    }

    assert classify_peer_reachability(peer, {"result": "open", "errno": None}) is None


def test_doctor_peer_probe_bound_is_two_seconds() -> None:
    assert PEER_CONNECT_START_TIMEOUT_SECONDS == 2.0


# infra-ops review of #851: "no route" and "name did not resolve" have different
# remedies from "the SYN went unanswered"; labelling them timeout recreates the
# old "closed" lumping one level down.


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
        (OSError(errno.ENETUNREACH, "Network is unreachable"), "unreachable"),
        (OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"), "error"),
        (socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided"), "error"),
        (TimeoutError("timed out"), "timeout"),
        (OSError(errno.ETIMEDOUT, "Operation timed out"), "timeout"),
    ],
)
def test_only_an_unanswered_connect_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch, raised: OSError, expected: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise raised

    monkeypatch.setattr(peer_reachability.socket, "create_connection", fail)
    probe = peer_reachability.tcp_probe("100.64.20.9", 7447, timeout=0.1)
    assert probe["result"] == expected
    if expected != "timeout":
        assert probe["errno"] is not None


def test_a_no_route_peer_gets_its_own_verdict_not_data_plane_down() -> None:
    peer = {"name": "p", "online": True, "path": "relayed", "homeDerp": "sfo", "lastHandshakeAgeS": 5.0}
    verdict = peer_reachability.classify_peer_reachability(
        peer, {"result": "unreachable", "errno": errno.EHOSTUNREACH}
    )
    assert verdict is not None and verdict["verdict"] == "no_route"
    other = peer_reachability.classify_peer_reachability(
        peer, {"result": "error", "errno": errno.EADDRNOTAVAIL}
    )
    assert other is not None and other["verdict"] == "probe_error"
