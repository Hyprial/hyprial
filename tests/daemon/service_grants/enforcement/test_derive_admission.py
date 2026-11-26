"""Deriving each port's admission set from grants and the sidecar roster.

Decision (service grant S4-py, 2026-11-26, coordinator ruling on
s4py-design-mismatch-evidence.md): the admission key of a granted device is
the entry under THIS device's server public key in the recipient's row of the
sidecar's own membership roster, read through ``membership-status``. It is
never taken from the directory device row, which carries none. The
derivation is a pure function of grants, one roster observation per org and
the clock; it is re-derived, never stored. A port's set is the union over
every org, and anything that cannot be resolved tightens the set and is
reported as an unresolved grant that carries no key.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.service_grants import (
    ALL_MEMBERS,
    GrantRecipient,
    ServiceEndpoint,
    ServiceGrant,
    StoredServiceGrant,
)
from hyprial.daemon.impl.service_grants.enforcement import (
    AdmissionProjection,
    AdmissionSet,
    UnresolvedGrant,
    derive_admission,
)
from tests.daemon.service_grants.enforcement._support import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    ORG,
    PORT,
    peer_key,
)

BETA = "group-beta"
NOW_MS = 1_790_000_000_000
LOCAL_DEVICE = ALICE_NODE.device_id
SERVER_PUBLIC = ALICE_NODE.server_public
BOB_KEY = peer_key(BOB_NODE, ALICE_NODE)
CAROL_KEY = peer_key(CAROL_NODE, ALICE_NODE)
EMPTY = AdmissionSet(admission="keys", keys=())
ALLOW = AdmissionSet(admission="allow", keys=())
ROSTER_UNAVAILABLE = "roster-unavailable"


def _grant(
    label: str,
    recipient,
    *,
    port: int = PORT,
    device_id: str = LOCAL_DEVICE,
    org: str = ORG,
    expires_at_ms: int | None = None,
    revoked_at_ms: int | None = None,
) -> StoredServiceGrant:
    """One grant by this device, with a valid id that encodes ``label``."""

    assert len(label) == 1 and label.isalpha()
    return StoredServiceGrant(
        org=org,
        grantor=ALICE_NODE.owner,
        grant=ServiceGrant(
            schema_version=1,
            grant_id=f"sg.{label * 26}",
            service=ServiceEndpoint(
                device_id=device_id, port=port, name=None, protocol="tcp", note=None
            ),
            recipient=(
                ALL_MEMBERS
                if recipient == ALL_MEMBERS
                else GrantRecipient(owner=recipient.owner, device_id=recipient.device_id)
            ),
            granted_at_ms=NOW_MS - 1_000,
            expires_at_ms=expires_at_ms,
            revoked_at_ms=revoked_at_ms,
        ),
    )


def _row(device, *, state: str = "active", with_server_key: bool = True) -> dict:
    row = {
        "owner": device.owner,
        "deviceId": device.device_id,
        "masterPublic": device.master_public,
        "serverPublic": device.server_public,
        "address": device.address,
        "state": state,
        "revision": 1,
    }
    row["clientPublicByServer"] = (
        {SERVER_PUBLIC: peer_key(device, ALICE_NODE)} if with_server_key else {}
    )
    return row


def _roster(*rows: dict, ready: bool = True) -> dict:
    """One tailnet observation in the shape ``membership-status`` returns."""

    return {
        "readiness": {"sidecar": True, "control": True, "member": ready},
        "tailnets": [
            {
                "tailnetId": "space-group-acme",
                "role": "authority",
                "revision": 1,
                "authorityDeviceId": LOCAL_DEVICE,
                "memberReady": ready,
                "leaseRemainingSeconds": None,
                "activeCount": len(rows),
                "tombstoneCount": sum(row["state"] == "tombstone" for row in rows),
                "code": None,
                "members": list(rows),
            }
        ],
    }


def _derive(grants_by_org: dict, rosters: dict, *, now_ms: int = NOW_MS) -> dict:
    return derive_admission(
        grants_by_org,
        rosters,
        device_id=LOCAL_DEVICE,
        server_public=SERVER_PUBLIC,
        now_ms=now_ms,
    )


def _reasons(projection: AdmissionProjection) -> list[tuple[str, str, str, str]]:
    return sorted(
        (item.org, item.grant_id, item.recipient["owner"], item.recipient["deviceId"])
        for item in projection.unresolved
    )


def test_a_grant_to_one_device_puts_that_devices_roster_key_in_the_set() -> None:
    projection = _derive(
        {ORG: (_grant("a", BOB_NODE),)},
        {ORG: _roster(_row(ALICE_NODE), _row(BOB_NODE), _row(CAROL_NODE))},
    )

    assert projection[PORT] == AdmissionProjection(
        admission=AdmissionSet(admission="keys", keys=(BOB_KEY,)), unresolved=()
    )


def test_revoking_the_grant_removes_the_key() -> None:
    projection = _derive(
        {ORG: (_grant("a", BOB_NODE, revoked_at_ms=NOW_MS),)},
        {ORG: _roster(_row(BOB_NODE))},
    )

    assert projection[PORT].admission == EMPTY


def test_an_expired_grant_no_longer_contributes() -> None:
    grants = {ORG: (_grant("a", BOB_NODE, expires_at_ms=NOW_MS + 1),)}
    roster = {ORG: _roster(_row(BOB_NODE))}

    before = _derive(grants, roster)
    at_expiry = _derive(grants, roster, now_ms=NOW_MS + 1)

    assert before[PORT].admission.keys == (BOB_KEY,)
    assert at_expiry[PORT].admission == EMPTY


def test_a_members_grant_admits_every_member() -> None:
    projection = _derive(
        {ORG: (_grant("m", ALL_MEMBERS),)},
        {ORG: _roster(_row(ALICE_NODE), _row(BOB_NODE))},
    )

    assert projection[PORT].admission == ALLOW


def test_revoking_members_returns_to_the_remaining_device_keys() -> None:
    projection = _derive(
        {ORG: (_grant("m", ALL_MEMBERS, revoked_at_ms=NOW_MS), _grant("a", BOB_NODE))},
        {ORG: _roster(_row(BOB_NODE))},
    )

    assert projection[PORT].admission == AdmissionSet(admission="keys", keys=(BOB_KEY,))


def test_an_ungranted_exposure_admits_nobody() -> None:
    projection = _derive({ORG: ()}, {ORG: _roster(_row(BOB_NODE))})

    assert projection == {}


def test_a_port_with_only_unrelated_grants_admits_nobody() -> None:
    projection = _derive(
        {ORG: (_grant("y", CAROL_NODE, port=PORT + 1),)},
        {ORG: _roster(_row(CAROL_NODE))},
    )

    assert projection == {}


def test_an_org_with_no_roster_observation_yet_still_admits_nobody() -> None:
    projection = _derive({ORG: ()}, {})

    assert projection == {}


@pytest.mark.parametrize(
    "roster",
    [
        pytest.param(_roster(), id="member-not-in-roster"),
        pytest.param(_roster(_row(BOB_NODE, state="tombstone")), id="member-tombstoned"),
        pytest.param(
            _roster(_row(BOB_NODE, with_server_key=False)), id="row-without-local-entry"
        ),
        pytest.param(_roster(_row(BOB_NODE), ready=False), id="tailnet-not-ready"),
    ],
)
def test_a_granted_device_without_a_resolvable_key_is_unresolved_not_admitted(
    roster: dict,
) -> None:
    """Ruling 2 and 5: fail closed, and say so without any key."""

    grant = _grant("a", BOB_NODE)

    projection = _derive({ORG: (grant,)}, {ORG: roster})

    result = projection[PORT]
    assert result.admission == EMPTY
    assert [(item.org, item.grant_id) for item in result.unresolved] == [
        (ORG, grant.grant.grant_id)
    ]
    assert result.unresolved[0].recipient == {"owner": BOB_NODE.owner, "deviceId": BOB_NODE.device_id}
    assert result.unresolved[0].reason
    assert BOB_KEY not in repr(result)


def test_a_device_removed_from_the_org_is_not_admitted_while_its_grant_document_remains() -> None:
    """Ruling 5: the recipient must also be an active roster member."""

    grant = _grant("a", BOB_NODE)
    before_removal = _derive({ORG: (grant,)}, {ORG: _roster(_row(BOB_NODE))})
    after_removal = _derive(
        {ORG: (grant,)}, {ORG: _roster(_row(BOB_NODE, state="tombstone"))}
    )

    assert before_removal[PORT].admission.keys == (BOB_KEY,)
    assert after_removal[PORT].admission == EMPTY


def test_grants_from_two_orgs_are_unioned() -> None:
    projection = _derive(
        {ORG: (_grant("a", BOB_NODE),), BETA: (_grant("b", CAROL_NODE, org=BETA),)},
        {ORG: _roster(_row(BOB_NODE)), BETA: _roster(_row(CAROL_NODE))},
    )

    assert sorted(projection[PORT].admission.keys) == sorted((BOB_KEY, CAROL_KEY))


def test_an_unreadable_roster_leaves_only_the_readable_part_and_never_widens_to_allow() -> None:
    projection = _derive(
        {ORG: (_grant("a", BOB_NODE),), BETA: (_grant("m", ALL_MEMBERS, org=BETA),)},
        {ORG: _roster(_row(BOB_NODE))},
    )

    assert projection[PORT].admission == AdmissionSet(admission="keys", keys=(BOB_KEY,))
    assert [(item.org, item.reason) for item in projection[PORT].unresolved] == [
        (BETA, ROSTER_UNAVAILABLE)
    ]


def test_a_readable_members_grant_widens_to_allow_while_another_org_is_unreadable() -> None:
    projection = _derive(
        {ORG: (_grant("m", ALL_MEMBERS),), BETA: (_grant("b", BOB_NODE, org=BETA),)},
        {ORG: _roster(_row(BOB_NODE))},
    )

    assert projection[PORT].admission == ALLOW
    assert [(item.org, item.reason) for item in projection[PORT].unresolved] == [
        (BETA, ROSTER_UNAVAILABLE)
    ]


def test_every_grant_of_an_unreadable_org_is_reported_once() -> None:
    projection = _derive(
        {BETA: (_grant("a", BOB_NODE, org=BETA), _grant("b", CAROL_NODE, org=BETA))},
        {},
    )

    assert _reasons(projection[PORT]) == [
        (BETA, _grant("a", BOB_NODE, org=BETA).grant.grant_id, BOB_NODE.owner, BOB_NODE.device_id),
        (BETA, _grant("b", CAROL_NODE, org=BETA).grant.grant_id, CAROL_NODE.owner, CAROL_NODE.device_id),
    ]


def test_only_this_devices_grants_for_the_port_count() -> None:
    projection = _derive(
        {
            ORG: (
                _grant("a", BOB_NODE),
                _grant("x", CAROL_NODE, device_id="device-a2"),
                _grant("y", CAROL_NODE, port=PORT + 1),
            )
        },
        {ORG: _roster(_row(BOB_NODE), _row(CAROL_NODE))},
    )

    assert projection[PORT].admission.keys == (BOB_KEY,)


def test_a_grant_by_another_grantor_is_not_this_devices_to_push() -> None:
    foreign = StoredServiceGrant(org=ORG, grantor="carol", grant=_grant("a", BOB_NODE).grant)

    projection = _derive({ORG: (foreign,)}, {ORG: _roster(_row(BOB_NODE))})

    assert projection == {}


def test_a_key_that_is_not_a_canonical_nodekey_is_unresolved() -> None:
    row = _row(BOB_NODE)
    row["clientPublicByServer"] = {SERVER_PUBLIC: "nodekey:not-hex"}

    projection = _derive({ORG: (_grant("a", BOB_NODE),)}, {ORG: _roster(row)})

    assert projection[PORT].admission.keys == ()
    [item] = projection[PORT].unresolved
    assert item.reason == "key-malformed"


def test_the_same_recipient_granted_in_two_orgs_is_sent_once() -> None:
    projection = _derive(
        {ORG: (_grant("a", BOB_NODE),), BETA: (_grant("b", BOB_NODE, org=BETA),)},
        {ORG: _roster(_row(BOB_NODE)), BETA: _roster(_row(BOB_NODE))},
    )

    assert projection[PORT].admission.keys == (BOB_KEY,)


def test_the_result_does_not_depend_on_org_or_grant_order() -> None:
    grants = {
        ORG: (_grant("a", BOB_NODE), _grant("c", CAROL_NODE)),
        BETA: (_grant("b", BOB_NODE, org=BETA),),
    }
    rosters = {
        ORG: _roster(_row(BOB_NODE), _row(CAROL_NODE)),
        BETA: _roster(_row(BOB_NODE)),
    }
    reversed_grants = {
        BETA: tuple(reversed(grants[BETA])),
        ORG: tuple(reversed(grants[ORG])),
    }

    assert _derive(grants, rosters) == _derive(reversed_grants, dict(reversed(list(rosters.items()))))


def test_projection_types_carry_no_key_outside_the_set() -> None:
    assert AdmissionSet.__dataclass_fields__.keys() == {"admission", "keys"}
    assert UnresolvedGrant.__dataclass_fields__.keys() == {
        "org",
        "grant_id",
        "recipient",
        "reason",
    }
    assert AdmissionProjection.__dataclass_fields__.keys() == {"admission", "unresolved"}
