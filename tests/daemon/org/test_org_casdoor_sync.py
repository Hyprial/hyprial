"""S2: the owner's membership actor keeps OrgFS in line with Casdoor, per user.

A fake Casdoor answers the group-scoped single-user read and counts every
read; OrgFS is a real local runtime.  Acceptance (design §3, S2 node):
pending removal follows one event with one read; operator confirmation is
separate from a historical Casdoor membership observation; a forged event changes nothing; casdoorOnly is reported, never added; the owner is never removed;
the backstop catches a dropped event; and the not-found rules with their
breaker (org-improver, 2026-10-08).
"""

# ruff: noqa: F811 - the imported pytest fixtures are redefined as parameters

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from tests.support.tailnet_membership import confirm_removal, confirm_ipc_removal

from hyprial.daemon.impl.org.network.casdoor import CasdoorGroupError
from hyprial.daemon.impl.org.network.casdoor.sync import (
    CasdoorMembershipSync,
    ConfirmedMembers,
    breaker_trips,
)

from .network.test_org_lifecycle import (  # noqa: F401 - fixtures
    BOB,
    OWNER,
    _acl_member_users,
    _member_users,
    _service,
    home,
    runtime,
)

from tests.support.tailnet_membership import member as roster_member

ORG = "group-acme"
CAROL = "carol@example"
DAVE = "dave@example"
EVE = "eve@example"
FRANK = "frank@example"


class FakeCasdoor:
    """Single-user reads only, like the patched endpoint; no list call exists."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.reads: list[str] = []
        self.gate: threading.Event | None = None

    def member_state(self, group: str, user: str) -> str:
        self.reads.append(user)
        state = self.states.get(user, "not-member")  # what Casdoor answered, then the reply is slow
        if self.gate is not None:
            assert self.gate.wait(5)
        if state.startswith("CASDOOR_"):
            raise CasdoorGroupError(state, state)
        return state

    def require_member(self, group: str, user: str) -> None:
        if self.states.get(user, "not-member") != "member":
            raise CasdoorGroupError("CASDOOR_NOT_A_MEMBER", user)

    def add_member(self, group: str, user: str) -> bool:
        self.states[user] = "member"
        return True

    def remove_member(self, group: str, user: str) -> bool:
        self.states[user] = "not-member"
        return True


def _c(user: str) -> str:
    return f"hyprial/{user}"


def _org_with(runtime: Any, home: Path, casdoor: FakeCasdoor, *members: str) -> None:
    casdoor.states[_c(OWNER)] = "member"
    service = _service(runtime, home, casdoor=casdoor)
    created = service.create(ORG)
    for member in members:
        service.invite(ORG, f"user:{member}")
        row = roster_member(member, "device-" + member)
        runtime.membership_sidecar.tailnets[created["spaceId"]]["members"][row["deviceId"]] = row


def _pending(runtime, home, owner):
    operations = [json.loads(path.read_text()) for path in (home / "tailnet/removals").glob("*.json")]
    operation = next(row for row in operations if row["owner"] == owner)
    assert operation["phase"] == "pending-confirmation"
    assert operation["confirmed"] is False
    assert operation["deviceIds"] == ["device-" + owner]
    assert f"user:{owner}" in _member_users(runtime, ORG)
    assert f"user:{owner}" in _acl_member_users(runtime, ORG)
    assert not any(op == "member-revoke" for op, _ in runtime.membership_sidecar.calls)
    return operation


def _sync(runtime: Any, home: Path, casdoor: FakeCasdoor, **options: Any) -> CasdoorMembershipSync:
    return CasdoorMembershipSync(
        service_factory=lambda: _service(runtime, home, casdoor=casdoor),
        confirmed=ConfirmedMembers(home / "org-sync" / "confirmed-members.json"),
        **options,
    )


def _settle(sync: CasdoorMembershipSync, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not sync.idle():
        assert time.monotonic() < deadline, "membership actor did not settle"
        time.sleep(0.01)


def _started(sync: CasdoorMembershipSync) -> CasdoorMembershipSync:
    sync.start()
    _settle(sync)
    return sync


def test_a_casdoor_removal_is_pending_after_one_event_and_one_read(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        casdoor.reads.clear()
        casdoor.states[_c(BOB)] = "not-member"

        sync.user_changed(_c(BOB))
        _settle(sync)

        _pending(runtime, home, BOB)
        assert casdoor.reads == [_c(BOB)]  # one user read, never the group
    finally:
        sync.stop()


def test_a_forged_event_changes_nothing_on_its_own(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        before = _member_users(runtime, ORG)
        casdoor.reads.clear()

        sync.user_changed(_c(BOB))  # Casdoor still says member
        sync.user_changed("hyprial/nobody@example")  # never a member anywhere
        _settle(sync)

        assert _member_users(runtime, ORG) == before
        assert casdoor.reads == [_c(BOB), "hyprial/nobody@example"]
        assert sync.status(ORG)["casdoorOnly"] == []
    finally:
        sync.stop()


def test_a_casdoor_only_user_is_reported_not_added(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        casdoor.states[_c(CAROL)] = "member"

        sync.user_changed(_c(CAROL))
        _settle(sync)

        assert sync.status(ORG)["casdoorOnly"] == [f"user:{CAROL}"]
        assert f"user:{CAROL}" not in _member_users(runtime, ORG)
    finally:
        sync.stop()


def test_the_owner_is_never_removed(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        casdoor.states[_c(OWNER)] = "not-member"

        sync.user_changed(_c(OWNER))
        sync.reconcile()
        _settle(sync)

        assert f"user:{OWNER}" in _member_users(runtime, ORG)
        assert sync.status(ORG)["lastError"]["code"] == "CASDOOR_OWNER_NOT_IN_GROUP"
    finally:
        sync.stop()


def test_the_backstop_catches_a_dropped_event(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor, interval_seconds=0.2))
    try:
        casdoor.states[_c(BOB)] = "not-member"  # no event is sent

        deadline = time.monotonic() + 5
        while not list((home / "tailnet/removals").glob("*.json")):
            assert time.monotonic() < deadline, "the backstop never ran"
            time.sleep(0.02)

        _settle(sync)
        _pending(runtime, home, BOB)
        assert sync.status(ORG)["lastFullSyncAt"] is not None
    finally:
        sync.stop()


def test_the_full_pass_reads_each_orgfs_member_once(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL)
    sync = _sync(runtime, home, casdoor)
    try:
        casdoor.reads.clear()
        sync.start()
        _settle(sync)

        assert sorted(casdoor.reads) == sorted(_c(user) for user in (OWNER, BOB, CAROL))
    finally:
        sync.stop()


def test_events_for_one_user_coalesce_while_a_read_is_in_flight(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        casdoor.reads.clear()
        casdoor.gate = threading.Event()
        for _ in range(6):
            sync.user_changed(_c(BOB))
        time.sleep(0.2)
        casdoor.gate.set()
        _settle(sync)

        assert casdoor.reads.count(_c(BOB)) <= 2  # the one in flight, then one more
    finally:
        casdoor.gate = None
        sync.stop()


# -- not found: confirmed members only, and the breaker ------------------------


def test_an_unconfirmed_user_casdoor_does_not_know_is_only_reported(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _sync(runtime, home, casdoor)  # never started: nothing confirmed yet
    try:
        casdoor.states[_c(BOB)] = "no-user"

        sync.user_changed(_c(BOB))
        _settle(sync)

        assert f"user:{BOB}" in _member_users(runtime, ORG)
        assert sync.status(ORG)["casdoorMissing"] == [f"user:{BOB}"]
    finally:
        sync.stop()


def test_a_previously_observed_member_with_deleted_account_needs_confirmation(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor))  # the start-up pass confirms bob
    try:
        casdoor.states[_c(BOB)] = "no-user"

        sync.user_changed(_c(BOB))
        _settle(sync)

        _pending(runtime, home, BOB)
    finally:
        sync.stop()


def test_confirmation_survives_a_restart(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    _started(_sync(runtime, home, casdoor)).stop()
    casdoor.states[_c(BOB)] = "no-user"
    sync = _sync(runtime, home, casdoor)  # a new daemon, not started
    try:
        sync.user_changed(_c(BOB))
        _settle(sync)

        _pending(runtime, home, BOB)
    finally:
        sync.stop()


def test_a_name_mapping_bug_trips_the_breaker_and_removes_nobody(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        before = _member_users(runtime, ORG)
        for user in (OWNER, BOB, CAROL, DAVE):
            casdoor.states[_c(user)] = "no-user"  # every name maps wrongly

        sync.reconcile()
        _settle(sync)

        assert _member_users(runtime, ORG) == before
        assert sync.status(ORG)["syncHalted"]["reason"].startswith("3 of 3 members")
    finally:
        sync.stop()


def test_the_next_clean_pass_clears_the_halt_and_records_pending_removal(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        for user in (CAROL, DAVE):
            casdoor.states[_c(user)] = "no-user"
        casdoor.states[_c(BOB)] = "not-member"
        sync.reconcile()
        _settle(sync)
        assert sync.status(ORG)["syncHalted"] is not None
        assert f"user:{BOB}" in _member_users(runtime, ORG)  # held this round

        for user in (CAROL, DAVE):
            casdoor.states[_c(user)] = "member"
        sync.reconcile()
        _settle(sync)

        assert sync.status(ORG)["syncHalted"] is None
        _pending(runtime, home, BOB)
    finally:
        sync.stop()


def test_while_halted_a_single_not_member_becomes_pending_and_not_found_waits(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        for user in (BOB, CAROL, DAVE):
            casdoor.states[_c(user)] = "no-user"
        sync.reconcile()
        _settle(sync)
        assert sync.status(ORG)["syncHalted"] is not None

        casdoor.states[_c(CAROL)] = "not-member"
        sync.user_changed(_c(CAROL))
        sync.user_changed(_c(BOB))
        _settle(sync)

        _pending(runtime, home, CAROL)
        assert f"user:{BOB}" in _member_users(runtime, ORG)
        assert f"user:{BOB}" in sync.status(ORG)["casdoorMissing"]
    finally:
        sync.stop()


@pytest.mark.parametrize(
    ("no_user", "members", "trips"),
    [(1, 4, False), (2, 4, True), (2, 10, False), (3, 10, True), (1, 1, False)],
)
def test_the_breaker_threshold_is_the_larger_of_one_and_a_fifth(no_user, members, trips) -> None:
    assert breaker_trips(no_user, members) is trips


def test_any_other_casdoor_error_changes_nothing(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        casdoor.states[_c(BOB)] = "CASDOOR_GROUP_REFUSED"  # e.g. Unauthorized operation

        sync.user_changed(_c(BOB))
        _settle(sync)

        assert f"user:{BOB}" in _member_users(runtime, ORG)
        assert sync.status(ORG)["lastError"]["code"] == "CASDOOR_GROUP_REFUSED"
    finally:
        sync.stop()


def test_confirmed_remove_commits_casdoor_orgfs_and_tailnet_receipts(runtime, home) -> None:
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB)

    result = confirm_removal(_service(runtime, home, casdoor=casdoor).remove, ORG, f"user:{BOB}")

    assert result["phase"] == "complete"
    assert result["upperLayerReceipt"] == "committed"
    assert casdoor.states[_c(BOB)] == "not-member"
    assert f"user:{BOB}" not in _member_users(runtime, ORG)
    assert result["result"]["deviceIds"] == ["device-" + BOB]
    assert result['casdoorGroup'] == f'removed from hyprial/{ORG}'


# -- IPC wiring: local commands notify the actor; org network shows its state --


class _RecordingSync:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    def user_changed(self, user: str, org: str | None = None) -> None:
        self.events.append((user, org))

    def status(self, org: str) -> dict[str, Any]:
        return {"casdoorOnly": [f"user:{CAROL}"], "lastSyncAt": "t", "lastError": None}


def test_invite_and_remove_tell_the_actor_and_network_shows_its_status(runtime, home) -> None:
    from hyprial.daemon.impl.org.network.ipc import (
        ipc_org_invite,
        ipc_org_network,
        ipc_org_remove,
    )

    from .network.test_org_lifecycle import _IpcApp

    from hyprial.identity import address_path

    _service(runtime, home).create(ORG)
    address_path(home).parent.mkdir(parents=True, exist_ok=True)
    address_path(home).write_text("tcp://10.0.0.2:7447#own-psk", encoding="utf-8")
    app = _IpcApp(runtime, home, owner=OWNER)
    sync = _RecordingSync()
    app._casdoor_membership_sync = sync  # type: ignore[attr-defined]

    ipc_org_invite(app, {"org": ORG, "user": f"user:{BOB}"})
    removed = confirm_ipc_removal(ipc_org_remove, app, {"org": ORG, "user": f"user:{BOB}"})
    assert removed["phase"] == "complete"
    assert f"user:{BOB}" not in _member_users(runtime, ORG)
    network = ipc_org_network(app, {})

    # Invite, explicit removal preview, then its bound confirmation.
    assert sync.events == [(f"user:{BOB}", ORG), (f"user:{BOB}", ORG), (f"user:{BOB}", ORG)]
    assert network["casdoorSync"][ORG]["casdoorOnly"] == [f"user:{CAROL}"]


# -- review 1200 ------------------------------------------------------------------


def test_a_pass_where_every_read_fails_keeps_the_halt(runtime, home) -> None:
    """B1: an outage reads no no-user, so it must not count as a clean pass."""

    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        for user in (BOB, CAROL, DAVE):
            casdoor.states[_c(user)] = "no-user"
        sync.reconcile()
        _settle(sync)
        assert sync.status(ORG)["syncHalted"] is not None

        for user in (OWNER, BOB, CAROL, DAVE):
            casdoor.states[_c(user)] = "CASDOOR_UNREACHABLE"
        sync.reconcile()
        _settle(sync)
        assert sync.status(ORG)["syncHalted"] is not None

        casdoor.states[_c(BOB)] = "no-user"  # confirmed bob, single-user event
        sync.user_changed(_c(BOB))
        _settle(sync)

        assert f"user:{BOB}" in _member_users(runtime, ORG)
    finally:
        sync.stop()


def test_a_group_emptied_in_casdoor_trips_the_breaker(runtime, home) -> None:
    """M1: not-member removals count too; an emptied group removes nobody."""

    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE)
    sync = _started(_sync(runtime, home, casdoor))
    try:
        before = _member_users(runtime, ORG)
        for user in (BOB, CAROL, DAVE):
            casdoor.states[_c(user)] = "not-member"

        sync.reconcile()
        _settle(sync)

        assert _member_users(runtime, ORG) == before
        assert sync.status(ORG)["syncHalted"]["reason"] == "3 of 3 members would be removed in one pass"
    finally:
        sync.stop()


def test_pending_single_user_removals_do_not_spend_destructive_budget(runtime, home) -> None:
    """Contract §6: webhooks create pending records, never implicit removal."""

    now = [1_000_000.0]
    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE, EVE, FRANK)  # 5 others: limit 1
    sync = _started(_sync(runtime, home, casdoor, clock=lambda: now[0]))
    try:
        casdoor.states[_c(BOB)] = "not-member"
        casdoor.states[_c(CAROL)] = "not-member"
        casdoor.states[_c(DAVE)] = "not-member"

        sync.user_changed(_c(BOB))
        _settle(sync)
        sync.user_changed(_c(CAROL))
        _settle(sync)

        _pending(runtime, home, BOB)
        _pending(runtime, home, CAROL)
        # Pending operations spend no destructive-removal budget.
        assert sync.status(ORG)["syncHalted"] is None

        now[0] += 601  # Passing the budget window must not imply confirmation.
        sync.user_changed(_c(DAVE))
        _settle(sync)

        _pending(runtime, home, DAVE)
    finally:
        sync.stop()


def test_a_re_invite_between_the_read_and_the_removal_wins(runtime, home) -> None:
    """A read about the old membership never removes the re-invited member."""

    casdoor = FakeCasdoor()
    _org_with(runtime, home, casdoor, BOB, CAROL, DAVE, EVE, FRANK)
    sync = _started(_sync(runtime, home, casdoor))
    service = _service(runtime, home, casdoor=casdoor)
    try:
        casdoor.states[_c(BOB)] = "not-member"
        casdoor.gate = threading.Event()
        sync.user_changed(_c(BOB))
        deadline = time.monotonic() + 5
        while _c(BOB) not in casdoor.reads[-1:]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        confirm_removal(service.remove, ORG, f"user:{BOB}")
        time.sleep(0.01)  # a later added_at than the removed row's
        service.invite(ORG, f"user:{BOB}")
        casdoor.gate.set()
        _settle(sync)

        assert f"user:{BOB}" in _member_users(runtime, ORG)
    finally:
        casdoor.gate = None
        sync.stop()
