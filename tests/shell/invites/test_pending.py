"""run_pending: the §5.3 pickup flow against a fake Casdoor (real HTTP).

The Casdoor client and its urllib transport are real; the seams that would
leave the machine are stubbed — ``token_source`` stands in for the U3
refresh (covered by the login tests), ``ipc_join``/``ipc_list`` for the
daemon.  Every test also pins the read-only contract: no update-user call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hyprial.identity import DeviceRecord, write_device_record
from hyprial.identity import (
    InviteLink,
    bootstrap_yaml,
    decode_invite,
    default_bootstrap,
    encode_invite,
)
from hyprial.shell.impl.invites.pending import InvitePickupError, run_pending
from tests.shell.invites.fakes import FakeCasdoor

DEVICE_ID = "node-a"


def _payload(org: str = "group-acme", inviter: str = "bob") -> str:
    """A real invite payload (what the landing page stores), minus scheme."""
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
        expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat(),
        tailnet={'version': 2, 'tailnetId': f'space-{org}', 'inviteId': f'invite-{org}',
                 'token': 't' * 43, 'owner': 'alice',
                 'expiresAtMs': int((datetime.now(UTC) + timedelta(hours=24)).timestamp() * 1000),
                 'authority': {'owner': inviter, 'deviceId': 'node-b',
                               'masterPublic': 'nodekey:' + 'a' * 64,
                               'serverPublic': 'nodekey:' + 'b' * 64,
                               'address': 'tcp://10.0.0.1:7447#psk', 'controlPort': 7448}},
    )
    return encode_invite(link).removeprefix("hyprial-invite:v2:")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    write_device_record(
        home,
        DeviceRecord(
            device_id=DEVICE_ID,
            owner="alice",
            server_public="nodekey:server-a",
            client_public="nodekey:client-a",
        ),
    )
    return home


def _credential(home: Path, issuer: str) -> None:
    secrets_dir = home / "secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    (secrets_dir / "login.json").write_text(
        json.dumps(
            {
                "version": 1,
                "issuer": issuer,
                "refreshToken": "refresh-token-1",
                "obtainedAt": "2026-10-03T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _run(home: Path, server: FakeCasdoor, **kwargs):
    _credential(home, server.url)
    defaults = {
        "accept_all": True,
        "confirm": None,
        "ipc_join": lambda link: {"org": "joined"},
        "ipc_list": lambda: {"orgs": [], "leftOrgs": []},
        "token_source": lambda issuer: "access-token-1",
    }
    defaults.update(kwargs)
    return run_pending(home, **defaults)


def test_accept_all_joins_and_never_writes_to_casdoor(home: Path) -> None:
    payload = _payload()
    joins: list[str] = []
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": payload, "acceptedAt": "2026-10-03T01:00:00Z"}]
        )
        result = _run(
            home,
            server,
            ipc_join=lambda link: joins.append(link) or {"org": "group-acme"},
        )

        assert result == {
            "ok": True,
            "joined": [{"org": "group-acme", "inviter": "bob"}],
            "failed": [],
            "skipped": 0,
            "pending": [],
            "warnings": [],
        }
        assert joins == [f"v2.{payload}"]
        # The join's link is a real, decodable invite form.
        assert decode_invite(joins[0]).org == "group-acme"
        assert server.update_calls == []
    # The payload (bearer secret) never lands in the result object.
    assert payload not in json.dumps(result)


def test_an_org_this_node_already_holds_is_skipped(home: Path) -> None:
    # The 2026-10-04 live run: after joining, the next pickup listed the
    # same invite again because the Casdoor write-back was refused.
    joins: list[str] = []
    with FakeCasdoor() as server:
        server.set_invites_property(
            [
                {"payload": _payload(org="group-acme"), "acceptedAt": "t1"},
                {"payload": _payload(org="group-other"), "acceptedAt": "t2"},
            ]
        )
        result = _run(
            home,
            server,
            ipc_join=lambda link: joins.append(link),
            ipc_list=lambda: {
                "orgs": [{"org": "group-acme", "spaceId": "space-group-acme"}],
                "leftOrgs": [],
            },
        )

        assert result["skipped"] == 1
        assert result["joined"] == [{"org": "group-other", "inviter": "bob"}]
        assert [decode_invite(link).org for link in joins] == ["group-other"]
        assert server.update_calls == []


def test_a_left_org_is_never_rejoined_by_an_old_accepted_invite(home: Path) -> None:
    joins: list[str] = []
    with FakeCasdoor() as server:
        server.set_invites_property([{"payload": _payload(), "acceptedAt": "t1"}])
        for accept_all in (True, False):
            result = _run(
                home,
                server,
                accept_all=accept_all,
                ipc_join=lambda link: joins.append(link),
                ipc_list=lambda: {"orgs": [], "leftOrgs": ["group-acme"]},
            )
            assert result["skipped"] == 1
            assert result["joined"] == []
            assert result["pending"] == []
    assert joins == []


def test_the_same_space_accepted_twice_is_joined_once(home: Path) -> None:
    joins: list[str] = []
    with FakeCasdoor() as server:
        server.set_invites_property(
            [
                {"payload": _payload(), "acceptedAt": "t1"},
                {"payload": _payload(inviter="carol"), "acceptedAt": "t2"},
            ]
        )
        result = _run(home, server, ipc_join=lambda link: joins.append(link))

    assert len(joins) == 1
    assert result["skipped"] == 1


def test_an_unreachable_daemon_still_attempts_and_reports_per_invite(
    home: Path,
) -> None:
    class Down(RuntimeError):
        code = "DAEMON_UNAVAILABLE"

    def down(*_args: object) -> object:
        raise Down("no daemon")

    with FakeCasdoor() as server:
        server.set_invites_property([{"payload": _payload(), "acceptedAt": "t1"}])
        result = _run(home, server, ipc_join=down, ipc_list=down)

    assert result["failed"] == [{"org": "group-acme", "code": "DAEMON_UNAVAILABLE"}]
    assert result["warnings"] == ["org state unavailable (DAEMON_UNAVAILABLE)"]


def test_no_acceptance_lists_pending_and_writes_nothing(home: Path) -> None:
    payload = _payload()
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": payload, "acceptedAt": "2026-10-03T01:00:00+00:00"}]
        )
        joins: list[str] = []
        result = _run(
            home,
            server,
            accept_all=False,
            confirm=None,
            ipc_join=lambda link: joins.append(link),
        )

        assert result["joined"] == []
        assert result["pending"] == [
            {
                "org": "group-acme",
                "inviter": "bob",
                "acceptedAt": "2026-10-03T01:00:00+00:00",
            }
        ]
        assert joins == []
        assert server.update_calls == []


def test_confirm_decides_per_invite(home: Path) -> None:
    yes = _payload(org="group-acme")
    no = _payload(org="group-other", inviter="carol")
    asked: list[tuple[str, str]] = []
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": yes, "acceptedAt": "t1"}, {"payload": no, "acceptedAt": "t2"}]
        )

        def confirm(org: str, inviter: str) -> bool:
            asked.append((org, inviter))
            return org == "group-acme"

        result = _run(home, server, accept_all=False, confirm=confirm)

        assert asked == [("group-acme", "bob"), ("group-other", "carol")]
        assert result["joined"] == [{"org": "group-acme", "inviter": "bob"}]
        assert result["pending"] == [
            {"org": "group-other", "inviter": "carol", "acceptedAt": "t2"}
        ]
        assert server.update_calls == []


def test_a_failed_join_is_reported(home: Path) -> None:
    class JoinRefused(RuntimeError):
        code = "INVITE_EXPIRED"

    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": _payload(), "acceptedAt": "t1"}]
        )
        result = _run(
            home,
            server,
            ipc_join=lambda link: (_ for _ in ()).throw(JoinRefused("expired")),
        )

        assert result["failed"] == [{"org": "group-acme", "code": "INVITE_EXPIRED"}]
        assert result["joined"] == []
        assert server.update_calls == []


def test_missing_device_record_is_device_not_ready(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with pytest.raises(InvitePickupError) as raised:
        run_pending(
            home,
            accept_all=True,
            confirm=None,
            ipc_join=lambda link: None,
            ipc_list=lambda: {"orgs": [], "leftOrgs": []},
            token_source=lambda issuer: "t",
        )
    assert raised.value.code == "DEVICE_NOT_READY"


def test_missing_credential_is_not_logged_in(home: Path) -> None:
    with pytest.raises(InvitePickupError) as raised:
        run_pending(
            home,
            accept_all=True,
            confirm=None,
            ipc_join=lambda link: None,
            ipc_list=lambda: {"orgs": [], "leftOrgs": []},
            token_source=lambda issuer: "t",
        )
    assert raised.value.code == "NOT_LOGGED_IN"


def test_malformed_invites_property_is_loud(home: Path) -> None:
    with FakeCasdoor() as server:
        server.set_invites_property("{broken")
        with pytest.raises(InvitePickupError) as raised:
            _run(home, server)
    assert raised.value.code == "INVITES_MALFORMED"
