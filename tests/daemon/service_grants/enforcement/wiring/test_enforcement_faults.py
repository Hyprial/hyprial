"""What the wired daemon does when a step of the admission path fails.

Decision (S4-py fix, 2026-11-27, coordinator ruling, added tests): every
case here drives a real ``DaemonApplication`` whose sidecar is the stdio
fixture, and the failure is made on the sidecar's wire or in the org
directory, never above it. Nothing is faked in the enforcer or the service.

The fixture's fault file (``FAKE_FORWARD_FAULT``, written by the daemon
wrapper while the sidecar runs) ends one command in a chosen way. An expose
can answer an error, as the Go engine does while it is not up; never be
executed and never be answered; or be executed with its reply lost. The last
two end for the daemon in its own response timeout. A membership-status can
be left unanswered, which ends at the roster budget.

Three groups, each holding one rule:

* a push the sidecar did not confirm keeps the set that was in force, is one
  event, and is pushed again by a later maintenance cycle with no new
  trigger; the enforcer does not remember a set the sidecar never confirmed;
* a directory read that fails, at the org listing, at one org's grant
  directory or at the tailnet binding, tightens the port to nobody and never
  widens it; the grant stays on disk; no event names the grant, which is the
  readers' stated decision and is held here as what it looks like;
* an unexpose asked while a maintenance derivation is stuck ends within its
  bound, leaves the port's unexpose as its last command on the wire, and
  revokes that port's grant and no other; and a second unexpose of a port
  whose first is still waiting fails at once and says which is so.

A failed push does not mark the sets stale, so the retry is the next
throttled derivation, thirty seconds on a serving daemon. The push cases
shorten only that interval, through the enforcer's own attribute: what they
hold is that the later derivation pushes again, not how soon.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.forwarding.controller import _DEFAULT_TIMEOUT as RESPONSE_TIMEOUT
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.service_grants.enforcement.enforcer import (
    DEFAULT_MEMBERSHIP_TIMEOUT as ROSTER_BUDGET,
)
from hyprial.daemon.impl.service_grants.enforcement.enforcer import (
    DEFAULT_UNEXPOSE_WAIT_SECONDS as UNEXPOSE_WAIT,
)
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
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
from tests.daemon.service_grants.enforcement.wiring._app import Daemon

PUSH_REJECTED = "network.exposure.admission_rejected"
LISTING = LocalOrgFs.listdir
BOB_KEY = peer_key(BOB_NODE, ALICE_NODE)
CAROL_KEY = peer_key(CAROL_NODE, ALICE_NODE)
MARGIN = 5.0
SETTLE = RESPONSE_TIMEOUT + MARGIN


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

    def make() -> Daemon:
        root = tmp_path / "daemon"
        root.mkdir()
        daemon = Daemon(root, harness)
        daemon.start()
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        daemon.close()


def _grant(daemon: Daemon, device: Any) -> str:
    grant = daemon.app.handle("service.grant", {"org": ORG, "port": PORT, "device": device.slug})
    daemon.settle(timeout=SETTLE)
    return grant["grant"]["grantId"]


def _admitted(daemon: Daemon, device: Any) -> bool:
    """Whether the sidecar's gate now admits ``device`` to the port."""

    controller = daemon.supervisor.endpoints().controller._controller
    probe = {"v": 7, "op": "fixture-connect", "port": PORT, "device": device.device_id}
    return controller._request(probe, "fixture-connected")["admitted"] is True


def _maintenance_cycles(daemon: Daemon, count: int) -> None:
    for _ in range(count):
        daemon.app._offer_service_admission_tick()
        daemon.settle(timeout=SETTLE)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("reject", id="the-sidecar-answers-an-error"),
        pytest.param("drop", id="the-command-is-never-executed"),
        pytest.param("silent", id="the-command-runs-and-its-reply-is-lost"),
    ],
)
def test_a_push_that_the_sidecar_did_not_confirm_keeps_the_set_and_a_later_cycle_pushes_again(
    make_daemon: Any, mode: str
) -> None:
    daemon = make_daemon()
    daemon.app._service_admission_enforcer._interval = 0.0
    _grant(daemon, BOB_NODE)
    assert _last_keys(daemon) == [BOB_KEY]
    assert _admitted(daemon, BOB_NODE)
    pushes = len(daemon.exposes(port=PORT))

    daemon.arm_fault("expose", mode, port=PORT)
    daemon.events.clear()
    _grant(daemon, CAROL_NODE)

    [(level, fields)] = daemon.logged(PUSH_REJECTED)
    assert (level, fields) == (
        "error",
        {"port": PORT, "code": "INTERNAL" if mode == "reject" else "FORWARD_PROTOCOL"},
    )
    assert len(daemon.exposes(port=PORT)) == pushes + 1
    assert _last_keys(daemon) == [BOB_KEY, CAROL_KEY]
    assert _admitted(daemon, BOB_NODE)
    assert _admitted(daemon, CAROL_NODE) is (mode == "silent")
    assert _remembered(daemon, PORT) == [BOB_KEY]

    daemon.events.clear()
    _maintenance_cycles(daemon, 4)

    assert len(daemon.exposes(port=PORT)) == pushes + 2
    assert _last_keys(daemon) == [BOB_KEY, CAROL_KEY]
    assert _admitted(daemon, BOB_NODE)
    assert _admitted(daemon, CAROL_NODE)
    assert _remembered(daemon, PORT) == [BOB_KEY, CAROL_KEY]
    assert daemon.logged(PUSH_REJECTED) == []


def _last_keys(daemon: Daemon) -> list[str]:
    return daemon.exposes(port=PORT)[-1]["keys"]


def _remembered(daemon: Daemon, port: int) -> list[str] | None:
    pushed = daemon.app._service_admission_enforcer._pushed.get(port)
    return None if pushed is None else list(pushed.keys)


def _break(daemon: Daemon, failure: str) -> None:
    """Make one step of the derivation fail, and nothing above it.

    The first two replace one method of the live OrgFS facade, so that the
    production readers, the service grant service and the application's own
    input lookup all run as they do on a serving daemon. The third creates a
    second directory space of the org's name: nothing is replaced, and the
    org's tailnet binding cannot be resolved because the directory is
    ambiguous.
    """

    facade = daemon.harness.fs
    if failure == "orgs":

        def spaces() -> Any:
            raise OrgFsError("io-error", {"message": "directory unavailable"})

        facade.spaces = spaces
    elif failure == "grants":
        def grant_directory(space_id: str, node: str, **options: Any) -> Any:
            if "service-grants" in node:
                raise OrgFsError("io-error", {"message": "directory unavailable"})
            return LISTING(facade, space_id, node, **options)

        facade.listdir = grant_directory
    else:
        facade.create_space(ORG)


def _grant_documents(daemon: Daemon, owner: str) -> list[str]:
    """The grant documents in the owner's directory.

    The space is named by its id, so the read holds while the org listing and
    the org's directory resolution are failing. The listing is made by the
    facade's own ``listdir``, taken when this module is imported and so
    before any case replaces it on the facade.
    """

    facade = daemon.harness.fs
    [directory] = facade.resolve(daemon.harness.space_id, f"directory/service-grants/{owner}")
    return sorted(item.name for item in LISTING(facade, daemon.harness.space_id, directory.path))


def _modes_pushed(daemon: Daemon) -> set[str]:
    return {push["admission"] for push in daemon.exposes(port=PORT)}


def _grant_events(daemon: Daemon) -> list[str]:
    return sorted({name for _l, _c, name, _f in daemon.events if name.startswith("service.grant.")})


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("orgs", id="the-org-listing-cannot-be-read"),
        pytest.param("grants", id="one-orgs-grant-directory-cannot-be-read"),
        pytest.param("tailnet", id="the-tailnet-binding-cannot-be-resolved"),
    ],
)
def test_a_directory_read_that_fails_closes_the_port_and_reports_no_grant(
    make_daemon: Any, failure: str
) -> None:
    """Each of the three directory steps can fail on its own; none widens the port.

    The set is derived again with that one step failing, and it tightens to
    nobody: the port stops admitting the device it admitted a moment before.
    The grant itself is not touched, so the device is admitted again by the
    derivation that follows once the step can be read.

    No event is logged for it. That is the readers' stated decision (an org
    whose documents cannot be read has nothing that can be reported per
    grant), and this case holds what that decision looks like on a serving
    daemon: the operator sees a granted device refused with no line that
    names the grant or the org. The migration guide's diagnostic table
    describes ``service.grant.key_unavailable``; none of these three failures
    produces it.
    """

    daemon = make_daemon()
    grant_id = _grant(daemon, BOB_NODE)
    assert _last_keys(daemon) == [BOB_KEY]
    assert _admitted(daemon, BOB_NODE)
    assert _grant_documents(daemon, ALICE_NODE.owner) == [f"{grant_id}.json"]

    _break(daemon, failure)
    daemon.events.clear()
    daemon.app._service_admission_changed("service.grant")
    daemon.settle(timeout=SETTLE)

    assert _last_keys(daemon) == []
    assert _admitted(daemon, BOB_NODE) is False
    assert _modes_pushed(daemon) == {"keys"}
    assert _grant_events(daemon) == []
    assert [name for _l, _c, name, _f in daemon.events] == []
    assert BOB_KEY not in str(daemon.events)
    assert _grant_documents(daemon, ALICE_NODE.owner) == [f"{grant_id}.json"]


OTHER_PORT = PORT + 1
OTHER_EXPOSURE = {
    "port": OTHER_PORT,
    "target": f"tcp:127.0.0.1:{OTHER_PORT}",
    "proxyProtocol": "none",
}


def _expose(daemon: Daemon, exposure: dict[str, Any]) -> None:
    daemon.app.handle("network.expose", dict(exposure))
    daemon.settle()


def _grants(daemon: Daemon) -> dict[int, str]:
    """Port to the id of the grant on it that the directory still counts effective."""

    granted = daemon.app.handle("service.grants", {"org": ORG})["granted"]
    return {int(item["service"]["port"]): item["grantId"] for item in granted}


def _exposed_ports(daemon: Daemon) -> set[int]:
    exposures = daemon.app.handle("network.exposures", {})["exposures"]
    return {int(item["port"]) for item in exposures}


def _after_its_unexpose(daemon: Daemon, port: int) -> list[str]:
    """The commands for ``port`` that followed its unexpose; the whole list when there is none."""

    ops = daemon.wire_ops(port)
    return ops[ops.index("unexpose") + 1 :] if "unexpose" in ops else ops


def _unexpose(daemon: Daemon, port: int) -> tuple[float, threading.Thread, list[Any]]:
    """Start the real ``network.unexpose`` request of ``port``; return when it was started."""

    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(daemon.app.handle("network.unexpose", {"port": port}))
        except BaseException as error:  # noqa: BLE001 - the test reads the failure
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    return started, thread, outcome


def _seconds_taken(started: float, thread: threading.Thread, bound: float) -> float | None:
    """How long the thread took from ``started``, or ``None`` when it outlived ``bound``."""

    thread.join(max(0.0, bound - (time.monotonic() - started)))
    return None if thread.is_alive() else time.monotonic() - started


@pytest.mark.parametrize(
    ("stuck", "step", "bound"),
    [
        pytest.param(
            "on the roster read",
            ("membership-status", None),
            ROSTER_BUDGET + MARGIN,
            id="a-derivation-stuck-on-the-roster-read",
        ),
        pytest.param(
            "on the push of that port",
            ("expose", PORT),
            UNEXPOSE_WAIT,
            id="a-derivation-stuck-on-the-push-of-that-port",
        ),
    ],
)
def test_an_unexpose_during_a_maintenance_derivation_stuck_in_one_step_ends_within_a_bound(
    make_daemon: Any, stuck: str, step: tuple[str, int | None], bound: float
) -> None:
    """Two ports are exposed, and a derivation over both is stuck in one step.

    The step is left to end on its own, as it would on a serving daemon: a
    membership-status the sidecar never answers, which ends at the roster
    budget, or an expose for the lower port that it never answers, which ends
    at the sidecar response timeout. While it is stuck, ``network.unexpose``
    is asked for that lower port.

    What is held is the bound, and the end state. The unexpose ends within
    the bound named for each step, the port's last command on the wire is its
    unexpose, nothing is pushed for the port afterwards, its grant is revoked
    and the other port's is not, and the other port ends with its granted
    device admitted.

    How long it takes today is written here, not asserted: there is no
    requirement that an unexpose be slow. A push of the port being unexposed
    is waited for by the enforcer on purpose, so that the unexpose is the
    port's last command on the wire; that wait would fail at
    ``UNEXPOSE_WAIT`` if the push outlived it, and the unexpose here ends
    when the push times out, at the response timeout. A roster read is not
    waited for by the enforcer at all, and the unexpose still ends only when
    the roster read has timed out, at the roster budget: the forwarding
    authority runs one sidecar command at a time, and the unexpose is queued
    behind the membership read on it. That is the forwarding layer's order,
    not the enforcer's lock, and a forwarding layer that let the unexpose
    ahead would make this case faster and no less correct.
    """

    daemon = make_daemon()
    daemon.app._service_admission_enforcer._interval = 0.0
    _expose(daemon, OTHER_EXPOSURE)
    op, port = step
    daemon.arm_fault(op, "drop", port=port)
    daemon.app.handle("service.grant", {"org": ORG, "port": PORT, "device": BOB_NODE.slug})
    kept = daemon.app.handle(
        "service.grant", {"org": ORG, "port": OTHER_PORT, "device": CAROL_NODE.slug}
    )["grant"]["grantId"]
    daemon.app._offer_service_admission_tick()
    daemon.wait_for_wire(op)

    started, unexposer, outcome = _unexpose(daemon, PORT)
    taken = _seconds_taken(started, unexposer, bound)
    daemon.settle()

    assert taken is not None, outcome
    assert taken < bound
    assert outcome == [{"port": PORT, "removed": True}]
    assert daemon.wire_ops(PORT)[-1] == "unexpose"
    assert _after_its_unexpose(daemon, PORT) == []
    assert _exposed_ports(daemon) == {OTHER_PORT}
    assert _grants(daemon) == {OTHER_PORT: kept}
    assert daemon.exposes(port=OTHER_PORT)[-1]["keys"] == [CAROL_KEY]
    assert daemon.fault_file.exists() is False


def test_a_second_unexpose_of_a_port_whose_first_is_waiting_fails_at_once_and_says_why(
    make_daemon: Any,
) -> None:
    """Two unexposes of one port overlap: the second does not queue, and does not blame a push.

    The first is waiting, as it is meant to, for a push of the port that is
    in flight. The second arrives meanwhile. It fails at once with the code
    the exposure path already reports, and its message names what is so: the
    port is already being unexposed. It used to report the same code with the
    message of the other outcome, that the port is being pushed and the
    caller should try again, which sends the caller to retry a request whose
    twin is still running. Nothing changes on its account: the first goes on
    and ends as the port's unexpose.
    """

    daemon = make_daemon()
    _grant(daemon, BOB_NODE)
    enforcer = daemon.app._service_admission_enforcer
    expose = daemon.supervisor.expose
    claimed = threading.Event()
    release = threading.Event()

    def held(exposure: dict[str, Any], **admission: Any) -> Any:
        if exposure["port"] == PORT:
            claimed.set()
            assert release.wait(20.0)
        return expose(exposure, **admission)

    daemon.supervisor.expose = held
    daemon.app.handle("service.grant", {"org": ORG, "port": PORT, "device": CAROL_NODE.slug})
    assert claimed.wait(SETTLE)

    _started, first, first_outcome = _unexpose(daemon, PORT)
    _wait_until(lambda: PORT in enforcer._removing)

    with pytest.raises(DaemonRequestError) as refused:
        daemon.app.handle("network.unexpose", {"port": PORT})

    assert refused.value.code == "FORWARDING_UNAVAILABLE"
    assert "already being unexposed" in str(refused.value)
    assert first.is_alive()
    assert _exposed_ports(daemon) == {PORT}

    release.set()
    assert _seconds_taken(_started, first, UNEXPOSE_WAIT) is not None
    daemon.supervisor.expose = expose
    daemon.settle()

    assert first_outcome == [{"port": PORT, "removed": True}]
    assert _exposed_ports(daemon) == set()
    assert _grants(daemon) == {}


def _wait_until(condition: Any, *, timeout: float = SETTLE) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()
