"""A leave request covers only the membership period it was filed in.

Design ``orgfs-minimal-design-2026-11-25.md`` §5: the request carries
``memberAddedAt`` copied from the requester's member row when it is filed,
and the owner's sweep fulfils it only while that value equals the user's
current member row.  The journal's causal relation
(``document_covers_member_add``) is no longer consulted.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceDiagnostics,
    OrgFsDirectoryStore,
    leave_request_admitted,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.identity.impl.org_directory import DirectoryDevice

from tests.support.tailnet_membership import (
    FakeMembershipSidecar,
    confirm_removal,
    key,
    member,
)

OWNER = "owner@example"
BOB = "bob@example"
ORG = "group-acme"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[OrgFsRuntime]:
    runtime = OrgFsRuntime(tmp_path / "state", node_id="node-a", author=f"user:{OWNER}")
    yield runtime
    runtime.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id="node-a",
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    return tmp_path


def _service(
    runtime: OrgFsRuntime, home: Path, *, owner: str = OWNER, node_id: str = "node-a"
) -> OrgNetworkService:
    if not hasattr(runtime, "membership_sidecar"):
        runtime.membership_sidecar = FakeMembershipSidecar()
    sidecar = runtime.membership_sidecar
    if owner != OWNER:
        sidecar = sidecar.for_device(owner, node_id)
    return OrgNetworkService(
        fs=runtime.facade,
        home=home,
        node_id=node_id,
        owner=owner,
        membership_request=sidecar.membership_request,
        invalid_devices=InvalidDeviceDiagnostics(),
        read_own_address=lambda: "tcp://10.0.0.2:7447#own-psk",
        now=lambda: datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC),
    )


@contextmanager
def _as_bob(runtime: OrgFsRuntime) -> Iterator[None]:
    previous = runtime.facade.author
    runtime.facade.author = f"user:{BOB}"
    try:
        yield
    finally:
        runtime.facade.author = previous


def _space_id(runtime: OrgFsRuntime) -> str:
    return next(space.space_id for space in runtime.facade.spaces() if space.name == ORG)


def _bob_added_at(runtime: OrgFsRuntime) -> str:
    return next(
        row.added_at
        for row in runtime.facade.members(_space_id(runtime))
        if row.user == f"user:{BOB}"
    )


def _org_with_bob(runtime: OrgFsRuntime, home: Path) -> OrgFsDirectoryStore:
    _service(runtime, home).create(ORG)
    runtime.facade.invite(_space_id(runtime), f"user:{BOB}", "rw")
    return OrgFsDirectoryStore(runtime.facade)


def _file_request(store: OrgFsDirectoryStore, runtime: OrgFsRuntime, **fields: Any) -> None:
    record = {"org": ORG, "user": BOB, "deviceId": "node-b", "requestedAt": "t1", **fields}
    with _as_bob(runtime):
        store.put_leave_request(ORG, BOB, record)


def _readd_bob(runtime: OrgFsRuntime) -> None:
    previous = _bob_added_at(runtime)
    runtime.facade.remove_member(_space_id(runtime), f"user:{BOB}")
    runtime.facade.invite(_space_id(runtime), f"user:{BOB}", "rw")
    assert _bob_added_at(runtime) != previous


def test_self_leave_writes_the_current_member_row_added_at(
    runtime: OrgFsRuntime, home: Path
) -> None:
    store = _org_with_bob(runtime, home)
    with _as_bob(runtime):
        store.put_device(ORG, DirectoryDevice(
            device_id="node-b", owner=BOB, server_public="nodekey:server-node-b",
            client_public="nodekey:client-node-b", address="tcp://10.0.0.9:7447#b",
            key_generation=1, updated_at="2026-10-03T11:00:00+00:00",
        ))
    table = runtime.membership_sidecar.tailnets[_space_id(runtime)]
    table["members"]["node-b"] = member(BOB, "node-b")
    bob = _service(runtime, home, owner=BOB, node_id="node-b")

    with _as_bob(runtime):
        assert confirm_removal(bob.leave, ORG)["phase"] == "complete"

    record = store.list_leave_requests(ORG)[BOB]
    assert record["memberAddedAt"] == _bob_added_at(runtime)
    assert store.leave_request_covers_current_add(ORG, BOB) is True


def test_a_request_from_the_current_membership_period_covers_the_add(
    runtime: OrgFsRuntime, home: Path
) -> None:
    store = _org_with_bob(runtime, home)
    _file_request(store, runtime, memberAddedAt=_bob_added_at(runtime))

    assert store.leave_request_covers_current_add(ORG, BOB) is True


def test_a_request_from_an_earlier_membership_period_does_not_cover_a_rejoin(
    runtime: OrgFsRuntime, home: Path
) -> None:
    store = _org_with_bob(runtime, home)
    _file_request(store, runtime, memberAddedAt=_bob_added_at(runtime))
    _readd_bob(runtime)

    assert store.leave_request_covers_current_add(ORG, BOB) is False


def test_an_earlier_period_value_rewritten_after_the_rejoin_does_not_cover(
    runtime: OrgFsRuntime, home: Path
) -> None:
    store = _org_with_bob(runtime, home)
    earlier = _bob_added_at(runtime)
    _readd_bob(runtime)
    _file_request(store, runtime, memberAddedAt=earlier)

    assert store.leave_request_covers_current_add(ORG, BOB) is False


@pytest.mark.parametrize(
    "fields",
    [{}, {"memberAddedAt": None}, {"memberAddedAt": 17}, {"memberAddedAt": ""}],
    ids=["missing", "null", "integer", "empty"],
)
def test_a_record_without_a_well_formed_member_added_at_does_not_cover(
    runtime: OrgFsRuntime, home: Path, fields: dict[str, Any]
) -> None:
    store = _org_with_bob(runtime, home)
    _file_request(store, runtime, **fields)

    assert store.leave_request_covers_current_add(ORG, BOB) is False


def test_a_request_for_a_user_without_a_member_row_does_not_cover(
    runtime: OrgFsRuntime, home: Path
) -> None:
    store = _org_with_bob(runtime, home)
    added_at = _bob_added_at(runtime)
    _file_request(store, runtime, memberAddedAt=added_at)
    runtime.facade.remove_member(_space_id(runtime), f"user:{BOB}")

    assert store.leave_request_covers_current_add(ORG, BOB) is False


def test_the_cover_check_does_not_consult_the_journal_relation(
    runtime: OrgFsRuntime, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _org_with_bob(runtime, home)
    _file_request(store, runtime, memberAddedAt=_bob_added_at(runtime))

    def refuse(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("document_covers_member_add must not be called")

    monkeypatch.setattr(runtime.facade, "document_covers_member_add", refuse)

    assert store.leave_request_covers_current_add(ORG, BOB) is True


def test_the_sweep_skips_a_stale_request_as_stale(runtime: OrgFsRuntime, home: Path) -> None:
    store = _org_with_bob(runtime, home)
    _file_request(store, runtime, memberAddedAt=_bob_added_at(runtime))
    _readd_bob(runtime)
    events: list[tuple[str, dict[str, Any]]] = []

    def log(_level: str, _area: str, event: str, **fields: Any) -> None:
        events.append((event, fields))

    assert leave_request_admitted(store, log, ORG, BOB) is False
    assert events == [("org.leaves.stale", {"org": ORG, "user": BOB})]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"memberAddedAt": "2026-10-03T11:00:00Z"}, "2026-10-03T11:00:00Z"),
        ({}, None),
        ({"memberAddedAt": ""}, None),
        ({"memberAddedAt": ["2026"]}, None),
        (["memberAddedAt"], None),
        (json.dumps({"memberAddedAt": "x"}), None),
    ],
    ids=["present", "missing", "empty", "list", "not-a-mapping", "unparsed-text"],
)
def test_the_record_format_check_requires_a_non_empty_string(
    record: object, expected: str | None
) -> None:
    from hyprial.daemon.impl.org.network.directory import leave_request_member_added_at

    assert leave_request_member_added_at(record) == expected
