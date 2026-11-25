"""Contract OC0 §2.2, §2.3, §2.5, §4.2: ``org`` cell kind and validation."""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest

from hyprial.daemon.impl.cells.codec import decode, encode, from_document, to_document, verify
from hyprial.daemon.impl.cells.model import (
    CELL_KINDS,
    EAGER_KINDS,
    Cell,
    CellBinding,
    CellError,
    CellKind,
    CellMember,
    CellPermissions,
    CellScope,
    default_scope,
)
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.kernel import keyed_cell_id, subject_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE, authority_for, uid_for

BOB = authority_for("user:bob")
ALICE_REF = f"u.{uid_for('user:alice')}"
BOB_REF = f"u.{uid_for('user:bob')}"
AGENT_ID = "a." + "b" * 26
DEVICE_A = "d." + "a" * 32
DEVICE_B = "d." + "b" * 32
ORG_NAME = "group-acme"
SPACE_ID = "sp-acme-1"
PARENT_CELL_ID = "c." + "p" * 26


def _valid_org_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "org.name": ORG_NAME,
        "org.issuer": "https://login.example.com",
        "org.directorySpace": SPACE_ID,
        "org.tailnet": SPACE_ID,
        "org.parent": None,
        "org.members": {
            ALICE_REF: {
                "username": "alice@example.com",
                "joinedAtMs": 1_763_700_000_000,
                "invitedBy": ALICE_REF,
                "devices": [
                    {
                        "deviceUid": DEVICE_A,
                        "keyId": "kid-alice-1",
                        "publicKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                        "verifiedAtMs": 1_763_700_000_000,
                    }
                ],
            }
        },
    }
    params.update(overrides)
    return params


def _valid_org_cell(**overrides: Any) -> Cell:
    fields: dict[str, Any] = {
        "cell_id": keyed_cell_id("org", ALICE, SPACE_ID),
        "kind": "org",
        "authority": ALICE,
        "scope": CellScope.of_org(ORG_NAME),
        "binding": CellBinding.keyed(SPACE_ID),
        "name": ORG_NAME,
        "members": (CellMember(ALICE_REF, "owner"),),
        "permissions": CellPermissions(),
        "params": _valid_org_params(),
        "revision": 1,
        "updated_at_ms": 1_763_700_000_000,
    }
    fields.update(overrides)
    return Cell(**fields)


def test_agent_cell_encode_golden_bytes() -> None:
    cell = Cell(
        cell_id=subject_cell_id("agent", AGENT_ID),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(AGENT_ID),
        name="worker",
        members=(CellMember("agent:alice:mac:worker", "harness"),),
        params={"delivery.bodyRetentionMs": 3_600_000},
        revision=2,
        updated_at_ms=42,
    )
    expected = (
        '{"authority":"'
        + ALICE
        + '","binding":{"type":"subject","value":"'
        + AGENT_ID
        + '"},"cellId":"'
        + subject_cell_id("agent", AGENT_ID)
        + '","kind":"agent","members":[{"ref":"agent:alice:mac:worker","role":"harness"}],'
        '"name":"worker","params":{"delivery.bodyRetentionMs":3600000},'
        '"permissions":{},"retired":false,"revision":2,'
        '"schema":"hyprial-cell/v1","scope":{"kind":"org","org":null},"updatedAtMs":42}'
    )
    assert encode(cell) == expected


def test_cell_kinds_are_converged_to_agent_and_org() -> None:
    assert CELL_KINDS == frozenset({"agent", "org"})
    assert EAGER_KINDS == CELL_KINDS
    assert set(get_args(CellKind)) == {"agent", "org"}
    assert default_scope("agent") == CellScope.of_org()
    with pytest.raises(CellError) as raised:
        default_scope("org")
    assert raised.value.code == "invalid-cell"
    with pytest.raises(CellError) as spec_raised:
        CellSpec(kind="org", binding=CellBinding.keyed(SPACE_ID))
    assert spec_raised.value.code == "invalid-cell"
    with pytest.raises(CellError) as dept_raised:
        CellSpec(kind="department", binding=CellBinding.keyed("eng"))
    assert dept_raised.value.code == "invalid-cell"


def test_valid_org_cell_round_trips_and_verifies() -> None:
    cell = _valid_org_cell(
        members=(CellMember(ALICE_REF, "owner"), CellMember(BOB_REF, "admin")),
        params=_valid_org_params(
            **{
                "org.members": {
                    ALICE_REF: {
                        "username": "alice@example.com",
                        "joinedAtMs": 1_763_700_000_000,
                        "invitedBy": ALICE_REF,
                        "devices": [
                            {
                                "deviceUid": DEVICE_A,
                                "keyId": None,
                                "publicKey": None,
                                "verifiedAtMs": 1_763_700_000_000,
                            }
                        ],
                    },
                    BOB_REF: {
                        "username": "bob@example.com",
                        "joinedAtMs": 1_763_700_000_500,
                        "invitedBy": ALICE_REF,
                        "devices": [
                            {
                                "deviceUid": DEVICE_B,
                                "keyId": "kid-bob-1",
                                "publicKey": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=",
                                "verifiedAtMs": 1_763_700_000_500,
                            }
                        ],
                    },
                }
            }
        ),
    )
    text = encode(cell)
    doc = to_document(cell)
    assert doc["schema"] == "hyprial-cell/v1"
    assert decode(text) == cell
    assert from_document(doc) == cell
    assert (
        verify(
            cell,
            authors=("user:alice",),
            space_owner="user:alice",
            writer_of={ALICE: "user:alice"}.get,
            kind="org",
            cell_id=cell.cell_id,
            space_role="org",
            space_org=ORG_NAME,
        )
        is None
    )


def test_valid_child_org_cell_accepts_parent_and_inherited_tailnet() -> None:
    parent = {"cellId": PARENT_CELL_ID, "directorySpace": "sp-parent", "owner": ALICE_REF}
    cell = _valid_org_cell(
        params=_valid_org_params(
            **{
                "org.tailnet": "sp-parent",
                "org.parent": parent,
            }
        )
    )
    assert decode(encode(cell)) == cell


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": CellScope.owner()},
        {"scope": CellScope.of_org()},
        {"scope": CellScope.of_org("group-other")},
        {"binding": CellBinding.subject(SPACE_ID), "cell_id": subject_cell_id("org", SPACE_ID)},
        {"binding": CellBinding.keyed("") , "cell_id": keyed_cell_id("org", ALICE, "")},
        {"permissions": CellPermissions.of(read=("*",))},
        {"members": ()},
        {"members": (CellMember(ALICE_REF, "admin"),)},
        {"members": (CellMember(BOB_REF, "owner"),)},
        {"members": (CellMember(ALICE_REF, "owner"), CellMember(BOB_REF, "owner"))},
        {"members": (CellMember(ALICE_REF, "owner"), CellMember(BOB_REF, "lead"))},
        {"members": (CellMember(ALICE_REF, "owner"), CellMember("agent:bob:mac:a", "member"))},
    ],
)
def test_invalid_org_top_level_or_members_are_rejected(overrides: dict[str, Any]) -> None:
    cell = _valid_org_cell(**overrides)
    for call in (lambda: to_document(cell), lambda: encode(cell)):
        with pytest.raises(CellError) as raised:
            call()
        assert raised.value.code == "invalid-cell"


@pytest.mark.parametrize(
    ("mutate_params"),
    [
        lambda p: p.pop("org.name"),
        lambda p: p.update({"org.name": "acme"}),
        lambda p: p.update({"org.name": "group-acme-acl"}),
        lambda p: p.update({"org.issuer": ""}),
        lambda p: p.update({"org.issuer": "   "}),
        lambda p: p.update({"org.directorySpace": "sp-other"}),
        lambda p: p.update({"org.tailnet": "sp-other"}),
        lambda p: p.update({"org.tailnet": ""}),
        lambda p: p.update({"org.parent": "bad"}),
        lambda p: p.update({"org.parent": {"cellId": PARENT_CELL_ID, "directorySpace": "sp-p"}}),
        lambda p: p.update(
            {
                "org.parent": {
                    "cellId": PARENT_CELL_ID,
                    "directorySpace": "sp-p",
                    "owner": ALICE_REF,
                    "extra": 1,
                }
            }
        ),
        lambda p: p.update(
            {
                "org.parent": {
                    "cellId": "not-a-cell",
                    "directorySpace": "sp-p",
                    "owner": ALICE_REF,
                }
            }
        ),
        lambda p: p.update(
            {
                "org.parent": {
                    "cellId": keyed_cell_id("org", ALICE, SPACE_ID),
                    "directorySpace": "sp-p",
                    "owner": ALICE_REF,
                }
            }
        ),
        lambda p: p.update(
            {
                "org.parent": {
                    "cellId": PARENT_CELL_ID,
                    "directorySpace": SPACE_ID,
                    "owner": ALICE_REF,
                }
            }
        ),
        lambda p: p.update(
            {
                "org.parent": {
                    "cellId": PARENT_CELL_ID,
                    "directorySpace": "sp-p",
                    "owner": "alice",
                }
            }
        ),
        lambda p: p.update({"delivery.bodyRetentionMs": 3_600_000}),
        lambda p: p.update({"agent.custody": "external"}),
        lambda p: p.update({"threads.policy": {"maxK": 2}}),
        lambda p: p.update({"org.inviteToken": "tok-secret"}),
        lambda p: p.update({"org.address": "10.0.0.1:443"}),
        lambda p: p.update({"org.members": {}}),
        lambda p: p["org.members"].update(
            {
                BOB_REF: {
                    "username": "bob@example.com",
                    "joinedAtMs": 1_763_700_000_000,
                    "invitedBy": ALICE_REF,
                    "devices": [
                        {
                            "deviceUid": DEVICE_B,
                            "keyId": None,
                            "publicKey": None,
                            "verifiedAtMs": 1_763_700_000_000,
                        }
                    ],
                }
            }
        ),
        lambda p: p["org.members"][ALICE_REF].update({"username": ""}),
        lambda p: p["org.members"][ALICE_REF].update({"username": "bad name/slash"}),
        lambda p: p["org.members"][ALICE_REF].update({"joinedAtMs": True}),
        lambda p: p["org.members"][ALICE_REF].update({"joinedAtMs": -1}),
        lambda p: p["org.members"][ALICE_REF].update({"invitedBy": "alice"}),
        lambda p: p["org.members"][ALICE_REF].update({"devices": []}),
        lambda p: p["org.members"][ALICE_REF].update({"address": "10.0.0.1"}),
        lambda p: p["org.members"][ALICE_REF]["devices"][0].update({"deviceUid": "bad"}),
        lambda p: p["org.members"][ALICE_REF]["devices"][0].update({"keyId": "k1", "publicKey": None}),
        lambda p: p["org.members"][ALICE_REF]["devices"][0].update({"keyId": None, "publicKey": "pk"}),
        lambda p: p["org.members"][ALICE_REF]["devices"][0].update({"keyId": "", "publicKey": "pk"}),
        lambda p: p["org.members"][ALICE_REF]["devices"][0].update({"verifiedAtMs": False}),
        lambda p: p["org.members"][ALICE_REF]["devices"][0].update({"tailnetPublicKey": "pk"}),
        lambda p: p["org.members"][ALICE_REF]["devices"].append(
            dict(p["org.members"][ALICE_REF]["devices"][0])
        ),
    ],
)
def test_invalid_org_params_are_rejected_on_encode_and_decode(mutate_params: Any) -> None:
    params = _valid_org_params()
    mutate_params(params)
    cell = _valid_org_cell(params=params)
    with pytest.raises(CellError) as raised:
        encode(cell)
    assert raised.value.code == "invalid-cell"

    raw = json.loads(encode(_valid_org_cell()))
    raw_params = _valid_org_params()
    mutate_params(raw_params)
    raw["params"] = raw_params
    with pytest.raises(CellError) as dec_raised:
        from_document(raw)
    assert dec_raised.value.code == "invalid-cell"


def test_duplicate_username_across_different_uids_is_rejected() -> None:
    params = _valid_org_params()
    params["org.members"][BOB_REF] = {
        "username": "alice@example.com",
        "joinedAtMs": 1_763_700_000_000,
        "invitedBy": ALICE_REF,
        "devices": [
            {
                "deviceUid": DEVICE_B,
                "keyId": None,
                "publicKey": None,
                "verifiedAtMs": 1_763_700_000_000,
            }
        ],
    }
    cell = _valid_org_cell(
        members=(CellMember(ALICE_REF, "owner"), CellMember(BOB_REF, "member")),
        params=params,
    )
    with pytest.raises(CellError) as raised:
        encode(cell)
    assert raised.value.code == "invalid-cell"


def test_duplicate_device_uid_across_different_members_is_rejected() -> None:
    params = _valid_org_params()
    params["org.members"][BOB_REF] = {
        "username": "bob@example.com",
        "joinedAtMs": 1_763_700_000_500,
        "invitedBy": ALICE_REF,
        "devices": [
            {
                "deviceUid": DEVICE_A,
                "keyId": None,
                "publicKey": None,
                "verifiedAtMs": 1_763_700_000_500,
            }
        ],
    }
    cell = _valid_org_cell(
        members=(CellMember(ALICE_REF, "owner"), CellMember(BOB_REF, "member")),
        params=params,
    )
    with pytest.raises(CellError) as enc_raised:
        encode(cell)
    assert enc_raised.value.code == "invalid-cell"

    raw = json.loads(encode(_valid_org_cell(members=(CellMember(ALICE_REF, "owner"),))))
    raw["members"].append({"ref": BOB_REF, "role": "member"})
    raw["params"] = params
    with pytest.raises(CellError) as dec_raised:
        decode(json.dumps(raw))
    assert dec_raised.value.code == "invalid-cell"


@pytest.mark.parametrize("second_role", ["owner", "admin"])
def test_duplicate_raw_org_member_refs_are_rejected_without_silent_dedup(
    second_role: str,
) -> None:
    raw = json.loads(encode(_valid_org_cell()))
    raw["members"].append({"ref": ALICE_REF, "role": second_role})
    with pytest.raises(CellError) as dec_raised:
        decode(json.dumps(raw))
    assert dec_raised.value.code == "invalid-cell"
    with pytest.raises(CellError) as doc_raised:
        from_document(raw)
    assert doc_raised.value.code == "invalid-cell"


def test_agent_cell_rejects_org_params_on_encode_and_decode() -> None:
    agent = Cell(
        cell_id=subject_cell_id("agent", AGENT_ID),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(AGENT_ID),
        params={"org.name": ORG_NAME},
    )
    with pytest.raises(CellError) as raised:
        encode(agent)
    assert raised.value.code == "invalid-cell"
    with pytest.raises(CellError) as doc_raised:
        to_document(agent)
    assert doc_raised.value.code == "invalid-cell"

    clean = Cell(
        cell_id=subject_cell_id("agent", AGENT_ID),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(AGENT_ID),
    )
    raw = json.loads(encode(clean))
    raw["params"] = {"org.tailnet": SPACE_ID}
    with pytest.raises(CellError) as dec_raised:
        decode(json.dumps(raw))
    assert dec_raised.value.code == "invalid-cell"


def test_sub_org_rejects_own_directory_space_as_tailnet() -> None:
    """Child org must inherit parent tailnet rather than binding org.tailnet to itself."""
    parent = {"cellId": PARENT_CELL_ID, "directorySpace": "sp-parent", "owner": ALICE_REF}
    params = _valid_org_params(
        **{
            "org.parent": parent,
            "org.tailnet": SPACE_ID,
        }
    )
    cell = _valid_org_cell(params=params)
    with pytest.raises(CellError) as raised:
        encode(cell)
    assert raised.value.code == "invalid-cell"
