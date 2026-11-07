"""OrgNetworkService: create/invite/execute/network over a recording FS.

The OrgFS integration itself is covered by test_org_directory_store.py
against a real runtime; here the facade is an in-memory recording fake so
the service's own logic — workflow step order, join retry, peer
exclusion, address secrecy — is what's under test.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.tailnet_membership import confirm_removal

from hyprial.daemon.impl.orgfs.api import OrgFsError, SpaceStatus
from hyprial.kernel import ORGFS_CONTENT_PENDING
from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.identity.impl.org_directory import (
    DIRECTORY_DIR,
    INVITES_DIR,
    ORG_META_DOC,
    InviteError,
    decode_invite,
    default_role_grants_document,
    encode_invite,
    protected_directory_doc_id,
)

from tests.support.tailnet_membership import FakeMembershipSidecar, key, member

OWNER = "owner@example"
INVITEE_OWNER = "bob@example"
NODE_ID = "node-a"
INVITER_ADDRESS = "tcp://10.0.0.1:7447#inviter-psk"
OWN_ADDRESS = "tcp://10.0.0.2:7447#own-psk"
TEST_NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _invite_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep decode expiry checks on the same clock as the service fixtures."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return TEST_NOW.replace(tzinfo=None)
            return TEST_NOW.astimezone(tz)

    monkeypatch.setattr(
        "hyprial.identity.impl.org_directory.invite.datetime", FrozenDateTime
    )


@dataclass
class FakeFs:
    """The OrgFS surface the service uses, in memory, recording every call."""

    calls: list[str] = field(default_factory=list)
    space_ids: dict[str, str] = field(default_factory=dict)  # name -> space_id
    metadata: dict[str, dict[str, object]] = field(default_factory=dict)
    docs: dict[tuple[str, str], str] = field(default_factory=dict)  # (space, path)
    dirs: set[tuple[str, str]] = field(default_factory=set)
    invited: list[tuple[str, str, str]] = field(default_factory=list)
    join_failures_left: int = 0
    content_pending: set[str] = field(default_factory=set)  # device file names
    parked: tuple[tuple[str, int], ...] = ()

    def create_space(self, name: str) -> Any:
        self.calls.append(f"create_space:{name}")
        if name in self.space_ids:
            raise OrgFsError("invalid-argument", {"message": "node already exists"})
        space_id = f"space-{name}"
        self.space_ids[name] = space_id
        self.metadata[space_id] = {
            "name": name,
            "owner": f"user:{OWNER}",
            "createdAt": "t0",
            "spaceId": space_id,
        }
        return SimpleNamespace(
            space_id=space_id, name=name, owner=f"user:{OWNER}", created_at="t0"
        )

    def spaces_listing(self) -> list[Any]:
        return [
            SimpleNamespace(
                space_id=space_id, name=name, owner=f"user:{OWNER}", created_at="t0"
            )
            for name, space_id in sorted(self.space_ids.items())
        ]

    def spaces(self) -> tuple[Any, ...]:
        return tuple(self.spaces_listing())

    def space_meta(self, space_id: str) -> dict[str, object]:
        return dict(self.metadata[space_id])

    def update_space_meta(
        self, space_id: str, values: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append(f"update_space_meta:{space_id}")
        self.metadata[space_id].update(values)
        return self.space_meta(space_id)

    def invite(self, space_id: str, user: str, mode: str = "rw") -> Any:
        self.calls.append(f"invite:{space_id}:{user}:{mode}")
        self.invited.append((space_id, user, mode))
        return SimpleNamespace(space_id=space_id, user=user, mode=mode)

    def members(self, space_id: str) -> tuple[Any, ...]:
        self.calls.append(f"members:{space_id}")
        # Mirror the real facade: the space owner is a rw member too.
        owner = next(
            info.owner for info in self.spaces_listing() if info.space_id == space_id
        )
        rows = [(space_id, owner, "rw")]
        rows.extend((s, u, mode) for s, u, mode in self.invited if s == space_id)
        return tuple(
            SimpleNamespace(
                space_id=s, user=u, mode=mode, added_by=owner, added_at="t0"
            )
            for s, u, mode in rows
        )

    def remove_member(self, space_id: str, user: str) -> None:
        self.calls.append(f"remove_member:{space_id}:{user}")
        known = {u for s, u, _mode in self.invited if s == space_id}
        owner = next(
            info.owner for info in self.spaces_listing() if info.space_id == space_id
        )
        if user == owner or user not in known:
            raise OrgFsError("invalid-argument", {"user": user})
        self.invited[:] = [
            (s, u, mode)
            for s, u, mode in self.invited
            if not (s == space_id and u == user)
        ]

    def join(self, space_id: str, *, timeout: float | None = None) -> Any:
        self.calls.append(f"join:{space_id}")
        if self.join_failures_left > 0:
            self.join_failures_left -= 1
            raise OrgFsError("no-holder-online", {"spaceId": space_id})
        return SimpleNamespace(
            space_id=space_id,
            name=next(n for n, s in self.space_ids.items() if s == space_id),
            owner=f"user:{OWNER}",
            created_at="t0",
        )

    def mkdir(self, space_id: str, path: str) -> Any:
        self.calls.append(f"mkdir:{space_id}:{path}")
        key = (space_id, path)
        if key in self.dirs:
            raise OrgFsError("invalid-argument", {"message": "node already exists"})
        self.dirs.add(key)
        return SimpleNamespace(node_id=path)

    def write_text(self, space_id: str, path: str, content: str) -> Any:
        self.calls.append(f"write:{space_id}:{path}")
        self.docs[(space_id, path)] = content
        return SimpleNamespace(node_id=f"doc-{path}")

    def remove(self, space_id: str, node: str) -> Any:
        self.calls.append(f"remove:{space_id}:{node}")
        key = (space_id, node)
        if key not in self.docs:
            raise OrgFsError("unknown-doc", {"node": node})
        del self.docs[key]

    def listdir(self, space_id: str, path: str) -> tuple[Any, ...]:
        path = path.removeprefix("id:")
        prefix = f"{path}/"
        children: dict[str, str] = {}
        for space, candidate in self.dirs:
            if space == space_id and candidate.startswith(prefix):
                relative = candidate[len(prefix) :]
                if "/" not in relative:
                    children[relative] = "dir"
        for space, candidate in self.docs:
            if space == space_id and candidate.startswith(prefix):
                relative = candidate[len(prefix) :]
                if "/" not in relative:
                    children[relative] = "doc"
        if not children and (space_id, path) not in self.dirs:
            # Mirror the real facade: listing an absent directory is
            # unknown-doc, not an empty answer.
            raise OrgFsError("unknown-doc", {"path": path})
        return tuple(
            SimpleNamespace(
                node_id=f"{path}/{name}",
                name=name,
                kind=kind,
                doc_id=(
                    protected_directory_doc_id(
                        space_id,
                        f"{path}/{name}",
                        space_owner=f"user:{OWNER}",
                    )
                    if kind == "doc"
                    else None
                ),
                version="v1" if kind == "doc" else None,
            )
            for name, kind in sorted(children.items())
        )

    def status(self, space_id: str) -> SpaceStatus:
        return SpaceStatus(space_id, 0, (), (), self.parked)

    def parked_commits(self, space_id: str) -> tuple[tuple[str, int], ...]:
        return self.parked

    def read_text(self, space_id: str, node: str) -> tuple[str, str]:
        name = node.removeprefix("id:").rsplit("/", 1)[-1]
        if name in self.content_pending:
            raise OrgFsError(ORGFS_CONTENT_PENDING, {"spaceId": space_id, "node": node})
        if (space_id, node) in self.docs:
            return self.docs[(space_id, node)], "v1"
        node_id = node.removeprefix("id:")
        if (space_id, node_id) in self.docs:
            return self.docs[(space_id, node_id)], "v1"
        for (space, path), content in self.docs.items():
            if space == space_id and path.endswith(f"/{node_id}.json"):
                return content, "v1"
        raise OrgFsError("unknown-doc", {"spaceId": space_id, "node": node})

    def stat(self, space_id: str, node: str) -> Any:
        node_id = node.removeprefix("id:")
        path = next(
            (
                candidate
                for space, candidate in self.docs
                if space == space_id and candidate == node_id
            ),
            None,
        )
        if path is None:
            raise OrgFsError("unknown-doc", {"spaceId": space_id, "node": node})
        return SimpleNamespace(
            node_id=path,
            doc_id=protected_directory_doc_id(
                space_id, path, space_owner=f"user:{OWNER}"
            ),
            kind="doc",
            name=path.rsplit("/", 1)[-1],
        )

    def document_authors(self, space_id: str, node: str) -> tuple[str, ...]:
        text, _version = self.read_text(space_id, node)
        owner = json.loads(text)["owner"]
        return (f"user:{owner.removeprefix('user:')}",)


def _create_org_spaces(fs: FakeFs, org: str = "group-acme") -> None:
    import json

    fs.create_space(org)
    fs.create_space(f"{org}-acl")
    fs.docs[(f"space-{org}-acl", ACL_POLICY_DOC)] = json.dumps(
        default_role_grants_document()
    )
    fs.metadata[f"space-{org}"]["aclSpaceId"] = f"space-{org}-acl"
    if not hasattr(fs, "membership_sidecar"):
        fs.membership_sidecar = FakeMembershipSidecar()
    invite = _tailnet_invite(f"space-{org}")
    fs.membership_sidecar.seed(
        f"space-{org}", authority=invite["authority"],
        members=[member(OWNER, "node-b", address=INVITER_ADDRESS)],
    )
    fs.membership_sidecar.tailnets[f"space-{org}"]["invites"][invite["inviteId"]] = {
        "invite": invite, "deviceId": "",
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    return tmp_path


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.ports: dict[str, int] = {"peer-b": 41001}

    def map_peer(self, peer: str, address: str) -> int:
        self.calls.append(("map-peer", peer, address))
        return self.ports.get(peer, 41000)

    def redial(self) -> None:
        self.calls.append(("redial",))


def _service(
    fs: FakeFs,
    home: Path,
    recorder: Recorder | None = None,
    **overrides: Any,
) -> OrgNetworkService:
    if not hasattr(fs, "membership_sidecar"):
        fs.membership_sidecar = FakeMembershipSidecar()
    defaults: dict[str, Any] = {
        "membership_request": fs.membership_sidecar.membership_request,
        "invalid_devices": InvalidDeviceQuarantine(),
        "map_peer": recorder.map_peer if recorder else None,
        "request_redial": recorder.redial if recorder else None,
        "derive_client_key": lambda server: "nodekey:"
        + hashlib.sha256(server.encode()).hexdigest(),
        "read_own_address": lambda: OWN_ADDRESS,
        "sleep": lambda _seconds: None,
        "now": lambda: TEST_NOW,
    }
    defaults.update(overrides)
    return OrgNetworkService(
        fs=fs,
        home=home,
        node_id=NODE_ID,
        owner=OWNER,
        **defaults,
    )


def test_create_makes_the_org_space_and_publishes_this_device(
    home: Path,
) -> None:
    fs = FakeFs()
    casdoor: list[tuple[str, str]] = []

    class _Casdoor:
        # The CasdoorGroups contract grew a probe and a writer on 2026-10-20
        # (see casdoor/client.py).  This org is reported as already a member,
        # so the create path never asks for a write -- which is the behaviour
        # this test is about.
        def member_state(self, group: str, user: str) -> str:
            casdoor.append((group, user))
            return "member"

        def require_member(self, group: str, user: str) -> None:
            casdoor.append((group, user))

        def ensure_group(self, group: str, user: str) -> dict[str, object]:
            raise AssertionError("ensure_group must not run when the owner is a member")

    service = _service(fs, home, casdoor=_Casdoor())
    result = service.create("group-acme")
    assert result["org"] == "group-acme"
    assert result["existed"] is False
    assert result["devicePublished"] is True
    # The probe answered "member", so there is one Casdoor call, not two.
    assert casdoor == [("hyprial/group-acme", f"hyprial/{OWNER}")]
    assert result["casdoorGroup"] == "verified"
    assert fs.space_ids == {
        "group-acme": "space-group-acme",
        "group-acme-acl": "space-group-acme-acl",
    }
    assert (
        fs.metadata["space-group-acme"]["aclSpaceId"]
        == "space-group-acme-acl"
    )
    lifecycle = fs.docs[("space-group-acme", ORG_META_DOC)]
    assert "aclSpaceId" not in lifecycle
    assert {
        ("space-group-acme", "directory"),
        ("space-group-acme", DIRECTORY_DIR),
        ("space-group-acme", "directory/people"),
    } <= fs.dirs
    # The device row is a real directory document with the current address.
    text = fs.docs[
        ("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}/{NODE_ID}.json")
    ]
    assert '"address":"tcp://10.0.0.2:7447#own-psk"' in text
    assert '"owner":"owner@example"' in text
    # A Casdoor failure now fails the command: tests/daemon/org/test_org_casdoor_first.py.


def test_create_is_idempotent_for_an_existing_space(home: Path) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    assert service.create("group-acme")["existed"] is False
    assert service.create("group-acme")["existed"] is True


def test_create_publish_failure_is_reported(home: Path, monkeypatch) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    publish, attempts = service._publish_into, 0

    def fail_once(org: str) -> True:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OrgNetworkError("PUBLISH_FAILED", "forced publication failure")
        return publish(org)

    monkeypatch.setattr(service, "_publish_into", fail_once)
    failed = service.create("group-acme")
    retried = service.create("group-acme")
    assert failed["published"] is failed["devicePublished"] is False
    assert retried["published"] is True
    assert set(fs.space_ids) == {"group-acme", "group-acme-acl"}


def test_create_rejects_bad_org_names(home: Path) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    for bad in ("Acme", "a b", "-acme", "a" * 64):
        with pytest.raises(ValueError):
            service.create(bad)
    assert fs.space_ids == {}


def test_invite_returns_a_link_that_decodes_and_registers_the_pending_invite(
    home: Path,
) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    result = service.invite("group-acme", f"user:{INVITEE_OWNER}")

    link = decode_invite(result["invite"])
    assert link.org == "group-acme"
    assert link.space_id == "space-group-acme"
    assert link.acl_space_id == "space-group-acme-acl"
    assert link.inviter == OWNER
    assert link.inviter_device == NODE_ID
    assert link.inviter_address == OWN_ADDRESS
    assert link.workflow_yaml.startswith("steps:")
    # hyprial.ai/join accepts only RFC 3339 ``Z`` times; ``+00:00`` was
    # rejected as a malformed link in the 2026-10-04 live run.
    for expires in (link.expires_at, result["expiresAt"]):
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", expires
        ), expires
    assert result["member"] == {
        "spaceId": "space-group-acme",
        "user": f"user:{INVITEE_OWNER}",
        "mode": "rw",
    }
    assert result["aclMember"] == {
        "spaceId": "space-group-acme-acl",
        "user": f"user:{INVITEE_OWNER}",
        "mode": "ro",
    }
    # The directory holds one pending-invite document keyed by the token
    # digest — never the raw bearer token.
    invites = [
        path
        for (_space, path) in fs.docs
        if path.startswith(f"{INVITES_DIR}/")
    ]
    assert len(invites) == 1
    assert link.token not in fs.docs[("space-group-acme", invites[0])]
    token_id = invites[0].rsplit("/", 1)[1].removesuffix(".json")
    assert f'"tokenDigest":"{token_id}"' in fs.docs[("space-group-acme", invites[0])]


def test_invite_stores_a_bare_user_name_as_the_orgfs_user_uri(home: Path) -> None:
    """The CLI passes ``--user bob``; OrgFS must record ``user:bob``.

    Regression (E2E-025): the joining daemon's OrgFS author is
    ``user:<owner>``, so a bare-name membership row matched nobody and the
    join failed with ``not-a-member``.
    """

    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    result = service.invite("group-acme", INVITEE_OWNER)

    assert ("space-group-acme", f"user:{INVITEE_OWNER}", "rw") in fs.invited
    assert ("space-group-acme-acl", f"user:{INVITEE_OWNER}", "ro") in fs.invited
    assert not any(
        space == "space-group-acme" and user == INVITEE_OWNER
        for space, user, _mode in fs.invited
    )
    assert result["member"]["user"] == f"user:{INVITEE_OWNER}"


def test_invite_requires_the_own_address(home: Path) -> None:
    fs = FakeFs()
    _service(fs, home).create("group-acme")
    service = _service(fs, home, read_own_address=lambda: None)
    with pytest.raises(OrgNetworkError) as raised:
        service.invite("group-acme", "user:bob@example")
    assert raised.value.code == "DEVICE_ADDRESS_UNAVAILABLE"


def test_invite_returns_all_three_link_forms_of_one_payload(home: Path) -> None:
    """§5.1: link (https), deepLink, invite (bare token), one payload."""
    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    result = service.invite("group-acme", f"user:{INVITEE_OWNER}")

    https = result["link"]
    assert https.startswith("https://hyprial.ai/join#v2.")
    assert result["deepLink"].startswith("hyprial://join#v2.")
    assert result["invite"].startswith("hyprial-invite:v2:")
    assert result["command"] == f"hyprial org join '{https}'"
    # One payload across the three forms.
    assert https.split("#v2.", 1)[1] == result["deepLink"].split("#v2.", 1)[1]
    assert https.split("#v2.", 1)[1] == result["invite"].split(":v2:", 1)[1]
    # Every form decodes to the same invite.
    for form in (https, result["deepLink"], result["invite"]):
        link = decode_invite(form)
        assert link.org == "group-acme"
        assert link.space_id == "space-group-acme"
        assert link.inviter == OWNER
        assert link.inviter_device == NODE_ID
        assert link.inviter_address == OWN_ADDRESS


def test_invite_https_form_uses_the_profile_invite_base_url(home: Path) -> None:
    from hyprial.daemon import NetworkProfile, write_profile

    write_profile(
        NetworkProfile(
            org="acme-net",
            issuer="https://auth.example",
            client_id="client-1",
            invite_base_url="https://invites.example",
        ),
        hyprial_home=home,
    )
    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    result = service.invite("group-acme", f"user:{INVITEE_OWNER}")

    assert result["link"].startswith("https://invites.example/join#v2.")
    assert decode_invite(result["link"]).org == "group-acme"


def _tailnet_invite(space_id="space-group-acme", inviter_device="node-b", address=INVITER_ADDRESS):
    row = member(OWNER, inviter_device, address=address)
    return {
        "version": 2, "tailnetId": space_id,
        "authority": {k: row[k] for k in (
            "owner", "deviceId", "masterPublic", "serverPublic", "address"
        )} | {"controlPort": 7448},
        "inviteId": "fixture-invite", "token": "t" * 43, "owner": OWNER,
        "expiresAtMs": int((TEST_NOW + timedelta(hours=1)).timestamp() * 1000),
    }


def _invite_link(
    org: str = "group-acme",
    space_id: str = "space-group-acme",
    acl_space_id: str = "space-group-acme-acl",
    inviter_device: str = "node-b",
    address: str = INVITER_ADDRESS,
) -> str:
    from hyprial.identity.impl.org_directory import (
        InviteLink,
        bootstrap_yaml,
        default_bootstrap,
    )
    import hashlib

    workflow = bootstrap_yaml(default_bootstrap())
    return encode_invite(
        InviteLink(
            org=org,
            space_id=space_id,
            acl_space_id=acl_space_id,
            inviter=OWNER,
            inviter_device=inviter_device,
            inviter_address=address,
            token="t" * 32,
            workflow_yaml=workflow,
            workflow_sha256=hashlib.sha256(workflow.encode()).hexdigest(),
            expires_at=(TEST_NOW + timedelta(hours=1)).isoformat(),
            tailnet=_tailnet_invite(space_id, inviter_device, address),
        )
    )


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(0)])
def test_execute_rejects_expired_invites_before_running_steps(
    home: Path, offset: timedelta
) -> None:
    link = replace(
        decode_invite(_invite_link()),
        expires_at=(TEST_NOW + offset).isoformat(),
    )
    fs = FakeFs()
    service = _service(fs, home)

    with pytest.raises(InviteError) as raised:
        service.execute(encode_invite(link))

    assert raised.value.code == "INVITE_EXPIRED"
    assert fs.calls == []


def test_execute_runs_the_steps_in_order_and_retries_the_join(
    home: Path,
) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    # The inviter's device row is already in the directory (synced).
    import json

    fs.dirs.update(
        {
            ("space-group-acme", DIRECTORY_DIR),
            ("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}"),
        }
    )
    fs.docs[
        ("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}/node-b.json")
    ] = json.dumps(
        {
            "deviceId": "node-b",
            "owner": OWNER,
            "serverPublic": "nodekey:server-b",
            "clientPublic": "nodekey:client-b",
            "clientPublicByServer": {},
            "address": INVITER_ADDRESS,
            "keyGeneration": 1,
            "updatedAt": "2026-10-03T11:00:00+00:00",
        }
    )
    # The holder only becomes visible after the inviter is mapped: the
    # first two joins report no-holder-online, the third succeeds.
    fs.join_failures_left = 2
    recorder = Recorder()
    service = _service(fs, home, recorder=recorder)

    result = service.execute(_invite_link())
    kinds = [step["kind"] for step in result["steps"]]
    assert kinds == [
        "connect-inviter",
        "join-org",
        "register-device",
        "connect-members",
    ]
    # Step order: map the inviter (and redial) before joining, join with
    # retries, register, then map the remaining members (the inviter is
    # one of them — map-peer is idempotent) and redial again.
    assert recorder.calls == [
        ("map-peer", "node-b", INVITER_ADDRESS),
        ("redial",),
        ("map-peer", "node-b", INVITER_ADDRESS),
        ("redial",),
    ]
    assert fs.calls.count("join:space-group-acme") == 3
    # Directory publication is not admission; derivation belongs to the sidecar.
    published = json.loads(
        fs.docs[
            (
                "space-group-acme",
                f"{DIRECTORY_DIR}/{OWNER}/{NODE_ID}.json",
            )
        ]
    )
    assert published["clientPublicByServer"] == {}
    assert [op for op, _ in fs.membership_sidecar.calls][:3] == [
        "membership-status", "invite-redeem", "member-announce",
    ]


def test_join_retries_typed_supplier_not_online_until_existing_deadline(
    home: Path,
) -> None:
    """F3: redial settlement is retryable without string-matching the message."""

    fs = FakeFs()
    _create_org_spaces(fs)
    attempts = 0

    def join(space_id: str, *, timeout: float | None = None) -> Any:
        nonlocal attempts
        fs.calls.append(f"join:{space_id}")
        attempts += 1
        if attempts < 3:
            raise OrgFsError(
                "not-a-member",
                {
                    "reason": "supplier-not-online",
                    "message": "localized message that must not be matched",
                },
            )
        return SimpleNamespace(space_id=space_id)

    fs.join = join  # type: ignore[method-assign]
    service = _service(fs, home)

    service._join_with_retry("space-group-acme")

    assert attempts == 3


def test_connect_inviter_uses_roster_before_directory_sync(home: Path) -> None:
    """Contract §3.2/§5: redemption precedes OrgFS; no bootstrap peer cache."""

    fs = FakeFs()
    _create_org_spaces(fs)
    # No directory row for the inviter yet -- the pre-sync reality.
    recorder = Recorder()
    service = _service(fs, home, recorder=recorder)

    service.execute(_invite_link())

    assert service.directory_peers() == {"node-b": INVITER_ADDRESS}


def test_directory_sync_cannot_override_the_roster_address(home: Path) -> None:
    import json

    fs = FakeFs()
    _create_org_spaces(fs)
    recorder = Recorder()
    service = _service(fs, home, recorder=recorder)
    service.execute(_invite_link())
    assert service.directory_peers() == {"node-b": INVITER_ADDRESS}

    # A conflicting directory address cannot override the leased roster.
    rotated = "tcp://10.0.0.9:7447#rotated"
    fs.dirs.update(
        {
            ("space-group-acme", DIRECTORY_DIR),
            ("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}"),
        }
    )
    fs.docs[
        ("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}/node-b.json")
    ] = json.dumps(
        {
            "deviceId": "node-b",
            "owner": OWNER,
            "serverPublic": "nodekey:server-b",
            "clientPublic": "nodekey:client-b",
            "clientPublicByServer": {},
            "address": rotated,
            "keyGeneration": 2,
            "updatedAt": "2026-10-03T12:30:00+00:00",
        }
    )
    assert service.directory_peers() == {"node-b": INVITER_ADDRESS}
    assert not (home / "org-network" / "bootstrap-peers.json").exists()


def test_confirmed_leave_revokes_without_bootstrap_peer_cache(home: Path) -> None:

    fs = FakeFs()
    _create_org_spaces(fs)
    recorder = Recorder()
    unmapped: list[str] = []
    service = _service(fs, home, recorder=recorder, unmap_peer=unmapped.append)
    service.execute(_invite_link())
    assert service.directory_peers() == {"node-b": INVITER_ADDRESS}

    pending = service.leave("group-acme")
    assert pending["phase"] == "pending-confirmation"
    result = confirm_removal(service.leave, "group-acme")
    assert result["phase"] == "complete"
    assert result["deviceIds"] == [NODE_ID]
    assert service.directory_peers() == {}
    assert unmapped == []  # The sidecar closes gates; Python does not replay mappings.


def test_execute_rejects_a_bad_link(home: Path) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    from hyprial.identity.impl.org_directory import InviteError

    with pytest.raises(InviteError):
        service.execute("hyprial-invite:v1:not-base64!!!")


@pytest.mark.parametrize("form", ["token", "https", "deep"])
def test_join_accepts_all_three_invite_forms(home: Path, form: str) -> None:
    """``join`` is ``execute`` over every §5.1 link form (token/https/deep)."""

    from hyprial.identity.impl.org_directory import (
        INVITE_SCHEME,
        invite_deep_link,
        invite_https_url,
        decode_invite,
    )

    fs = FakeFs()
    _create_org_spaces(fs)
    recorder = Recorder()
    service = _service(fs, home, recorder=recorder)
    link = _invite_link()
    parsed = decode_invite(link)
    if form == "https":
        text = invite_https_url(parsed, "https://invite.example.org")
    elif form == "deep":
        text = invite_deep_link(parsed)
    else:
        text = link
    assert text.startswith((INVITE_SCHEME, "https://", "hyprial://"))
    result = service.join(text)
    assert [step["kind"] for step in result["steps"]] == [
        "connect-inviter", "join-org", "register-device", "connect-members",
    ]
    assert (
        "space-group-acme", f"{DIRECTORY_DIR}/{OWNER}/{NODE_ID}.json",
    ) in fs.docs


def test_an_explicit_join_undoes_an_earlier_leave(home: Path) -> None:
    from hyprial.daemon.impl.org.network.state import add_left_org, read_left_orgs

    add_left_org(home, "group-acme")
    fs = FakeFs()
    _create_org_spaces(fs)
    service = _service(fs, home, recorder=Recorder())

    service.join(_invite_link())

    assert read_left_orgs(home) == []
    assert service.list()["leftOrgs"] == []



def test_a_join_clears_the_leave_even_when_a_later_step_fails(home: Path) -> None:
    """The spaces are joined once join-org succeeds; a failing later step must
    not leave the org marked left (anti-entropy would skip a joined org)."""

    from hyprial.daemon.impl.org.network.state import add_left_org, read_left_orgs

    add_left_org(home, "group-acme")
    fs = FakeFs()
    _create_org_spaces(fs)
    service = _service(fs, home, recorder=Recorder())
    run_step = service._execute_step

    def failing_after_join(link, kind):
        if kind not in {"connect-inviter", "join-org"}:
            raise RuntimeError(f"{kind} failed")
        return run_step(link, kind)

    service._execute_step = failing_after_join  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        service.join(_invite_link())

    assert read_left_orgs(home) == []

def test_join_rejects_a_malformed_link(home: Path) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    from hyprial.identity.impl.org_directory import InviteError

    with pytest.raises(InviteError):
        service.join("https://invite.example.org/wrong#v1.abc")


def test_directory_peers_excludes_this_device_and_addressless_rows(
    home: Path,
) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    import json

    # A device without an address is not representable in the frozen
    # DirectoryDevice model — it is simply absent until it publishes.
    for device_id, address in (
        (NODE_ID, OWN_ADDRESS),
        ("node-b", "tcp://10.0.0.1:7447#b"),
    ):
        fs.dirs.add(("space-group-acme", DIRECTORY_DIR))
        fs.dirs.add(("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}"))
        fs.docs[("space-group-acme", f"{DIRECTORY_DIR}/{OWNER}/{device_id}.json")] = json.dumps(
            {
                "deviceId": device_id,
                "owner": OWNER,
                "serverPublic": f"nodekey:server-{device_id}",
                "clientPublic": f"nodekey:client-{device_id}",
                "clientPublicByServer": {},
                "address": address,
                "keyGeneration": 1,
                "updatedAt": "2026-10-03T11:00:00+00:00",
            }
        )
    _create_org_spaces(fs, "group-other")
    fs.dirs.add(("space-group-other", DIRECTORY_DIR))
    fs.dirs.add(("space-group-other", f"{DIRECTORY_DIR}/{OWNER}"))
    fs.docs[("space-group-other", f"{DIRECTORY_DIR}/{OWNER}/node-d.json")] = json.dumps(
        {
            "deviceId": "node-d",
            "owner": OWNER,
            "serverPublic": "nodekey:server-node-d",
            "clientPublic": "nodekey:client-node-d",
            "clientPublicByServer": {},
            "address": "tcp://10.0.0.4:7447#d",
            "keyGeneration": 1,
            "updatedAt": "2026-10-03T11:00:00+00:00",
        }
    )
    service = _service(fs, home)
    # Directory-only rows cannot admit peers; the local device has no lease.
    assert service.directory_peers() == {}
    assert fs.membership_sidecar.calls[-1] == ("membership-status", {})


def test_publish_self_writes_every_org_and_reports_skips(home: Path) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    _create_org_spaces(fs, "group-zeta")
    service = _service(fs, home, read_own_address=lambda: None)
    result = service.publish_self()
    # No address yet: both orgs are skipped, named by reason, not failures.
    assert result == {
        "published": [],
        "errors": [],
        "skipped": [
            {"org": "group-acme", "reason": "DEVICE_ADDRESS_UNAVAILABLE"},
            {"org": "group-zeta", "reason": "DEVICE_ADDRESS_UNAVAILABLE"},
        ],
    }
    service2 = _service(fs, home)
    result2 = service2.publish_self()
    assert result2["published"] == ["group-acme", "group-zeta"]
    assert result2["skipped"] == []
    # The 10s refresh path calls publish_self repeatedly.  An unchanged device
    # row must not create another OrgFS commit every tick (the 2026-10-19..24
    # write storm came from per-server client-key projections).
    again = service2.publish_self()
    assert again == {"published": [], "skipped": [], "errors": []}


def test_publish_without_a_device_record_names_the_reason(home: Path) -> None:
    (home / "state" / "tailcat" / "device.json").unlink()
    fs = FakeFs()
    _create_org_spaces(fs)
    result = _service(fs, home).publish_self()
    assert result == {
        "published": [],
        "errors": [],
        "skipped": [{"org": "group-acme", "reason": "DEVICE_RECORD_MISSING"}],
    }
