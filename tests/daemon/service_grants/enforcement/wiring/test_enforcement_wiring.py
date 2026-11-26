"""The triggers: where the daemon re-derives a port's admission set.

Decision (service grant S4-py, 2026-11-26): brief goal 3 names four moments
at which the exposing device must push again, and goal 4 one more. Each test
here fakes nothing: it performs the operator action through the public
``handle`` path of a real ``DaemonApplication`` and reads what reached the
sidecar's wire. The first test pins the wiring itself, so that the others
cannot pass because nothing was ever pushed.

Two rules keep a test honest about cause and effect:

* every step ends by waiting for the admission cadence to finish the ticks
  it was offered, so a push is never misread as the answer to what came
  after it;
* a step's own push is read as "the last push once the cadence is idle",
  never as "the Nth push", because start-up offers more than one tick.

The maintenance loop is never started here. A push that follows an operation
can therefore only have come from that operation's own trigger, which is what
"without waiting for the maintenance tick" has to mean.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.service_grants.service import ServiceGrantService
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
from tests.daemon.service_grants.enforcement.wiring._app import KEY_UNAVAILABLE, Daemon

OTHER_PORT = PORT + 1
BOB_KEY = peer_key(BOB_NODE, ALICE_NODE)
CAROL_KEY = peer_key(CAROL_NODE, ALICE_NODE)
OTHER_EXPOSURE = {
    "port": OTHER_PORT,
    "target": f"tcp:127.0.0.1:{OTHER_PORT}",
    "proxyProtocol": "none",
}


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
        daemon.start()
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        daemon.close()


def _grant(daemon: Daemon, device: Any, *, port: int = PORT) -> dict[str, Any]:
    grant = daemon.app.handle("service.grant", {"org": ORG, "port": port, "device": device.slug})
    daemon.settle()
    return grant["grant"]


def _revoke(daemon: Daemon, grant_id: str) -> None:
    daemon.app.handle("service.revoke", {"org": ORG, "grantId": grant_id})
    daemon.settle()


def _expose(daemon: Daemon, exposure: dict[str, Any]) -> None:
    daemon.app.handle("network.expose", dict(exposure))
    daemon.settle()


def _last_keys(daemon: Daemon, port: int = PORT) -> list[str]:
    return daemon.exposes(port=port)[-1]["keys"]


def test_starting_the_sidecar_pushes_the_set_of_every_exposure(make_daemon: Any) -> None:
    """The daemon derives as soon as the sidecar is installed, not only on a later tick."""

    daemon = make_daemon()

    assert _last_keys(daemon) == []
    grant = _grant(daemon, BOB_NODE)

    assert _last_keys(daemon) == [BOB_KEY]
    assert grant["grantId"]


def test_a_grant_is_pushed_without_waiting_for_the_maintenance_tick(make_daemon: Any) -> None:
    daemon = make_daemon()

    _grant(daemon, BOB_NODE)

    assert _last_keys(daemon) == [BOB_KEY]


def test_a_revoke_is_pushed_without_waiting_for_the_maintenance_tick(make_daemon: Any) -> None:
    daemon = make_daemon()
    grant = _grant(daemon, BOB_NODE)
    assert _last_keys(daemon) == [BOB_KEY]

    _revoke(daemon, grant["grantId"])

    assert _last_keys(daemon) == []


def test_a_new_exposure_admits_nobody_and_a_grant_on_it_is_pushed(make_daemon: Any) -> None:
    """Brief goal 2 and P6: a port with no grant admits nobody from its first moment.

    The exposure is created with nobody admitted, and the derivation that
    follows its trigger arrives at nobody again, so the port's first two
    pushes carry the same closed set: one from the expose, one from the
    enforcer, which does not assume what another writer put on the sidecar.
    """

    daemon = make_daemon()
    assert daemon.exposes(port=OTHER_PORT) == []

    _expose(daemon, OTHER_EXPOSURE)

    pushes = daemon.exposes(port=OTHER_PORT)
    assert [(p["admission"], p["keys"]) for p in pushes] == [("keys", []), ("keys", [])]

    _grant(daemon, CAROL_NODE, port=OTHER_PORT)

    assert _last_keys(daemon, OTHER_PORT) == [CAROL_KEY]


def test_unexpose_revokes_the_ports_grants_and_keeps_other_ports_grants(
    make_daemon: Any,
) -> None:
    """Brief goal 4: the unexpose path is the caller of revoke_all_for_port."""

    daemon = make_daemon()
    _expose(daemon, OTHER_EXPOSURE)
    kept = _grant(daemon, BOB_NODE, port=OTHER_PORT)["grantId"]
    _grant(daemon, CAROL_NODE)

    assert daemon.app.handle("network.unexpose", {"port": PORT}) == {"port": PORT, "removed": True}

    granted = daemon.app.handle("service.grants", {"org": ORG})["granted"]
    assert [item["grantId"] for item in granted] == [kept]
    assert [item["port"] for item in daemon.app.handle("network.exposures", {})["exposures"]] == [
        OTHER_PORT
    ]


def test_a_failing_revoke_during_unexpose_is_logged_and_the_unexpose_still_succeeds(
    make_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon()
    _grant(daemon, BOB_NODE)

    def refused(self: ServiceGrantService, port: int) -> dict[str, Any]:
        raise OSError("directory unavailable")

    monkeypatch.setattr(ServiceGrantService, "revoke_all_for_port", refused)

    assert daemon.app.handle("network.unexpose", {"port": PORT}) == {"port": PORT, "removed": True}
    assert daemon.app.handle("network.exposures", {})["exposures"] == []
    [(level, fields)] = daemon.logged("service.grant.unexpose_revoke_failed")
    assert level == "warn"
    assert (fields["port"], fields["org"], fields["code"]) == (PORT, "*", "OSError")


def test_a_granted_device_without_a_roster_key_is_reported_not_silently_dropped(
    make_daemon: Any,
) -> None:
    """Ruling 2: the operator can tell "granted, no key yet" from "nobody granted"."""

    daemon = make_daemon(members=(ALICE_NODE, CAROL_NODE))
    assert daemon.logged(KEY_UNAVAILABLE) == []

    grant = _grant(daemon, BOB_NODE)

    assert _last_keys(daemon) == []
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


def test_an_unexpose_during_a_derivation_is_not_undone_by_its_push(make_daemon: Any) -> None:
    """A derivation that listed the port before an unexpose must not expose it again.

    The derivation is held between taking its exposure list and pushing, on
    the roster read that sits in that gap in a serving daemon. The unexpose
    is issued while it is held, and the derivation is released only after the
    unexpose has had time to finish if nothing orders the two. Afterwards the
    port's last command on the sidecar's wire has to be its unexpose: a later
    expose would leave the port open with no exposure record left to close it.
    """

    daemon = make_daemon()
    _grant(daemon, BOB_NODE)
    enforcer = daemon.app._service_admission_enforcer
    listed = threading.Event()
    release = threading.Event()

    def held_roster() -> Any:
        listed.set()
        assert release.wait(10.0)
        return daemon.supervisor.membership_request("membership-status", {}, timeout=5.0)

    enforcer._roster_reader = held_roster
    daemon.app._service_admission_changed()
    assert listed.wait(10.0)

    outcome: list[Any] = []
    unexposer = threading.Thread(
        target=lambda: outcome.append(daemon.app.handle("network.unexpose", {"port": PORT}))
    )
    unexposer.start()
    unexposer.join(0.5)
    release.set()
    unexposer.join(10.0)
    daemon.settle()

    assert outcome == [{"port": PORT, "removed": True}]
    assert daemon.wire_ops(PORT)[-1] == "unexpose"
    assert daemon.app.handle("network.exposures", {})["exposures"] == []
    assert daemon.app.handle("service.grants", {"org": ORG})["granted"] == []
