"""The triggers: where the daemon re-derives a port's admission set.

Decision (service grant S4-py, 2026-11-26): brief goal 3 names four moments
at which the exposing device must push again, and goal 4 one more. Each test
here removes nothing and fakes nothing: it performs the operator action
through the public ``handle`` path of a real ``DaemonApplication`` and waits
for the expose it must cause to reach the sidecar's wire. The first test pins
the wiring itself, so that the others cannot pass because nothing was ever
pushed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.daemon.service_grants.enforcement.wiring._app import KEY_UNAVAILABLE, Daemon
from tests.daemon.service_grants.enforcement._support import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    MEMBERS,
    ORG,
    PORT,
    Harness,
    peer_key,
)

OTHER_PORT = PORT + 1
BOB_KEY = peer_key(BOB_NODE, ALICE_NODE)
EXPOSURE = {"port": PORT, "target": f"tcp:127.0.0.1:{PORT}", "proxyProtocol": "none"}


@pytest.fixture
def harness(tmp_path: Path) -> Any:
    harness = Harness(tmp_path)
    try:
        for member in MEMBERS:
            harness.publish(member)
        yield harness
    finally:
        harness.close()


@pytest.fixture
def make_daemon(tmp_path: Path, harness: Harness) -> Any:
    made: list[Daemon] = []

    def make(**options: Any) -> Daemon:
        root = tmp_path / f"daemon-{len(made)}"
        root.mkdir()
        daemon = Daemon(root, harness, **options)
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        daemon.close()


def _keys(pushes: list[dict[str, Any]]) -> list[list[str]]:
    return [push["keys"] for push in pushes]


def test_starting_the_sidecar_pushes_the_set_of_every_exposure(make_daemon: Any) -> None:
    """The daemon derives on start, and again each time the sidecar is installed."""

    daemon = make_daemon()
    assert daemon.app.handle(
        "service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug}
    )["grant"]["grantId"]

    daemon.start()

    pushes = daemon.wait_for_pushes(2, port=PORT)
    assert _keys(pushes[-1:]) == [[BOB_KEY]]


def test_a_grant_is_pushed_without_waiting_for_the_maintenance_tick(make_daemon: Any) -> None:
    daemon = make_daemon()
    daemon.start()
    before = len(daemon.exposes(port=PORT))

    daemon.app.handle("service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug})

    assert _keys(daemon.wait_for_pushes(before + 1, port=PORT)[-1:]) == [[BOB_KEY]]


def test_a_revoke_is_pushed_without_waiting_for_the_maintenance_tick(make_daemon: Any) -> None:
    daemon = make_daemon()
    daemon.start()
    grant = daemon.app.handle(
        "service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug}
    )["grant"]
    before = len(daemon.exposes(port=PORT))
    assert _keys(daemon.wait_for_pushes(before + 1, port=PORT)[-1:]) == [[BOB_KEY]]

    daemon.app.handle("service.revoke", {"org": ORG, "grantId": grant["grantId"]})

    assert _keys(daemon.wait_for_pushes(before + 2, port=PORT)[-1:]) == [[]]


def test_a_new_exposure_admits_nobody_and_a_grant_on_it_is_pushed(make_daemon: Any) -> None:
    """Brief goal 2 and P6: a port with no grant admits nobody from its first moment."""

    daemon = make_daemon()
    daemon.start()

    result = daemon.app.handle(
        "network.expose",
        {"port": OTHER_PORT, "target": f"tcp:127.0.0.1:{OTHER_PORT}", "proxyProtocol": "none"},
    )

    assert result == {
        "exposure": {
            "port": OTHER_PORT,
            "target": f"tcp:127.0.0.1:{OTHER_PORT}",
            "proxyProtocol": "none",
        }
    }
    pushes = daemon.wait_for_pushes(1, port=OTHER_PORT)
    assert [(push["admission"], push["keys"]) for push in pushes] == [("keys", [])]

    daemon.app.handle("service.grant", {"org": ORG, "port": OTHER_PORT, "device": CAROL_NODE.slug})

    assert _keys(daemon.wait_for_pushes(2, port=OTHER_PORT)[-1:]) == [[peer_key(CAROL_NODE, ALICE_NODE)]]


def test_unexpose_revokes_the_ports_grants_and_keeps_other_ports_grants(
    make_daemon: Any,
) -> None:
    """Brief goal 4: the unexpose path is the caller of revoke_all_for_port."""

    daemon = make_daemon()
    daemon.start()
    kept = daemon.app.handle(
        "service.grant", {"org": ORG, "port": OTHER_PORT, "device": BOB_NODE.slug}
    )["grant"]["grantId"]
    daemon.app.handle("network.expose", {
        "port": OTHER_PORT, "target": f"tcp:127.0.0.1:{OTHER_PORT}", "proxyProtocol": "none"
    })
    daemon.app.handle("service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug})

    assert daemon.app.handle("network.unexpose", {"port": PORT}) == {"port": PORT, "removed": True}

    granted = daemon.app.handle("service.grants", {"org": ORG})["granted"]
    assert [item["grantId"] for item in granted] == [kept]


def test_a_failing_revoke_during_unexpose_is_logged_and_the_unexpose_still_succeeds(
    make_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon()
    daemon.start()
    daemon.app.handle("service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug})
    from hyprial.daemon.impl.service_grants.service import ServiceGrantService

    def refused(self: ServiceGrantService, port: int) -> dict[str, Any]:
        raise OSError("directory unavailable")

    monkeypatch.setattr(ServiceGrantService, "revoke_all_for_port", refused)

    assert daemon.app.handle("network.unexpose", {"port": PORT}) == {"port": PORT, "removed": True}
    assert PORT not in [item["port"] for item in daemon.app.handle("network.exposures", {})["exposures"]]
    [(level, fields)] = daemon.logged("service.grant.unexpose_revoke_failed")
    assert level == "warn"
    assert (fields["port"], fields["org"], fields["code"]) == (PORT, "*", "OSError")


def test_a_granted_device_without_a_roster_key_is_reported_not_silently_dropped(
    make_daemon: Any,
) -> None:
    """Ruling 2: the operator can tell "granted, no key yet" from "nobody granted"."""

    daemon = make_daemon(members=(ALICE_NODE, CAROL_NODE))
    daemon.start()

    grant = daemon.app.handle(
        "service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug}
    )["grant"]

    deadline_reached = daemon.wait_for_pushes(2, port=PORT)
    assert deadline_reached[-1]["keys"] == []
    [(level, fields)] = daemon.logged(KEY_UNAVAILABLE)
    assert level == "warn"
    assert fields == {
        "org": ORG,
        "port": PORT,
        "grantId": grant["grantId"],
        "recipient": {"owner": BOB_NODE.owner, "deviceId": BOB_NODE.device_id},
        "reason": "member-not-in-roster",
    }
    assert BOB_KEY not in str(fields)
