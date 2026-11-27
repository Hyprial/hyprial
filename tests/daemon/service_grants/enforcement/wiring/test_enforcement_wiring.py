"""The triggers: where the daemon re-derives a port's admission set.

Decision (service grant exposing side, 2026-11-26): there are four moments
at which the exposing device must push again, and one more on unexpose. Each test
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
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.service_grants.enforcement import wiring
from hyprial.daemon.impl.service_grants.service import ServiceGrantService
from hyprial.kernel import AdmissionResult
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

TRIGGER_FAILED = "service.grant.admission_trigger_failed"
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
    daemon.app._service_admission_changed("service.grant")
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


def _effective_grants(daemon: Daemon) -> list[str]:
    """The grant ids the durable directory still counts as effective."""

    return [item["grantId"] for item in daemon.app.handle("service.grants", {"org": ORG})["granted"]]


@pytest.mark.parametrize(
    "method", ["service.grant", "service.revoke"], ids=["grant", "revoke"]
)
@pytest.mark.parametrize(
    ("failure", "reason", "error_type"),
    [
        pytest.param("raises", "failed", "RuntimeError", id="the-submit-raises"),
        pytest.param(AdmissionResult.CLOSED, "unscheduled", None, id="the-cadence-is-closed"),
        pytest.param(AdmissionResult.OVERLOADED, "unscheduled", None, id="the-cadence-is-overloaded"),
    ],
)
def test_a_trigger_that_cannot_be_delivered_is_logged_and_the_grant_is_still_durable(
    make_daemon: Any, monkeypatch: pytest.MonkeyPatch, failure: Any, reason: str, error_type: str | None, method: str
) -> None:
    """Critic finding 1: an immediate trigger that does not reach the cadence is visible.

    ``service.grant`` and ``service.revoke`` have already written the document
    when they ask for a re-derivation. When that request cannot be delivered
    the write is not rolled back, and it used to be indistinguishable from a
    delivered one. It is one ``service.grant.admission_trigger_failed`` event
    that names the operation and the kind of failure, and the exception's
    class when one was raised; it carries nothing else, and no message of the
    exception. The write it followed is still on disk.
    """

    daemon = make_daemon()
    granted = _grant(daemon, BOB_NODE)["grantId"]
    cadence = daemon.app._service_admission_cadence

    def refuse(_observed_at_ms: int) -> Any:
        raise RuntimeError("effect lane is gone")

    if failure == "raises":
        monkeypatch.setattr(cadence, "submit", refuse)
    elif failure is None:
        daemon.app._service_admission_cadence = None
    else:
        monkeypatch.setattr(cadence, "submit", lambda _observed_at_ms: failure)
    monkeypatch.setattr(daemon, "settle", lambda timeout: None)

    params = (
        {"org": ORG, "port": PORT, "device": CAROL_NODE.slug}
        if method == "service.grant"
        else {"org": ORG, "grantId": granted}
    )
    answer = daemon.app.handle(method, params)

    [(level, fields)] = daemon.logged(TRIGGER_FAILED)
    assert level == "warn"
    expected = {"method": method, "reason": reason}
    if error_type is not None:
        expected["errorType"] = error_type
    assert fields == expected
    if method == "service.grant":
        assert answer["grant"]["grantId"]
        assert _effective_grants(daemon) == sorted([granted, answer["grant"]["grantId"]])
    else:
        assert answer == {"grantId": granted, "revoked": True}
        assert _effective_grants(daemon) == []


@pytest.mark.parametrize("method", ["service.grant", "service.revoke"], ids=["grant", "revoke"])
def test_a_trigger_whose_application_method_raised_names_the_exception(
    make_daemon: Any, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """The handler's own report, for an application that cannot run its trigger at all.

    The handler reaches the application's method through ``getattr``, because
    a host without admission enforcement has none. One that has it and
    raises is reported by the handler itself, with the same event the
    application would have logged: the operation, the reason, and the class
    of what was raised and never its message. The grant is written before
    the trigger, so the write stands.
    """

    daemon = make_daemon()
    granted = _grant(daemon, BOB_NODE)["grantId"]

    def broken(_method: str) -> Any:
        raise KeyError("a message the event must not carry")

    monkeypatch.setattr(daemon.app, "_service_admission_changed", broken)
    monkeypatch.setattr(daemon, "settle", lambda timeout: None)
    daemon.events.clear()

    params = (
        {"org": ORG, "port": PORT, "device": CAROL_NODE.slug}
        if method == "service.grant"
        else {"org": ORG, "grantId": granted}
    )
    answer = daemon.app.handle(method, params)

    [(level, fields)] = daemon.logged(TRIGGER_FAILED)
    assert level == "warn"
    assert fields == {"method": method, "reason": "failed", "errorType": "KeyError"}
    if method == "service.grant":
        assert _effective_grants(daemon) == sorted([granted, answer["grant"]["grantId"]])
    else:
        assert answer == {"grantId": granted, "revoked": True}
        assert _effective_grants(daemon) == []


def test_a_new_exposure_whose_trigger_cannot_be_delivered_is_logged_under_its_own_name(
    make_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exposure path's trigger is reported as the exposure's, not as a grant's.

    More than one path asks for the same re-derivation. The event's
    ``method`` says which one could not be delivered, so an operator reading
    it does not go looking for a grant that was never made. The exposure
    itself stands: it is registered and admits nobody, as a new exposure
    must.
    """

    daemon = make_daemon()
    monkeypatch.setattr(
        daemon.app._service_admission_cadence, "submit", lambda _observed_at_ms: AdmissionResult.CLOSED
    )
    monkeypatch.setattr(daemon, "settle", lambda timeout: None)

    answer = daemon.app.handle("network.expose", dict(OTHER_EXPOSURE))

    assert answer["exposure"]["port"] == OTHER_PORT
    assert OTHER_PORT in {
        exposure["port"] for exposure in daemon.app.handle("network.exposures", {})["exposures"]
    }
    assert daemon.logged(TRIGGER_FAILED) == [
        ("warn", {"method": "network.expose", "reason": "unscheduled"})
    ]


def test_service_start_installs_admission_after_late_orgfs_setup(
    tmp_path: Path, harness: Harness
) -> None:
    root = tmp_path / "daemon-late-orgfs"
    root.mkdir()
    daemon = Daemon(root, harness)
    daemon.app._orgfs_runtime = None
    try:
        daemon.app._start_forwarding_supervisor()
        assert getattr(daemon.app, "_service_admission_cadence", None) is None

        daemon.app._orgfs_runtime = harness.runtime
        daemon.app._start_service_connect()
        daemon.settle()

        assert daemon.app._service_admission_cadence is not None
        assert _last_keys(daemon) == []
    finally:
        daemon.close()


def test_lazy_forwarding_start_installs_admission_for_first_exposure(
    tmp_path: Path, harness: Harness
) -> None:
    root = tmp_path / "daemon-lazy-forwarding"
    root.mkdir()
    daemon = Daemon(root, harness)
    daemon.app._forwarding_supervisor = None
    daemon.app._forwarding_start_attempted = False
    try:
        daemon.app.handle("network.expose", dict(OTHER_EXPOSURE))
        daemon.settle()

        assert getattr(daemon.app, "_service_admission_cadence", None) is not None
        assert _last_keys(daemon, OTHER_PORT) == []
        _grant(daemon, CAROL_NODE, port=OTHER_PORT)
        assert _last_keys(daemon, OTHER_PORT) == [CAROL_KEY]
    finally:
        daemon.close()


def test_rebinding_forwarding_reinstalls_admission_on_new_supervisor(
    tmp_path: Path, harness: Harness
) -> None:
    root = tmp_path / "daemon-rebind-forwarding"
    root.mkdir()
    daemon = Daemon(root, harness)
    daemon.start()
    try:
        _grant(daemon, BOB_NODE)
        setattr(
            daemon.app,
            "_forwarding_automatic",
            SimpleNamespace(
                environment=lambda _target: dict(daemon.app._forwarding_environment)
            ),
        )
        daemon.app._rebind_forwarding_endpoint(
            f"unixsock-stream/{root / 'rebound.sock'}"
        )
        daemon.settle()

        assert daemon.app._service_admission_cadence is not None
        assert _last_keys(daemon) == [BOB_KEY]
    finally:
        daemon.close()


def test_the_start_up_trigger_names_the_sidecar_installation(
    tmp_path: Path, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first trigger of all is the sidecar's installation, and it is named so.

    The sidecar comes up with every exposure admitting nobody, and the hook
    that reports it is what asks for the first derivation. Its tick is
    refused here for the whole start-up, so the one refusal is logged as
    the installation's own. The cadence is created inside the start-up and
    only then can a tick be refused, so the daemon is started here and not
    by the fixture, with the refusal placed before the first tick is offered.
    """

    root = tmp_path / "daemon-start-up"
    root.mkdir()
    daemon = Daemon(root, harness)
    offer_tick = wiring.offer_tick
    monkeypatch.setattr(wiring, "offer_tick", lambda app: False)
    try:
        daemon.start()
    finally:
        monkeypatch.setattr(wiring, "offer_tick", offer_tick)
    try:
        assert daemon.logged(TRIGGER_FAILED) == [
            ("warn", {"method": "sidecar-installed", "reason": "unscheduled"})
        ]
    finally:
        daemon.close()
