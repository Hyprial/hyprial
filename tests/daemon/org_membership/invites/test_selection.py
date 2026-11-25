"""O2c: the daemon's invite decisions (``org.invites.list/accept``).

The account snapshot is the caller's (the credential never leaves the
shell, T11); everything after it is decided here and nothing is joined
outside the org network service ``org.join`` already uses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.org.membership.invites import (
    InviteService,
    account_state,
    snapshot_views,
)
from hyprial.daemon.impl.org.membership.invites.service import _accepted
from hyprial.daemon.impl.org.network import ipc as org_ipc
from hyprial.daemon.impl.request_catalog import (
    ORG_INVITES_ACCEPT_SPEC,
    ORG_INVITES_LIST_SPEC,
    METHOD_REGISTRY,
)
from hyprial.daemon.impl.request_catalog.migration import (
    MIGRATED_FAMILIES,
    MIGRATED_METHODS,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import (
    InviteError,
    InviteLink,
    bootstrap_yaml,
    default_bootstrap,
    encode_invite,
    invite_identifier,
)

DEVICE_ID = "node-a"


def _invite(
    org: str = "group-acme",
    *,
    inviter: str = "bob",
    hours: int = 24,
) -> tuple[str, dict[str, Any]]:
    """(payload, link fields) for one real, decodable invite."""

    workflow = bootstrap_yaml(default_bootstrap())
    link = InviteLink(
        org=org,
        space_id=f"space-{org}",
        acl_space_id=f"space-{org}-acl",
        inviter=inviter,
        inviter_device="node-b",
        inviter_address="tcp://10.0.0.1:7447#psk",
        token="t" * 32,
        workflow_yaml=workflow,
        workflow_sha256=hashlib.sha256(workflow.encode()).hexdigest(),
        expires_at=(datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
        tailnet={
            "version": 2,
            "tailnetId": f"space-{org}",
            "inviteId": f"invite-{org}",
            "token": "t" * 43,
            "owner": "alice",
            "expiresAtMs": int(
                (datetime.now(UTC) + timedelta(hours=hours)).timestamp() * 1000
            ),
            "authority": {
                "owner": inviter,
                "deviceId": "node-b",
                "masterPublic": "nodekey:" + "a" * 64,
                "serverPublic": "nodekey:" + "b" * 64,
                "address": "tcp://10.0.0.1:7447#psk",
                "controlPort": 7448,
            },
        },
    )
    encoded = encode_invite(link)
    payload = encoded.removeprefix("hyprial-invite:v2:")
    return payload, {
        "org": org,
        "spaceId": link.space_id,
        "inviter": inviter,
        "link": encoded,
        "id": invite_identifier(payload),
    }


def _row(payload: str, accepted_at: str | None = "2026-10-31T00:00:00Z") -> dict:
    return {"payload": payload, "acceptedAt": accepted_at}


class _Orgs:
    """The org network service's two relevant methods, recorded."""

    def __init__(
        self,
        *,
        held: tuple[str, ...] = (),
        left: tuple[str, ...] = (),
        failure: BaseException | None = None,
    ) -> None:
        self.held = held
        self.left = left
        self.failure = failure
        self.joins: list[str] = []

    def list(self) -> dict[str, Any]:
        return {
            "orgs": [
                {
                    "org": f"group-{index}",
                    "spaceId": space,
                    "role": "member",
                    "devices": 1,
                    "state": "active",
                }
                for index, space in enumerate(self.held)
            ],
            "leftOrgs": list(self.left),
        }

    def invite_missing_step(self, link: str) -> str | None:
        from hyprial.identity import decode_invite
        return None if decode_invite(link).space_id in self.held else "directory-committed"

    def join(self, link: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        self.joins.append(link)
        return {
            "org": "group-joined",
            "spaceId": "space-joined",
            "steps": [{"kind": "join-org", "ok": True, "detail": "joined"}],
        }


def _recorder() -> tuple[list[tuple[str, str, dict[str, Any]]], Any]:
    events: list[tuple[str, str, dict[str, Any]]] = []

    def log(level: str, domain: str, event: str, **fields: Any) -> None:
        events.append((level, event, fields))

    return events, log


# -- org.invites.list ----------------------------------------------------------


def test_list_offers_one_candidate_per_space_and_carries_no_payload() -> None:
    first, _acme = _invite("group-acme")
    second, _beta = _invite("group-beta")
    events, log = _recorder()
    service = InviteService(
        orgs=_Orgs(held=("space-group-held",), left=("group-left",)), log=log
    )

    result = service.list(
        [
            _row(first),
            _row(second),
            _row(first, "2026-10-31T01:00:00Z"),  # same space, second row
            _row(_invite("group-held")[0]),
            _row(_invite("group-left")[0]),
        ]
    )

    assert result["ok"] is True
    assert [row["org"] for row in result["pending"]] == ["group-acme", "group-beta"]
    assert {row["org"] for row in result["pending"]} == {"group-acme", "group-beta"}
    assert result["settled"] == 2
    assert result["duplicates"] == 1
    assert result["warnings"] == []
    # The payload is a bearer secret: it is never echoed back.
    rendered = json.dumps(result)
    assert "payload" not in rendered
    assert first not in rendered and second not in rendered
    assert [event for _level, event, _fields in events] == ["org.invite.pending"]
    assert events[0][2] == {"count": 2, "orgs": ["group-acme", "group-beta"]}


def test_list_stays_silent_when_the_account_holds_nothing_pending() -> None:
    events, log = _recorder()
    service = InviteService(
        orgs=_Orgs(held=("space-group-acme",)), log=log
    )

    result = service.list([_row(_invite("group-acme")[0])])

    assert result["pending"] == []
    assert result["settled"] == 1
    assert events == []


def test_list_refuses_a_malformed_account_snapshot_whole() -> None:
    service = InviteService(orgs=_Orgs())

    with pytest.raises(ValueError, match="array"):
        service.list({"payload": "x"})
    with pytest.raises(ValueError, match="payload"):
        service.list([{"acceptedAt": "t"}])
    with pytest.raises(ValueError, match="payload"):
        service.list([{"payload": ""}])


def test_an_unreadable_payload_stays_a_candidate_and_keeps_its_own_identity() -> None:
    service = InviteService(orgs=_Orgs())

    result = service.list([_row("not-base64-json"), _row("also-not-json")])

    ids = [row["inviteId"] for row in result["pending"]]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert {row["org"] for row in result["pending"]} == {"unknown"}


def test_a_row_accepted_twice_is_one_candidate() -> None:
    payload, invited = _invite("group-acme")
    service = InviteService(orgs=_Orgs())

    result = service.list([_row(payload), _row(payload, "2026-10-31T02:00:00Z")])

    assert [row["inviteId"] for row in result["pending"]] == [invited["id"]]


def test_left_orgs_never_come_back_through_an_old_accepted_invite() -> None:
    service = InviteService(orgs=_Orgs(left=("group-left",)))

    result = service.list([_row(_invite("group-left")[0])])

    assert result["pending"] == []
    assert result["settled"] == 1


# -- org.invites.accept --------------------------------------------------------


def test_accept_joins_exactly_the_confirmed_invite() -> None:
    payload, invited = _invite("group-acme")
    orgs = _Orgs()
    service = InviteService(orgs=orgs)

    result = service.accept(invite_id=invited["id"], link=invited["link"])

    assert result["ok"] is True
    assert result["inviteId"] == invited["id"]
    assert result["org"] == "group-acme"
    assert result["spaceId"] == "space-group-acme"
    assert result["joined"] is True
    assert result["alreadyMember"] is False
    assert [step["kind"] for step in result["steps"]] == ["join-org"]
    assert orgs.joins == [invited["link"]]
    assert payload not in json.dumps(result)


def test_accept_is_idempotent_for_a_space_this_node_already_holds() -> None:
    _payload, invited = _invite("group-acme")
    orgs = _Orgs(held=("space-group-acme",))
    service = InviteService(orgs=orgs)

    result = service.accept(invite_id=invited["id"], link=invited["link"])

    assert result["joined"] is False
    assert result["alreadyMember"] is True
    assert result["steps"] == []
    assert orgs.joins == []


def test_accept_refuses_a_confirmation_that_is_not_this_invite() -> None:
    _payload, acme = _invite("group-acme")
    _other, beta = _invite("group-beta")
    orgs = _Orgs()
    service = InviteService(orgs=orgs)

    with pytest.raises(ValueError, match="does not match"):
        service.accept(invite_id=beta["id"], link=acme["link"])

    assert orgs.joins == []


def test_accept_keeps_an_expired_invite_expired() -> None:
    _payload, expired = _invite("group-acme", hours=-1)
    service = InviteService(orgs=_Orgs())

    with pytest.raises(InviteError) as raised:
        service.accept(invite_id=expired["id"], link=expired["link"])

    assert raised.value.code == "INVITE_EXPIRED"


def test_accept_lets_a_join_failure_carry_its_own_type() -> None:
    _payload, invited = _invite("group-acme")
    service = InviteService(
        orgs=_Orgs(failure=OrgFsError("no-holder-online", {"spaceId": "s"}))
    )

    with pytest.raises(OrgFsError) as raised:
        service.accept(invite_id=invited["id"], link=invited["link"])

    assert raised.value.code == "no-holder-online"


# -- the IPC boundary ---------------------------------------------------------


class _App:
    hyprial_home = None

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> _Orgs:
    orgs = _Orgs()
    monkeypatch.setattr(org_ipc, "_request_service", lambda app: orgs)
    monkeypatch.setattr(
        org_ipc, "_accept_and_settle", lambda app, link: orgs.join(link)
    )
    return orgs


def test_the_ipc_list_handler_answers_the_declared_shape(wired: _Orgs) -> None:
    payload, invited = _invite("group-acme")

    result = org_ipc.ipc_org_invites_list(_App(), {"invites": [_row(payload)]})

    assert result["pending"][0]["inviteId"] == invited["id"]
    # The registry re-validates params and result against the declaration.
    assert (
        METHOD_REGISTRY.invoke(
            _App(), "org.invites.list", {"invites": [_row(payload)]}
        )["pending"][0]["org"]
        == "group-acme"
    )


def test_the_ipc_accept_handler_maps_every_failure_onto_a_declared_code(
    wired: _Orgs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _payload, invited = _invite("group-acme")
    from hyprial.kernel import DaemonRequestError

    with pytest.raises(DaemonRequestError) as mismatch:
        org_ipc.ipc_org_invites_accept(
            _App(), {"inviteId": "inv-wrong", "link": invited["link"]}
        )
    assert mismatch.value.code == "INVALID_ARGUMENT"

    monkeypatch.setattr(
        org_ipc,
        "_accept_and_settle",
        lambda app, link: (_ for _ in ()).throw(
            OrgFsError("no-holder-online", {"spaceId": "s"})
        ),
    )
    with pytest.raises(DaemonRequestError) as network:
        org_ipc.ipc_org_invites_accept(
            _App(), {"inviteId": invited["id"], "link": invited["link"]}
        )
    assert network.value.code == "no-holder-online"

    _expired_payload, expired = _invite("group-expired", hours=-1)
    with pytest.raises(DaemonRequestError) as expired_code:
        org_ipc.ipc_org_invites_accept(
            _App(), {"inviteId": expired["id"], "link": expired["link"]}
        )
    assert expired_code.value.code == "INVITE_EXPIRED"


def test_the_declared_codes_cover_the_handler_errors() -> None:
    for spec in (ORG_INVITES_LIST_SPEC, ORG_INVITES_ACCEPT_SPEC):
        codes = {error.code for error in spec.errors}
        assert {"INVALID_ARGUMENT", "DAEMON_NOT_READY", "ORG_NETWORK_ERROR"} <= codes
        assert spec.idempotency is not None
        assert spec.params_schema["additionalProperties"] is False
        if spec.mutates:
            assert spec.http_exposed is False
        else:
            assert spec.http_exposed is True
    assert "INVITE_EXPIRED" in {error.code for error in ORG_INVITES_ACCEPT_SPEC.errors}


def test_org_invites_is_a_precise_migration_not_a_family_marker() -> None:
    assert {"org.invites.list", "org.invites.accept"} <= MIGRATED_METHODS
    # C17: the org family still has legacy members (org.acl.*).
    assert "org" not in MIGRATED_FAMILIES


def test_account_state_never_infers_membership_from_space_listing() -> None:
    held, left = account_state(
        {"orgs": [{"spaceId": "s1"}, {"org": "no-space"}], "leftOrgs": ["a", 3]}
    )

    assert held == set()
    assert left == {"a"}


def test_the_accepted_row_never_names_a_payload() -> None:
    _payload, invited = _invite("group-acme")
    row = _accepted(
        invited=type(
            "I", (), {"org": "group-acme", "space_id": "space-group-acme"}
        )(),
        identifier=invited["id"],
        joined=True,
        steps=[],
    )

    assert set(row) == {
        "ok",
        "inviteId",
        "org",
        "spaceId",
        "joined",
        "alreadyMember",
        "steps",
    }


def test_snapshot_views_refuses_a_non_array_snapshot() -> None:
    with pytest.raises(ValueError, match="array"):
        snapshot_views("not-a-list")


# -- the frozen contract fixture ----------------------------------------------


FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "contract"
    / "fixtures"
    / "org-membership"
    / "invites.json"
)


def test_the_fixture_examples_still_validate_against_the_declarations() -> None:
    """The committed contract examples are schema-checked, not decoration."""

    from jsonschema import Draft202012Validator

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    specs = {spec.name: spec for spec in METHOD_REGISTRY.specs}
    for name, examples in fixture["methods"].items():
        spec = specs[name]
        params = Draft202012Validator(dict(spec.params_schema))
        result = Draft202012Validator(dict(spec.result_schema))
        params.validate(examples["params"])
        result.validate(examples["result"])
        if "alreadyMemberResult" in examples:
            result.validate(examples["alreadyMemberResult"])
        declared = {error.code for error in spec.errors}
        assert set(fixture["errors"][name]) <= declared, name
        if "partialError" in examples:
            error = examples["partialError"]
            declaration = next(item for item in spec.errors if item.code == error["code"])
            from hyprial.daemon.impl.request_catalog.registry import _plain
            Draft202012Validator(_plain(declaration.data_schema)).validate(error["data"])
    assert fixture["events"]["org.invite.pending"]["never"] == ["payload", "token"]
