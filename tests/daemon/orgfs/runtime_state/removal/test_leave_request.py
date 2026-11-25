# Decisions (phase F4, item 1, real-node finding R2-4, 2026-11-25):
# - The owner "not processing" a member's leave request is not an OrgFS
#   fault.  On the state runtime the request written by the leaving member
#   reaches the owner, ``list_leave_requests`` returns it and
#   ``leave_request_admitted`` (author set, ``memberAddedAt``) admits it.
# - The owner's sweep then previews the removal through ``RemovalJournal``
#   with ``confirmed=False``: the journal stays ``pending-confirmation`` and
#   the sweep reports the org as skipped without any ``org.leaves.*`` event.
#   That has been the org layer's rule since the sidecar membership contract
#   (``test_process_leave_requests_retains_members_until_confirmation``); the
#   owner confirms with the journal's operation id and only then the member
#   row is marked removed, the request is deleted and the member sees it.
# - The test pins that chain across two in-process nodes so a later change
#   to either side shows up here; the silent skip is reported, not fixed
#   (``org/**`` is out of this package's scope).
"""A member's leave request on the state runtime, end to end."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
    leave_request_admitted,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.identity.impl.device import DeviceRecord, write_device_record

from tests.support.tailnet_membership import FakeMembershipSidecar, key

from ..support import Peers, eventually

ORG = "group-acme"
OWNER = "owner@example"
CAROL = "carol@example"


def _owner_service(owner_fs: Any, home: Path, logs: list[str]) -> OrgNetworkService:
    write_device_record(
        home,
        DeviceRecord(
            device_id="node-a",
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    return OrgNetworkService(
        fs=owner_fs,
        home=home,
        node_id="node-a",
        owner=OWNER,
        membership_request=FakeMembershipSidecar().membership_request,
        invalid_devices=InvalidDeviceQuarantine(),
        map_peer=None,
        unmap_peer=None,
        read_own_address=lambda: "tcp://10.0.0.2:7447#own-psk",
        now=lambda: datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC),
        logger=lambda _level, _domain, event, **_fields: logs.append(event),
    )


def _users(fs: Any, space_id: str) -> set[str]:
    return {member.user for member in fs.members(space_id)}


def _journals(home: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in (home / "tailnet/removals").glob("*.json")]


def test_a_leave_request_reaches_the_owner_and_waits_for_its_confirmation(
    peers: Peers, tmp_path: Path
) -> None:
    owner = peers.add("node-a", f"user:{OWNER}")
    carol = peers.add("node-c", f"user:{CAROL}")
    home = tmp_path / "owner-home"
    home.mkdir()
    logs: list[str] = []
    service = _owner_service(owner.fs, home, logs)
    service.create(ORG)
    space_id = next(space.space_id for space in owner.fs.spaces() if space.name == ORG)
    owner.fs.invite(space_id, carol.author, "rw")
    carol.fs.join(space_id)
    added_at = next(row.added_at for row in carol.fs.members(space_id) if row.user == carol.author)

    OrgFsDirectoryStore(carol.fs).put_leave_request(
        ORG,
        CAROL,
        {"org": ORG, "user": CAROL, "deviceId": "node-c", "requestedAt": "t1", "memberAddedAt": added_at},
    )
    owner_store = OrgFsDirectoryStore(owner.fs)

    def arrived() -> None:
        assert CAROL in owner_store.list_leave_requests(ORG)

    eventually(arrived)
    assert leave_request_admitted(owner_store, lambda *_a, **_k: None, ORG, CAROL)

    logs.clear()
    assert service.process_leave_requests() == {"processed": [], "skipped": [ORG], "unbound": []}
    assert [event for event in logs if event.startswith("org.leaves.")] == []
    (journal,) = _journals(home)
    assert journal["phase"] == "pending-confirmation"
    assert journal["owner"] == CAROL
    assert carol.author in _users(owner.fs, space_id)

    confirmed = service.remove(
        ORG, CAROL, confirmed=True, operation_id=journal["operationId"], device_ids=journal["deviceIds"]
    )

    assert confirmed["phase"] == "complete"
    assert _users(owner.fs, space_id) == {owner.author}
    assert owner_store.list_leave_requests(ORG) == {}

    def carol_sees_it() -> None:
        assert carol.author not in _users(carol.fs, space_id)

    eventually(carol_sees_it)
