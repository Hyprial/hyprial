"""run_pending as a request adapter: real account read, fake daemon (§3 O2c).

The Casdoor client and its urllib transport stay real (a fake server on
loopback); the daemon is a fake that answers the documented
``org.invites.list`` / ``org.invites.accept`` contract.  The rules
themselves (settled, left, duplicates, id binding) are the daemon's and are
tested in ``tests/daemon/org_membership/invites``; what is pinned here is

* the credential and the account read stay shell-side (T11),
* the pickup calls the two declared methods and no other join surface,
* the result/JSON never carries the payload or a token,
* a decline stays local: the account property is never written.
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
    invite_views,
    select_invites,
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


class _Daemon:
    """A daemon that answers the declared contract over the shared rules."""

    def __init__(
        self,
        *,
        held: tuple[str, ...] = (),
        left: tuple[str, ...] = (),
        accept_error: BaseException | None = None,
    ) -> None:
        self.held = held
        self.left = left
        self.accept_error = accept_error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "org.invites.list":
            selection = select_invites(
                invite_views(params["invites"]),
                held_spaces=self.held,
                left_orgs=self.left,
            )
            return {
                "ok": True,
                "pending": selection.pending_rows(),
                "settled": len(selection.settled),
                "duplicates": len(selection.duplicates),
                "warnings": [],
            }
        if method == "org.invites.accept":
            if self.accept_error is not None:
                raise self.accept_error
            invited = decode_invite(params["link"])
            return {
                "ok": True,
                "inviteId": params["inviteId"],
                "org": invited.org,
                "spaceId": invited.space_id,
                "joined": True,
                "alreadyMember": False,
                "steps": [],
            }
        raise AssertionError(f"unexpected daemon method {method}")

    @property
    def accepted_links(self) -> list[str]:
        return [
            params["link"]
            for method, params in self.calls
            if method == "org.invites.accept"
        ]

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]


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
        "request": _Daemon(),
        "token_source": lambda issuer: "access-token-1",
    }
    defaults.update(kwargs)
    return run_pending(home, **defaults)


def test_partial_accept_reports_missing_step_without_claiming_join(home: Path) -> None:
    from hyprial.kernel import DaemonRequestError

    error = DaemonRequestError("no-holder-online", "secret transport text", {
        "status": "partial", "missingStep": "acl-applied", "payload": "secret",
    })
    with FakeCasdoor() as server:
        server.set_invites_property([{"payload": _payload(), "acceptedAt": "t1"}])
        result = _run(home, server, request=_Daemon(accept_error=error))
    assert result["joined"] == []
    assert result["failed"][0]["status"] == "partial"
    assert result["failed"][0]["missingStep"] == "acl-applied"
    assert "secret" not in json.dumps(result)


def test_accept_all_joins_each_pending_invite_through_the_daemon(
    home: Path,
) -> None:
    payload = _payload()
    daemon = _Daemon()
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": payload, "acceptedAt": "2026-10-03T01:00:00Z"}]
        )
        result = _run(home, server, request=daemon)

        assert [row["org"] for row in result["joined"]] == ["group-acme"]
        assert result["declined"] == [] and result["failed"] == []
        assert result["skipped"] == 0
        assert daemon.methods == ["org.invites.list", "org.invites.accept"]
        assert daemon.accepted_links == [f"v2.{payload}"]
        assert decode_invite(daemon.accepted_links[0]).org == "group-acme"
        # The account property is read-only here: declining is local and
        # accepting is the daemon's join, never an account write.
        assert server.update_calls == []
    # Neither the payload (bearer secret) nor a token lands in the result.
    rendered = json.dumps(result)
    assert payload not in rendered
    assert "access-token-1" not in rendered
    assert "refresh-token-1" not in rendered


def test_an_org_this_node_already_holds_is_settled_not_rejoined(
    home: Path,
) -> None:
    daemon = _Daemon(held=("space-group-acme",))
    with FakeCasdoor() as server:
        server.set_invites_property(
            [
                {"payload": _payload("group-acme"), "acceptedAt": "t1"},
                {"payload": _payload("group-other"), "acceptedAt": "t2"},
            ]
        )
        result = _run(home, server, request=daemon)

        assert result["skipped"] == 1
        assert [row["org"] for row in result["joined"]] == ["group-other"]
        assert [decode_invite(link).org for link in daemon.accepted_links] == [
            "group-other"
        ]
        assert server.update_calls == []


def test_a_left_org_is_never_rejoined_by_an_old_accepted_invite(
    home: Path,
) -> None:
    daemon = _Daemon(left=("group-acme",))
    with FakeCasdoor() as server:
        server.set_invites_property([{"payload": _payload(), "acceptedAt": "t1"}])
        for accept_all in (True, False):
            result = _run(home, server, request=daemon, accept_all=accept_all)
            assert result["skipped"] == 1
            assert result["joined"] == []
            assert result["pending"] == []
    assert daemon.accepted_links == []


def test_the_same_space_accepted_twice_is_joined_once(home: Path) -> None:
    daemon = _Daemon()
    with FakeCasdoor() as server:
        server.set_invites_property(
            [
                {"payload": _payload(), "acceptedAt": "t1"},
                {"payload": _payload(inviter="carol"), "acceptedAt": "t2"},
            ]
        )
        result = _run(home, server, request=daemon)

    assert len(daemon.accepted_links) == 1
    assert result["skipped"] == 1


def test_an_unreachable_daemon_reports_failure_per_invite(home: Path) -> None:
    class Down(RuntimeError):
        code = "DAEMON_UNAVAILABLE"

    def down(*_args: object) -> object:
        raise Down("no daemon")

    with FakeCasdoor() as server:
        server.set_invites_property([{"payload": _payload(), "acceptedAt": "t1"}])
        with pytest.raises(Down):
            _run(home, server, request=down)


def test_no_acceptance_lists_pending_and_joins_nothing(home: Path) -> None:
    daemon = _Daemon()
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": _payload(), "acceptedAt": "2026-10-03T01:00:00+00:00"}]
        )
        result = _run(home, server, accept_all=False, confirm=None, request=daemon)

        assert result["joined"] == [] and result["declined"] == []
        assert [row["org"] for row in result["pending"]] == ["group-acme"]
        assert result["pending"][0]["acceptedAt"] == "2026-10-03T01:00:00+00:00"
        assert daemon.methods == ["org.invites.list"]
        assert server.update_calls == []
    assert "payload" not in json.dumps(result)


def test_confirm_decides_per_invite_and_a_decline_stays_local(
    home: Path,
) -> None:
    yes = _payload(org="group-acme")
    no = _payload(org="group-other", inviter="carol")
    asked: list[tuple[str, str]] = []
    daemon = _Daemon()
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": yes, "acceptedAt": "t1"}, {"payload": no, "acceptedAt": "t2"}]
        )

        def confirm(org: str, inviter: str) -> bool:
            asked.append((org, inviter))
            return org == "group-acme"

        result = _run(home, server, accept_all=False, confirm=confirm, request=daemon)

        assert asked == [("group-acme", "bob"), ("group-other", "carol")]
        assert [row["org"] for row in result["joined"]] == ["group-acme"]
        assert [row["org"] for row in result["declined"]] == ["group-other"]
        assert result["pending"] == []
        assert "declined" in result["declinedNote"]
        assert daemon.accepted_links == [f"v2.{yes}"]
        assert server.update_calls == []


def test_a_failed_accept_is_reported_with_its_code(home: Path) -> None:
    class JoinRefused(RuntimeError):
        code = "INVITE_EXPIRED"

    daemon = _Daemon(accept_error=JoinRefused("expired"))
    with FakeCasdoor() as server:
        server.set_invites_property([{"payload": _payload(), "acceptedAt": "t1"}])
        result = _run(home, server, request=daemon)

        assert [row["code"] for row in result["failed"]] == ["INVITE_EXPIRED"]
        assert result["joined"] == []
        assert server.update_calls == []


def test_an_account_row_without_a_payload_is_reported_not_sent(
    home: Path,
) -> None:
    daemon = _Daemon()
    payload = _payload()
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"acceptedAt": "t1"}, {"payload": payload, "acceptedAt": "t2"}]
        )
        result = _run(home, server, request=daemon)

        assert len(result["unusable"]) == 1
        assert [row["org"] for row in result["joined"]] == ["group-acme"]
        listed = next(params for method, params in daemon.calls if method.endswith("list"))
        assert listed["invites"] == [{"payload": payload, "acceptedAt": "t2"}]


def test_missing_device_record_is_device_not_ready(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with pytest.raises(InvitePickupError) as raised:
        run_pending(
            home,
            accept_all=True,
            confirm=None,
            request=_Daemon(),
            token_source=lambda issuer: "t",
        )
    assert raised.value.code == "DEVICE_NOT_READY"


def test_missing_credential_is_not_logged_in(home: Path) -> None:
    with pytest.raises(InvitePickupError) as raised:
        run_pending(
            home,
            accept_all=True,
            confirm=None,
            request=_Daemon(),
            token_source=lambda issuer: "t",
        )
    assert raised.value.code == "NOT_LOGGED_IN"


def test_malformed_invites_property_is_loud(home: Path) -> None:
    with FakeCasdoor() as server:
        server.set_invites_property("{broken")
        with pytest.raises(InvitePickupError) as raised:
            _run(home, server)
    assert raised.value.code == "INVITES_MALFORMED"
