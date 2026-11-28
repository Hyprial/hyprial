"""Invite acceptance must resume effects, not infer completion from org.list."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.membership.invites import InviteService
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import decode_invite, default_role_grants_document

from .test_selection import _invite


class Crash(BaseException):
    pass


class Fs:
    def __init__(self, link):
        self.link = link
        self.joined = set()
        self.calls = Counter()
        self.fail = None
        self.crash = None
        self.incomplete = None
        self.binding = True
        self.policy = True
        self.devices = []

    def join(self, space, *, timeout=None):
        self.calls[space] += 1
        if self.fail == space:
            raise OrgFsError("not-a-member", {"spaceId": space})
        self.joined.add(space)
        if self.incomplete == space:
            self.incomplete = None
            raise OrgFsError("unavailable", {"spaceId": space, "reason": "join-incomplete"})
        if self.crash == space:
            self.crash = None
            raise Crash()

    def spaces(self):
        return tuple(SimpleNamespace(space_id=space, owner="user:bob", name=(
            self.link.org if space == self.link.space_id else "acl"
        )) for space in self.joined)

    def members(self, space):
        mode = "ro" if space == self.link.acl_space_id else "rw"
        return (SimpleNamespace(user="user:alice", mode=mode),)

    def space_meta(self, space):
        return {"aclSpaceId": self.link.acl_space_id} if self.binding else {}

    def read_text(self, space, path):
        import json
        if not self.policy:
            raise OrgFsError("unknown-doc", {})
        return json.dumps(default_role_grants_document()), "v1"


def network(home: Path, fs: Fs, calls: Counter, *, fail_step=None):
    orgs = OrgNetworkService.__new__(OrgNetworkService)
    orgs._home = home
    orgs._owner = "alice"
    orgs._node_id = "node-a"
    orgs._fs = fs
    orgs._log = lambda *a, **kw: None
    orgs._redeem_tailnet = lambda link: calls.update(["redeem"])
    orgs._announce_tailnet = lambda space: calls.update(["announce"])
    orgs._tailnet_candidates = lambda space: {}
    orgs._request_redial = None
    orgs._membership_request = None
    orgs._join_timeout_seconds = 0
    orgs._join_budget = None
    orgs._orgs = lambda: [fs.link.org] if fs.joined else []
    orgs._find_space = lambda name: SimpleNamespace(space_id=fs.link.space_id)
    orgs._store = SimpleNamespace(list_devices=lambda org: fs.devices)
    orgs._role_of = lambda org: "member"
    orgs._org_state = lambda org: "active"
    orgs._oidc_issuer = None
    orgs._oidc_roster = None
    execute = orgs._execute_step

    def step(link, kind):
        calls.update([kind])
        if fail_step == kind:
            raise OrgFsError("no-holder-online", {})
        if kind == "register-device":
            fs.devices = [SimpleNamespace(owner="alice", device_id="node-a")]
            return "registered"
        return execute(link, kind)

    orgs._execute_step = step
    return orgs


@pytest.mark.parametrize("phase", ["directory", "acl", "register-device", "connect-members"])
def test_failure_keeps_pending_and_fresh_service_resumes(tmp_path, phase):
    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls = Fs(link), Counter()
    fs.fail = {"directory": link.space_id, "acl": link.acl_space_id}.get(phase)
    service = InviteService(orgs=network(tmp_path, fs, calls, fail_step=phase))
    params = {"invite_id": invite["id"], "link": invite["link"]}
    with pytest.raises(OrgFsError):
        service.accept(**params)
    pending = service.list([{"payload": payload}])
    assert pending["settled"] == 0
    assert pending["pending"][0]["status"] == "partial"
    assert pending["pending"][0]["missingStep"]
    fs.fail = None
    recovered = InviteService(orgs=network(tmp_path, fs, calls))
    assert recovered.accept(**params)["ok"]
    assert calls["redeem"] == 1
    assert calls["connect-inviter"] == 1
    assert fs.calls[link.space_id] == (2 if phase == "directory" else 1)
    assert recovered.list([{"payload": payload}])["settled"] == 1


def test_accept_draws_on_the_shared_join_budget_like_org_join(tmp_path, monkeypatch):
    from hyprial.daemon.impl.org.membership.invites import recovery
    from hyprial.daemon.impl.org.network.service import OrgNetworkError
    from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget

    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls, clock = Fs(link), Counter(), [0.0]
    monkeypatch.setattr(recovery, "JoinBudget", lambda: JoinBudget(clock=lambda: clock[0]))
    orgs = network(tmp_path, fs, calls)

    def slow_redeem(_link):
        calls.update(["redeem"])
        clock[0] = 10_000

    orgs._redeem_tailnet = slow_redeem
    service = InviteService(orgs=orgs)
    with pytest.raises(OrgNetworkError) as caught:
        service.accept(invite_id=invite["id"], link=invite["link"])
    assert caught.value.code == "MEMBERSHIP_SIDECAR_TIMEOUT"
    assert caught.value.details["leg"] == "member-announce"
    assert calls["announce"] == 0
    assert orgs._join_budget is None
    assert service.list([{"payload": payload}])["pending"][0]["status"] == "partial"


def test_crash_after_directory_effect_before_checkpoint_resumes_without_rejoin(tmp_path):
    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls = Fs(link), Counter()
    fs.crash = link.space_id
    params = {"invite_id": invite["id"], "link": invite["link"]}
    with pytest.raises(Crash):
        InviteService(orgs=network(tmp_path, fs, calls)).accept(**params)
    recovered = InviteService(orgs=network(tmp_path, fs, calls))
    result = recovered.accept(**params)
    assert result["ok"]
    assert fs.calls == {link.space_id: 1, link.acl_space_id: 1}
    assert calls["redeem"] == 1
    assert recovered.list([{"payload": payload}])["settled"] == 1


@pytest.mark.parametrize("space_kind", ["directory", "acl"])
def test_a_listed_space_whose_join_did_not_finish_is_joined_again(tmp_path, space_kind):
    from hyprial.daemon.impl.org.membership.invites.journal import AcceptJournal

    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls = Fs(link), Counter()
    space = link.space_id if space_kind == "directory" else link.acl_space_id
    fs.incomplete = space
    params = {"invite_id": invite["id"], "link": invite["link"]}
    with pytest.raises(OrgFsError) as error:
        InviteService(orgs=network(tmp_path, fs, calls)).accept(**params)
    assert error.value.details["status"] == "partial"
    assert space in {item.space_id for item in fs.spaces()}
    recovered = InviteService(orgs=network(tmp_path, fs, calls))
    assert recovered.accept(**params)["ok"]
    assert fs.calls[space] == 2
    assert fs.calls == {link.space_id: fs.calls[link.space_id], link.acl_space_id: fs.calls[link.acl_space_id]}
    assert sum(fs.calls.values()) == 3
    assert "unfinishedJoins" not in AcceptJournal(tmp_path, invite["id"]).record
    assert recovered.list([{"payload": payload}])["settled"] == 1


@pytest.mark.parametrize("missing", ["binding", "policy"])
def test_spaces_alone_do_not_prove_membership(tmp_path, missing):
    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls = Fs(link), Counter()
    fs.joined.update([link.space_id, link.acl_space_id])
    setattr(fs, missing, False)
    service = InviteService(orgs=network(tmp_path, fs, calls))
    assert service.list([{"payload": payload}])["settled"] == 0
    try:
        result = service.accept(invite_id=invite["id"], link=invite["link"])
    except OrgFsError:
        result = {"ok": False}
    assert not result["ok"]
    assert service.list([{"payload": payload}])["settled"] == 0


@pytest.mark.parametrize("phase", [
    "tailnet-redeemed", "tailnet-announced", "connect-inviter", "directory-committed",
    "acl-applied", "space-bound", "join-org", "register-device", "connect-members",
])
def test_crash_after_each_checkpoint_resumes(tmp_path, monkeypatch, phase):
    from hyprial.daemon.impl.org.membership.invites.journal import AcceptJournal

    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls = Fs(link), Counter()
    original = AcceptJournal.complete_step

    def checkpoint(journal, step):
        original(journal, step)
        if step == phase:
            raise Crash()

    monkeypatch.setattr(AcceptJournal, "complete_step", checkpoint)
    params = {"invite_id": invite["id"], "link": invite["link"]}
    with pytest.raises(Crash):
        InviteService(orgs=network(tmp_path, fs, calls)).accept(**params)
    monkeypatch.setattr(AcceptJournal, "complete_step", original)
    recovered = InviteService(orgs=network(tmp_path, fs, calls))
    assert recovered.list([{"payload": payload}])["settled"] == 0
    first = recovered.accept(**params)
    assert first["ok"]
    assert InviteService(orgs=network(tmp_path, fs, calls)).accept(**params) == first
    assert calls["redeem"] == 1
    assert fs.calls == {link.space_id: 1, link.acl_space_id: 1}
    record = AcceptJournal(tmp_path, invite["id"]).record
    assert record["state"] == "done"
    assert record["result"] == first
    assert "token" not in str(record)
    assert payload not in str(record)


@pytest.mark.parametrize("failure", [False, True])
def test_fresh_ipc_services_share_one_flight_and_exact_outcome(tmp_path, monkeypatch, failure):
    from concurrent.futures import ThreadPoolExecutor, Future
    from threading import Event
    from hyprial.daemon.impl.org.membership.invites import flight
    from hyprial.daemon.impl.org.network import ipc
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
    from hyprial.kernel import DaemonRequestError

    _payload, invite = _invite()
    fs, calls = Fs(decode_invite(invite["link"])), Counter()
    entered, waiting, release = Event(), Event(), Event()

    class WaitingFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout=timeout)

    def factory(app):
        orgs = network(tmp_path, fs, calls,
                       fail_step="register-device" if failure else None)
        original = orgs._redeem_tailnet

        def redeem(link):
            original(link)
            entered.set()
            assert release.wait(timeout=5)

        orgs._redeem_tailnet = redeem
        return orgs

    monkeypatch.setattr(flight, "Future", WaitingFuture)
    monkeypatch.setattr(ipc, "_request_service", factory)
    app = SimpleNamespace(_log=lambda *a, **kw: None)
    params = {"inviteId": invite["id"], "link": invite["link"]}

    def accept():
        try:
            return METHOD_REGISTRY.invoke(app, "org.invites.accept", params)
        except DaemonRequestError as error:
            return {"code": error.code, "data": error.data}

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(accept)
        assert entered.wait(timeout=2)
        two = pool.submit(accept)
        try:
            assert waiting.wait(timeout=2)
        finally:
            release.set()
        results = [one.result(timeout=5), two.result(timeout=5)]
    assert results[0] == results[1]
    assert calls["redeem"] == 1
    assert calls["register-device"] == 1
    if failure:
        assert results[0]["code"] == "no-holder-online"
        assert results[0]["data"]["status"] == "partial"
        assert results[0]["data"]["missingStep"] == "register-device"
    else:
        assert results[0]["ok"]


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_space_cannot_hide_an_unfinished_invite(tmp_path, reverse):
    from dataclasses import replace
    from hyprial.identity import encode_invite

    payload, invite = _invite()
    link = decode_invite(invite["link"])
    fs, calls = Fs(link), Counter()
    service = InviteService(orgs=network(tmp_path, fs, calls, fail_step="connect-members"))
    with pytest.raises(OrgFsError):
        service.accept(invite_id=invite["id"], link=invite["link"])
    other = encode_invite(replace(link, token="x" * 32)).removeprefix("hyprial-invite:v2:")
    rows = [{"payload": payload}, {"payload": other}]
    result = service.list(list(reversed(rows)) if reverse else rows)
    assert result["settled"] == 0
    assert result["pending"][0]["inviteId"] == invite["id"]
    assert result["pending"][0]["status"] == "partial"


def test_register_device_success_requires_readback(tmp_path):
    _payload, invite = _invite()
    fs, calls = Fs(decode_invite(invite["link"])), Counter()
    orgs = network(tmp_path, fs, calls)
    orgs._store.list_devices = lambda org: []
    with pytest.raises(OrgFsError) as error:
        InviteService(orgs=orgs).accept(invite_id=invite["id"], link=invite["link"])
    assert error.value.details["missingStep"] == "register-device"
