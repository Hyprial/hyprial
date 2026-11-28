"""End to end: grants become the admission set the sidecar enforces.

Decision (service grant exposing side, 2026-11-26): these cases drive the real
``ForwardingSidecarSupervisor`` and the real controller wire against the
stdio fixture with a roster, so what is asserted is what left the daemon on
the ``expose`` command and whether a roster member's connection is admitted.
Grants are written by the real ``ServiceGrantService`` over a real OrgFS
runtime; device rows by the real publish path. The roster is the one test
input, because in production it comes from the sidecar. It changes only
through the fixture's reload probe, which stands in for the sidecar's own
roster refresh. Every dependency of the enforcer is injected at construction,
so a failing dependency is a different enforcer, never a mutated one.

The derivation-lock and unexpose-wait cases (exposing side fix, 2026-11-27)
share one enforcer object between the slow derivation and the
unexpose in every case, because what orders the two is that object's own
state; a second enforcer has a second lock and would prove nothing. A
derivation is held on one step through an injected reader or a wrapped
supervisor call, so a wait is measured against that step and not against a
scheduler. The unexpose goes through the wiring's own function over an
application that carries only the enforcer.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.forwarding import (
    ForwardingSidecarController,
    ForwardingSidecarError,
    ForwardingSidecarSupervisor,
)
from hyprial.daemon.impl.service_grants.enforcement import ServiceAdmissionEnforcer
from hyprial.daemon.impl.service_grants.enforcement.wiring import unexpose_port
from tests.daemon.service_grants.enforcement._crowd import crowd_grants, crowd_roster
from tests.daemon.service_grants.enforcement._support import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    FIXTURE,
    MEMBERS,
    ORG,
    PORT,
    Harness,
    peer_key,
    roster_document,
)

OTHER_PORT = PORT + 1
BOB_KEY = peer_key(BOB_NODE, ALICE_NODE)
CAROL_KEY = peer_key(CAROL_NODE, ALICE_NODE)
KEY_UNAVAILABLE = "service.grant.key_unavailable"
LIMIT_EVENT = "service.grant.admission_limit"
UNEXPOSE_FAILED = "service.grant.unexpose_revoke_failed"


class _Clock:
    """A manual monotonic clock: a test advances it instead of waiting."""

    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _up(root: Path) -> dict[str, Any]:
    return {
        "v": 7,
        "requestId": "up-fixture",
        "op": "up",
        "keyFile": str(root / "device-key.json"),
        "membershipStore": str(root / "membership.json"),
        "owner": "fixture",
        "deviceId": "fixture",
        "controlPort": 7448,
        "addressFile": str(root / "address"),
        "inboundTarget": "tcp:127.0.0.1:0",
        "peerPort": 7447,
        "slots": [],
    }


class Rig:
    """A real supervisor over the fixture, plus the enforcer under test."""

    def __init__(self, root: Path, harness: Harness, scheduler: Any, **roster: Any) -> None:
        self.root = root
        self.harness = harness
        self.receipt = root / "receipt.jsonl"
        self.roster_path = root / "roster.json"
        self.clock = _Clock()
        self.roster_reads = 0
        self.controllers: list[ForwardingSidecarController] = []
        self.installed: Callable[[], None] = lambda: None
        self.write_roster(**roster)
        self.supervisor = ForwardingSidecarSupervisor(
            {"FAKE_FORWARD_RECEIPT": str(self.receipt), "FAKE_FORWARD_ROSTER": str(self.roster_path)},
            event_log=lambda *_args, **_fields: None,
            controller_factory=self._controller,
            scheduler=scheduler,
            desired_exposures=harness.exposures.list,
            on_installed=lambda: self.installed(),
        )
        self.supervisor.ensure_started()

    def _controller(self, environ: Any, on_exit: Any) -> ForwardingSidecarController:
        controller = ForwardingSidecarController(
            (sys.executable, str(FIXTURE)),
            _up(self.root),
            environ=environ,
            timeout=10.0,
            on_exit=on_exit,
        )
        self.controllers.append(controller)
        return controller

    @property
    def controller(self) -> ForwardingSidecarController:
        return self.controllers[-1]

    def build(self, **overrides: Any) -> ServiceAdmissionEnforcer:
        """A new enforcer with empty memory; ``overrides`` replace a dependency."""

        options: dict[str, Any] = {
            "supervisor": self.supervisor,
            "exposure_store": self.harness.exposures,
            "store": self.harness.store,
            "fs": self.harness.fs,
            "home": self.harness.homes[ALICE_NODE.owner],
            "owner": ALICE_NODE.owner,
            "device_id": ALICE_NODE.device_id,
            "grants_service": self.harness.grants,
            "roster_reader": self.roster,
            "now_ms": lambda: self.harness.clock[0],
            "clock": self.clock,
            "logger": self.harness.log,
            "interval_seconds": 0.0,
            "membership_timeout": 5.0,
        }
        options.update(overrides)
        return ServiceAdmissionEnforcer(**options)

    def write_roster(self, *, members: tuple = MEMBERS, without_server_key: tuple = ()) -> None:
        document = roster_document(members, without_server_key=without_server_key)
        document["tailnetId"] = self.harness.space_id
        self.roster_path.write_text(json.dumps(document), encoding="utf-8")

    def roster(self) -> dict[str, Any] | None:
        self.roster_reads += 1
        return self.supervisor.membership_request("membership-status", {}, timeout=5.0)

    def reload_roster(self) -> None:
        self.controller._request(
            {"v": 7, "op": "fixture-reload-roster"}, "fixture-roster-reloaded"
        )

    def exposes(self, *, port: int | None = None) -> list[dict[str, Any]]:
        commands = [json.loads(line) for line in self.receipt.read_text().splitlines()]
        return [
            command
            for command in commands
            if command["op"] == "expose" and (port is None or command["port"] == port)
        ]

    def connect(self, member: Any, *, port: int = PORT) -> dict[str, Any]:
        return self.controller._request(
            {"v": 7, "op": "fixture-connect", "port": port, "device": member.device_id},
            "fixture-connected",
        )

    def logged(self, event: str) -> list[tuple[str, dict[str, Any]]]:
        return [(level, fields) for level, _c, name, fields in self.harness.events if name == event]

    def add_exposure(self, port: int) -> None:
        self.harness.exposures.set(
            {"port": port, "target": f"tcp:127.0.0.1:{port}", "proxyProtocol": "none"}
        )

    def grant(self, recipient: Any, *, port: int = PORT, **options: Any) -> dict[str, Any]:
        return self.harness.grants().grant(org=ORG, port=port, device=recipient, **options)[
            "grant"
        ]

    def close(self) -> None:
        self.supervisor.close()


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
def make_rig(tmp_path: Path, harness: Harness, generation_scheduler: Any) -> Any:
    made: list[Rig] = []

    def make(**roster: Any) -> Rig:
        root = tmp_path / f"rig-{len(made)}"
        root.mkdir()
        rig = Rig(root, harness, generation_scheduler, **roster)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.close()


def test_the_rig_drives_a_real_supervisor_over_the_fixture(make_rig: Any) -> None:
    """The rig itself: a running sidecar whose roster the real wire can read."""

    rig = make_rig()
    status = rig.roster()

    assert rig.supervisor.state == "running"
    assert [row["deviceId"] for row in status["tailnets"][0]["members"]] == [
        member.device_id for member in MEMBERS
    ]
    assert status["tailnets"][0]["tailnetId"] == rig.harness.space_id


def test_an_exposure_without_grants_admits_nobody_and_says_nothing(make_rig: Any) -> None:
    rig = make_rig()

    rig.build().apply_now()

    pushes = rig.exposes(port=PORT)
    assert [(push["port"], push["admission"], push["keys"]) for push in pushes]
    assert all(
        (push["port"], push["admission"], push["keys"]) == (PORT, "keys", []) for push in pushes
    )
    assert rig.connect(BOB_NODE)["admitted"] is False
    assert rig.logged(KEY_UNAVAILABLE) == []


def test_a_grant_is_pushed_with_the_recipients_roster_key_and_admits_only_that_recipient(
    make_rig: Any,
) -> None:
    rig = make_rig()
    enforcer = rig.build()
    enforcer.apply_now()

    rig.grant(BOB_NODE.slug)
    enforcer.apply_now()

    push = rig.exposes()[-1]
    assert {key: push[key] for key in ("port", "target", "proxyProtocol", "admission", "keys")} == {
        "port": PORT,
        "target": f"tcp:127.0.0.1:{PORT}",
        "proxyProtocol": "",
        "admission": "keys",
        "keys": [BOB_KEY],
    }
    assert rig.connect(BOB_NODE)["admitted"] is True
    assert rig.connect(CAROL_NODE)["admitted"] is False


def test_revoking_the_grant_removes_the_key_and_closes_the_recipients_connection(
    make_rig: Any,
) -> None:
    rig = make_rig()
    enforcer = rig.build()
    grant_id = rig.grant(BOB_NODE.slug)["grantId"]
    enforcer.apply_now()
    assert rig.connect(BOB_NODE)["established"] == [BOB_NODE.device_id]

    rig.harness.grants().revoke(org=ORG, grant_id=grant_id)
    enforcer.apply_now()

    assert rig.exposes()[-1]["keys"] == []
    assert rig.connect(BOB_NODE)["established"] == []
    assert rig.connect(BOB_NODE)["admitted"] is False


def test_an_unchanged_set_is_not_resent(make_rig: Any) -> None:
    rig = make_rig()
    enforcer = rig.build()
    rig.grant(BOB_NODE.slug)
    enforcer.apply_now()
    pushes = len(rig.exposes())

    enforcer.apply_now()
    enforcer.reconcile()

    assert len(rig.exposes()) == pushes


def test_the_periodic_tick_picks_up_expiry(make_rig: Any) -> None:
    rig = make_rig()
    enforcer = rig.build()
    rig.grant(BOB_NODE.slug, expires_at_ms=rig.harness.clock[0] + 1)
    enforcer.apply_now()
    assert rig.exposes()[-1]["keys"] == [BOB_KEY]

    rig.harness.clock[0] += 1
    enforcer.reconcile()

    assert rig.exposes()[-1]["keys"] == []


def test_a_device_added_to_the_roster_later_is_admitted_on_a_later_tick(make_rig: Any) -> None:
    """Ruling 3: the roster completes on the sidecar's refresh; a later tick adds it."""

    rig = make_rig(without_server_key=(BOB_NODE,))
    enforcer = rig.build()
    rig.grant(BOB_NODE.slug)
    enforcer.apply_now()
    assert rig.exposes()[-1]["keys"] == []
    assert rig.connect(BOB_NODE)["admitted"] is False
    assert [(level, fields["reason"]) for level, fields in rig.logged(KEY_UNAVAILABLE)] == [
        ("warn", "member-without-local-key")
    ]

    rig.write_roster()
    rig.reload_roster()
    enforcer.reconcile()

    assert rig.exposes()[-1]["keys"] == [BOB_KEY]
    assert rig.connect(BOB_NODE)["admitted"] is True


def test_a_device_removed_from_the_org_stops_being_admitted_on_a_later_tick(make_rig: Any) -> None:
    """Ruling 5: a grant document alone no longer admits once the member is gone."""

    rig = make_rig()
    enforcer = rig.build()
    grant_id = rig.grant(BOB_NODE.slug)["grantId"]
    enforcer.apply_now()
    assert rig.connect(BOB_NODE)["admitted"] is True

    rig.write_roster(members=(ALICE_NODE, CAROL_NODE))
    rig.reload_roster()
    enforcer.reconcile()

    assert rig.exposes()[-1]["keys"] == []
    assert [item["grantId"] for item in rig.harness.grants().grants(org=ORG)["granted"]] == [
        grant_id
    ]
    assert rig.connect(BOB_NODE)["reason"] == "not-a-member"


def test_a_failed_roster_read_tightens_the_port_and_is_logged(make_rig: Any) -> None:
    """Ruling 2: fail closed, and report it as "granted, key unavailable"."""

    def unavailable() -> Any:
        raise TimeoutError("roster read timed out")

    rig = make_rig()
    grant_id = rig.grant(BOB_NODE.slug)["grantId"]
    rig.build().apply_now()
    assert rig.exposes()[-1]["keys"] == [BOB_KEY]

    rig.build(roster_reader=unavailable).apply_now()

    assert rig.exposes()[-1]["keys"] == []
    [(level, fields)] = rig.logged(KEY_UNAVAILABLE)
    assert level == "warn"
    assert fields["grantId"] == grant_id
    assert fields["reason"] == "roster-unavailable"


def test_a_roster_read_that_answers_nothing_tightens_the_port(make_rig: Any) -> None:
    def empty() -> dict[str, Any]:
        return {"readiness": {}, "tailnets": []}

    rig = make_rig()
    rig.grant(BOB_NODE.slug)

    rig.build(roster_reader=empty).apply_now()

    assert rig.exposes()[-1]["keys"] == []
    [(level, fields)] = rig.logged(KEY_UNAVAILABLE)
    assert fields["reason"] == "roster-unavailable"


def test_the_tick_reads_the_roster_once_however_many_ports_and_grants(make_rig: Any) -> None:
    """Ruling 4: one bounded read serves every port and every org in a tick."""

    rig = make_rig()
    rig.add_exposure(OTHER_PORT)
    rig.grant(BOB_NODE.slug)
    rig.grant("@members", port=OTHER_PORT)
    rig.grant(CAROL_NODE.slug, port=OTHER_PORT)

    rig.build().apply_now()

    assert rig.roster_reads == 1
    assert {push["port"]: push["admission"] for push in rig.exposes()} == {
        PORT: "keys",
        OTHER_PORT: "keys",
    }
    assert rig.exposes(port=PORT)[-1]["keys"] == [BOB_KEY]


def test_a_device_with_no_exposure_reads_nothing_and_pushes_nothing(make_rig: Any) -> None:
    """Without a user exposure there is no set to push, so nothing is read for one.

    A sidecar start offers the first derivation, and most devices expose
    nothing; that derivation must not put a roster read on the wire of every
    device, nor log about grants that no exposure would admit through.
    """

    rig = make_rig()
    rig.grant(BOB_NODE.slug)
    rig.harness.exposures.remove(PORT)
    pushed_before = len(rig.exposes())

    rig.build().apply_now()

    assert rig.roster_reads == 0
    assert len(rig.exposes()) == pushed_before
    assert rig.logged("service.grant.key_unavailable") == []


def test_the_periodic_tick_waits_for_its_interval_and_the_immediate_push_does_not(
    make_rig: Any,
) -> None:
    interval = 30.0
    rig = make_rig()
    periodic = rig.build(interval_seconds=interval)
    immediate = rig.build(interval_seconds=interval)

    periodic.reconcile()
    rig.clock.advance(interval / 2)
    periodic.reconcile()
    assert rig.roster_reads == 1
    periodic.reconcile()
    assert rig.roster_reads == 1

    rig.clock.advance(interval)
    periodic.reconcile()
    assert rig.roster_reads == 2

    immediate.apply_now()
    immediate.apply_now()
    assert rig.roster_reads == 4


def test_an_object_with_empty_memory_derives_the_same_set(make_rig: Any) -> None:
    """The set is never stored: a new object derives it again from the same inputs."""

    rig = make_rig()
    rig.grant(BOB_NODE.slug)
    rig.build().apply_now()
    first = rig.exposes(port=PORT)[-1]["keys"]

    rig.build().apply_now()

    assert rig.exposes(port=PORT)[-1]["keys"] == first == [BOB_KEY]


def test_a_relaunched_sidecar_is_given_the_set_again(make_rig: Any) -> None:
    """A relaunched sidecar replays the port with nobody admitted, then gets the set.

    What was pushed before belongs to the sidecar that is gone. The same set
    is pushed again rather than skipped as unchanged, or the granted device
    would stay denied until its grant changed.
    """

    rig = make_rig()
    rig.grant(BOB_NODE.slug)
    enforcer = rig.build()
    enforcer.apply_now()
    rig.installed = enforcer.apply_now
    before = len(rig.exposes(port=PORT))

    rig.supervisor.restart(reason="test")

    pushes = rig.exposes(port=PORT)[before:]
    assert rig.supervisor.state == "running"
    assert [(push["admission"], push["keys"]) for push in pushes] == [("keys", []), ("keys", [BOB_KEY])]
    assert rig.connect(BOB_NODE)["admitted"] is True


def test_a_port_with_no_grant_stays_closed_across_a_relaunch(make_rig: Any) -> None:
    """The replay alone leaves nobody admitted; the push after it changes nothing."""

    rig = make_rig()
    enforcer = rig.build()
    enforcer.apply_now()
    rig.installed = enforcer.apply_now
    before = len(rig.exposes(port=PORT))

    rig.supervisor.restart(reason="test")

    pushes = rig.exposes(port=PORT)[before:]
    assert [(push["admission"], push["keys"]) for push in pushes] == [("keys", []), ("keys", [])]
    assert rig.connect(BOB_NODE)["admitted"] is False


def test_an_unresolvable_grant_is_logged_with_its_identity_and_reason_but_no_key(
    make_rig: Any,
) -> None:
    """Ruling 2: the operator can tell "granted, no key yet" from "nobody granted"."""

    rig = make_rig(members=(ALICE_NODE, CAROL_NODE))
    grant = rig.grant(BOB_NODE.slug)

    rig.build().apply_now()

    [(level, fields)] = rig.logged(KEY_UNAVAILABLE)
    assert level == "warn"
    assert fields == {
        "org": ORG,
        "port": PORT,
        "grantId": grant["grantId"],
        "recipient": {"owner": BOB_NODE.owner, "deviceId": BOB_NODE.device_id},
        "reason": "member-not-in-roster",
    }
    assert BOB_KEY not in json.dumps(fields)


def test_a_broken_grant_document_contributes_nothing_and_hides_no_other_grant(
    make_rig: Any,
) -> None:
    """The grant reader skips one bad document; the other grant on the port still pushes."""

    rig = make_rig()
    broken = rig.grant(BOB_NODE.slug)["grantId"]
    rig.grant(CAROL_NODE.slug)
    path = f"directory/service-grants/{ALICE_NODE.owner}/{broken}.json"
    rig.harness.fs.write_text(rig.harness.space_id, path, "{not json")

    rig.build().apply_now()

    assert rig.exposes(port=PORT)[-1]["keys"] == [CAROL_KEY]
    assert rig.logged(KEY_UNAVAILABLE) == []


def test_a_set_over_the_sidecar_limit_is_not_pushed_and_the_port_keeps_admitting_the_last_set(
    make_rig: Any,
) -> None:
    """Brief goal 2: over 4096 keys nothing is widened; the last set stays in force."""

    rig = make_rig()
    rig.grant(BOB_NODE.slug)
    rig.build().apply_now()
    pushes = len(rig.exposes(port=PORT))
    assert rig.connect(BOB_NODE)["admitted"] is True

    over = rig.build(
        grants_reader=lambda: crowd_grants(PORT, 4097),
        roster_reader=lambda: crowd_roster(4097, rig.harness.space_id),
    )
    over.apply_now()

    assert len(rig.exposes(port=PORT)) == pushes
    assert rig.connect(BOB_NODE)["admitted"] is True
    assert [(level, fields) for level, fields in rig.logged(LIMIT_EVENT)] == [
        ("warn", {"port": PORT, "count": 4097, "limit": 4096})
    ]


def test_a_set_at_the_sidecar_limit_is_pushed(make_rig: Any) -> None:
    rig = make_rig()

    rig.build(
        grants_reader=lambda: crowd_grants(PORT, 4096),
        roster_reader=lambda: crowd_roster(4096, rig.harness.space_id),
    ).apply_now()

    assert rig.exposes(port=PORT)[-1]["admission"] == "keys"
    assert len(rig.exposes(port=PORT)[-1]["keys"]) == 4096
    assert rig.logged(LIMIT_EVENT) == []


def test_unexpose_revokes_every_grant_for_the_port_and_leaves_other_ports(make_rig: Any) -> None:
    rig = make_rig()
    rig.add_exposure(OTHER_PORT)
    kept = rig.grant("@members", port=OTHER_PORT)["grantId"]
    to_bob = rig.grant(BOB_NODE.slug)["grantId"]
    to_all = rig.grant("@members")["grantId"]
    enforcer = rig.build()
    enforcer.apply_now()

    result = enforcer.forget_port(PORT)

    assert result["port"] == PORT
    assert result["failed"] == []
    assert sorted(item["grantId"] for item in result["revoked"]) == sorted([to_bob, to_all])
    assert {item["org"] for item in result["revoked"]} == {ORG}
    assert [item["grantId"] for item in rig.harness.grants().grants(org=ORG)["granted"]] == [kept]
    assert enforcer.last_set(PORT) is None
    assert enforcer.last_set(OTHER_PORT) == "keys"


def test_a_revoke_that_fails_during_unexpose_is_logged_and_does_not_raise(make_rig: Any) -> None:
    """Brief goal 4: a failing revocation never fails the unexpose, and is logged."""

    def refused(port: int) -> dict[str, Any]:
        raise OSError("directory unavailable")

    rig = make_rig()
    rig.grant(BOB_NODE.slug)
    enforcer = rig.build()
    enforcer.apply_now()

    result = rig.build(revoke_all_for_port=refused).forget_port(PORT)

    assert result == {"port": PORT, "revoked": [], "failed": [{"org": "*", "code": "OSError"}]}
    [(level, fields)] = rig.logged(UNEXPOSE_FAILED)
    assert (level, fields) == ("warn", {"port": PORT, "org": "*", "code": "OSError"})
    assert enforcer.last_set(PORT) == "keys"


THIRD_PORT = PORT + 2
WAIT_BOUND = 1.0


class _Held:
    """A step of one derivation that blocks until the test releases it.

    As a roster reader it answers with the sidecar's real roster once
    released, so the derivation it holds is delayed and not also starved.
    """

    def __init__(self, answer: Any = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._answer = answer

    def __call__(self) -> Any:
        self.entered.set()
        assert self.release.wait(20.0)
        return self._answer()


class _Derivation:
    """One derivation on its own thread, held on one step until released."""

    def __init__(self, rig: Rig, *, step: str) -> None:
        self.rig = rig
        self.worker: threading.Thread | None = None
        self._expose = rig.supervisor.expose
        if step == "roster":
            self.held: _Held = _Held(rig.roster)
            self.reader: Any = self.held
        else:
            self.held = _Held()
            self.reader = rig.roster
            rig.supervisor.expose = self._held_expose

    def _held_expose(self, exposure: Any, **admission: Any) -> Any:
        if exposure["port"] == PORT:
            self.held.entered.set()
            assert self.held.release.wait(20.0)
        return self._expose(exposure, **admission)

    def start(self, enforcer: ServiceAdmissionEnforcer) -> None:
        enforcer._roster_reader = self.reader
        self.worker = threading.Thread(target=enforcer.apply_now, daemon=True)
        self.worker.start()
        assert self.held.entered.wait(10.0)

    def finish(self) -> None:
        self.held.release.set()
        assert self.worker is not None
        self.worker.join(20.0)
        assert not self.worker.is_alive()
        self.rig.supervisor.expose = self._expose


def _unexpose(rig: Rig, enforcer: ServiceAdmissionEnforcer, port: int) -> tuple[threading.Thread, list[Any]]:
    """Start the wiring's unexpose of ``port`` on its own thread."""

    app = SimpleNamespace(_service_admission_enforcer=enforcer)
    outcome: list[Any] = []

    def remove() -> None:
        rig.supervisor.unexpose(port)
        rig.harness.exposures.remove(port)

    def run() -> None:
        try:
            outcome.append(unexpose_port(app, port, remove))
        except BaseException as error:  # noqa: BLE001 - the test reads the failure
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _done_within(thread: threading.Thread, seconds: float) -> bool:
    thread.join(seconds)
    return not thread.is_alive()


def _exposed_ports(rig: Rig) -> set[int]:
    return {int(item["port"]) for item in rig.harness.exposures.list()}


def _ops(rig: Rig, port: int) -> list[str]:
    """The expose and unexpose commands for ``port``, in wire order."""

    commands = [json.loads(line) for line in rig.receipt.read_text().splitlines()]
    return [
        command["op"]
        for command in commands
        if command["op"] in {"expose", "unexpose"} and command.get("port") == port
    ]


def _granted(rig: Rig) -> list[str]:
    return [item["grantId"] for item in rig.harness.grants().grants(org=ORG)["granted"]]


@pytest.mark.parametrize("step", ["roster", "push"])
def test_an_unexpose_of_another_port_waits_for_no_step_of_a_derivation(
    make_rig: Any, step: str
) -> None:
    """Critic finding 2: nothing a derivation does is another port's unexpose's business.

    A maintenance tick over several ports spends its roster budget and then
    one push per changed port. The unexpose of a port that is in neither used
    to queue behind the whole derivation, because the derivation held one lock
    from its first read to its last push.
    """

    rig = make_rig()
    rig.add_exposure(OTHER_PORT)
    rig.add_exposure(THIRD_PORT)
    enforcer = rig.build(interval_seconds=0.0)
    enforcer.apply_now()
    rig.grant(BOB_NODE.slug)
    rig.grant(CAROL_NODE.slug, port=OTHER_PORT)
    derivation = _Derivation(rig, step=step)
    derivation.start(enforcer)
    try:
        unexposer, outcome = _unexpose(rig, enforcer, THIRD_PORT)

        assert _done_within(unexposer, WAIT_BOUND), outcome
    finally:
        derivation.finish()

    assert outcome == [None]
    assert THIRD_PORT not in _exposed_ports(rig)
    assert _ops(rig, THIRD_PORT)[-1] == "unexpose"
    assert _ops(rig, PORT)[-1] == "expose"
    assert rig.exposes(port=PORT)[-1]["keys"] == [BOB_KEY]


def test_an_unexpose_waits_for_the_push_of_its_own_port_and_that_wait_is_ordered(
    make_rig: Any,
) -> None:
    """The one wait an unexpose has is the push of its own port, and it ends after it.

    A derivation that is already pushing the port is let to finish, so that
    the unexpose is the port's last command on the wire. Before the release
    the unexpose is still waiting; after it the unexpose follows the push.
    """

    rig = make_rig()
    rig.add_exposure(OTHER_PORT)
    enforcer = rig.build(interval_seconds=0.0)
    enforcer.apply_now()
    rig.grant(BOB_NODE.slug)
    kept = rig.grant(CAROL_NODE.slug, port=OTHER_PORT)["grantId"]
    derivation = _Derivation(rig, step="push")
    derivation.start(enforcer)
    unexposer, outcome = _unexpose(rig, enforcer, PORT)
    waiting = not _done_within(unexposer, 0.3)
    derivation.held.release.set()

    assert waiting
    assert _done_within(unexposer, WAIT_BOUND), outcome
    derivation.finish()
    assert outcome == [None]
    assert _ops(rig, PORT)[-2:] == ["expose", "unexpose"]
    assert PORT not in _exposed_ports(rig)
    assert _granted(rig) == [kept]


def test_an_unexpose_that_cannot_get_its_port_within_the_bound_fails_and_nothing_moves(
    make_rig: Any,
) -> None:
    """At the bound the three states agree: the unexpose fails and changes nothing.

    The push of the port is held past the bound. The unexpose then fails with
    the forwarding error the exposure path already maps, and what it would
    have changed is exactly what it left alone: the exposure is still desired,
    the sidecar still carries it, and its grants are still effective.
    """

    rig = make_rig()
    enforcer = rig.build(interval_seconds=0.0, unexpose_wait_seconds=0.3)
    enforcer.apply_now()
    grant_id = rig.grant(BOB_NODE.slug)["grantId"]
    assert rig.connect(BOB_NODE)["admitted"] is False
    before = (
        _exposed_ports(rig),
        _ops(rig, PORT),
        _granted(rig),
        rig.connect(BOB_NODE)["admitted"],
        enforcer.last_set(PORT),
    )
    derivation = _Derivation(rig, step="push")
    derivation.start(enforcer)
    unexposer, outcome = _unexpose(rig, enforcer, PORT)

    assert _done_within(unexposer, WAIT_BOUND), "the unexpose outlived its bound"
    [error] = outcome
    after = (
        _exposed_ports(rig),
        _ops(rig, PORT),
        _granted(rig),
        rig.connect(BOB_NODE)["admitted"],
        enforcer.last_set(PORT),
    )
    assert after == before
    assert isinstance(error, ForwardingSidecarError)
    assert error.code == "FORWARD_PROTOCOL"
    assert _granted(rig) == [grant_id]
    derivation.finish()
    assert rig.connect(BOB_NODE)["admitted"] is True


def test_a_derivation_that_listed_a_port_before_its_unexpose_pushes_nothing_for_it(
    make_rig: Any,
) -> None:
    """The exposure snapshot carries a generation, and an unexpose makes it stale.

    The derivation takes its list and is held on the roster read. The unexpose
    of a port it listed completes meanwhile. The stale derivation must not
    expose that port again, or the port would be open with no record to close
    it; its other port still gets its set on the derivation that follows.
    """

    rig = make_rig()
    rig.add_exposure(OTHER_PORT)
    enforcer = rig.build(interval_seconds=0.0)
    enforcer.apply_now()
    rig.grant(BOB_NODE.slug)
    rig.grant(CAROL_NODE.slug, port=OTHER_PORT)
    pushes_before = len(rig.exposes(port=OTHER_PORT))
    derivation = _Derivation(rig, step="roster")
    derivation.start(enforcer)
    unexposer, outcome = _unexpose(rig, enforcer, OTHER_PORT)
    assert _done_within(unexposer, WAIT_BOUND), outcome
    derivation.finish()

    assert outcome == [None]
    assert _ops(rig, OTHER_PORT)[-1] == "unexpose"
    assert len(rig.exposes(port=OTHER_PORT)) == pushes_before
    assert OTHER_PORT not in _exposed_ports(rig)
    assert enforcer.last_set(PORT) == "keys"


def test_a_port_exposed_again_after_its_unexpose_gets_its_set_from_a_later_derivation(
    make_rig: Any,
) -> None:
    """A derivation that lists a port after its unexpose is not held against that unexpose.

    The unexpose advances the generation, and the derivation that follows
    lists the re-exposed port at that same generation. It is later than the
    unexpose and must push the port's set. Comparing the two as "at or after"
    would refuse it forever: the port would be exposed, a grant on it would
    be written, and no granted device would ever be admitted through it.

    The unexpose revoked the port's first grant, as an unexpose does, so the
    grant the second derivation pushes is a new one.
    """

    rig = make_rig()
    first = rig.grant(BOB_NODE.slug)["grantId"]
    enforcer = rig.build(interval_seconds=0.0)
    enforcer.apply_now()
    assert rig.exposes(port=PORT)[-1]["keys"] == [BOB_KEY]
    unexposer, outcome = _unexpose(rig, enforcer, PORT)
    assert _done_within(unexposer, WAIT_BOUND), outcome
    assert outcome == [None]
    assert PORT not in _exposed_ports(rig)
    assert _granted(rig) == []

    rig.add_exposure(PORT)
    second = rig.grant(CAROL_NODE.slug)["grantId"]
    enforcer.apply_now()

    assert second != first
    assert _ops(rig, PORT)[-2:] == ["unexpose", "expose"]
    assert rig.exposes(port=PORT)[-1]["keys"] == [CAROL_KEY]
    assert rig.connect(CAROL_NODE)["admitted"] is True
    assert rig.connect(BOB_NODE)["admitted"] is False


def test_the_unexpose_of_one_port_does_not_cost_another_port_its_push(make_rig: Any) -> None:
    """Only the unexposed port's push is dropped; the others of the same derivation land.

    One derivation reads two new grants, one for each of two ports, and is
    held on its roster read. The lower port is unexposed meanwhile, so when
    the derivation is released both of its pushes are pending and the dropped
    one comes first: a derivation that gave up at the first stale port would
    never reach the second. The push for the port that left is dropped; the
    push for the port that stayed is sent by this derivation.
    """

    rig = make_rig()
    rig.add_exposure(OTHER_PORT)
    enforcer = rig.build(interval_seconds=0.0)
    enforcer.apply_now()
    rig.grant(BOB_NODE.slug)
    kept = rig.grant(CAROL_NODE.slug, port=OTHER_PORT)["grantId"]
    assert PORT < OTHER_PORT
    pushes = len(rig.exposes(port=OTHER_PORT))
    derivation = _Derivation(rig, step="roster")
    derivation.start(enforcer)
    unexposer, outcome = _unexpose(rig, enforcer, PORT)
    assert _done_within(unexposer, WAIT_BOUND), outcome
    assert len(rig.exposes(port=OTHER_PORT)) == pushes
    derivation.finish()

    assert len(rig.exposes(port=OTHER_PORT)) == pushes + 1
    assert rig.exposes(port=OTHER_PORT)[-1]["keys"] == [CAROL_KEY]
    assert rig.connect(CAROL_NODE, port=OTHER_PORT)["admitted"] is True
    assert _granted(rig) == [kept]
    assert _ops(rig, PORT)[-1] == "unexpose"
    assert PORT not in _exposed_ports(rig)
