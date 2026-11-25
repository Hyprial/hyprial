"""Real OrgFS grants must satisfy the invitation's read-back and resume checks.

Transport and device publication are stand-ins; membership, binding, policy,
and the invitation grant producer are real local product implementations.
"""

from collections import Counter
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.membership.invites import InviteService
from hyprial.daemon.impl.org.membership.invites.journal import AcceptJournal
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import (
    decode_invite, default_role_grants_document, encode_invite, invite_identifier,
)

from .test_recovery import Crash, Fs, network
from .test_selection import _invite


@pytest.fixture
def granted(tmp_path):
    runtime = OrgFsRuntime(tmp_path / "state", node_id="node-b", author="user:bob")
    try:
        fs = runtime.facade
        directory = fs.create_space("group-acme")
        acl = fs.create_space("group-acme-acl")
        fs.update_space_meta(directory.space_id, {"aclSpaceId": acl.space_id})
        fs.mkdir(acl.space_id, "policy")
        fs.write_text(acl.space_id, ACL_POLICY_DOC,
                      json.dumps(default_role_grants_document()))
        _, invite = _invite()
        original = decode_invite(invite["link"])
        parsed = replace(original, space_id=directory.space_id, acl_space_id=acl.space_id,
                         tailnet=original.tailnet | {"tailnetId": directory.space_id})
        text = encode_invite(parsed)
        calls = Counter()
        orgs = network(tmp_path, Fs(parsed), calls)
        orgs._fs = fs
        orgs._casdoor = None
        orgs._store.list_devices = lambda org: [
            SimpleNamespace(owner=orgs._owner, device_id="node-a")
        ]
        orgs._invite_casdoor_first(parsed.org, parsed.space_id,
                                  parsed.acl_space_id, "user:alice")
        grants = {
            space: next(member.mode for member in fs.members(space)
                        if member.user == "user:alice")
            for space in (directory.space_id, acl.space_id)
        }
        assert grants == {directory.space_id: "rw", acl.space_id: "ro"}

        def join(space):
            calls[space] += 1
            fs.invite(space, "user:alice", grants[space])

        orgs._join_with_retry = join
        yield orgs, text, parsed, calls
    finally:
        runtime.close()


def accept(orgs, text):
    return InviteService(orgs=orgs).accept(invite_id=invite_identifier(text), link=text)


def assert_done(orgs, text, result):
    record = AcceptJournal(orgs._home, invite_identifier(text)).record
    assert record["state"] == "done"
    assert record["result"] == result
    assert orgs.invite_missing_step(text) is None
    assert InviteService(orgs=orgs).list([
        {"payload": text.removeprefix("hyprial-invite:v2:")}
    ])["settled"] == 1
    assert accept(orgs, text) == result


@pytest.mark.parametrize("member", ["ordinary", "owner", "higher-acl"])
def test_existing_membership_reaches_done(granted, member):
    orgs, text, parsed, calls = granted
    if member == "owner":
        orgs._owner = "bob"
    elif member == "higher-acl":
        orgs._fs.invite(parsed.acl_space_id, "user:alice", "rw")
    result = accept(orgs, text)
    assert result["alreadyMember"]
    assert calls == {}
    assert_done(orgs, text, result)


def test_first_accept_reaches_done_with_production_grants(granted):
    orgs, text, parsed, calls = granted
    for space in (parsed.space_id, parsed.acl_space_id):
        orgs._fs.remove_member(space, "user:alice")
    result = accept(orgs, text)
    assert result["joined"]
    assert calls[parsed.space_id] == calls[parsed.acl_space_id] == 1
    assert_done(orgs, text, result)


@pytest.mark.parametrize("phase", ["directory-committed", "acl-applied", "join-org"])
def test_crash_resume_keeps_read_only_acl(granted, monkeypatch, phase):
    orgs, text, parsed, calls = granted
    for space in (parsed.space_id, parsed.acl_space_id):
        orgs._fs.remove_member(space, "user:alice")
    original = AcceptJournal.complete_step

    def checkpoint(journal, step):
        original(journal, step)
        if step == phase:
            raise Crash()

    monkeypatch.setattr(AcceptJournal, "complete_step", checkpoint)
    with pytest.raises(Crash):
        accept(orgs, text)
    monkeypatch.setattr(AcceptJournal, "complete_step", original)
    result = accept(orgs, text)
    assert result["joined"]
    assert calls[parsed.space_id] == calls[parsed.acl_space_id] == 1
    assert calls["redeem"] == 1
    assert_done(orgs, text, result)


@pytest.mark.parametrize("space_kind", ["directory", "acl"])
def test_crash_after_membership_write_before_checkpoint(granted, space_kind):
    orgs, text, parsed, calls = granted
    for space in (parsed.space_id, parsed.acl_space_id):
        orgs._fs.remove_member(space, "user:alice")
    crash_space = parsed.space_id if space_kind == "directory" else parsed.acl_space_id
    original = orgs._join_with_retry

    def interrupted(space):
        original(space)
        if space == crash_space:
            raise Crash()

    orgs._join_with_retry = interrupted
    with pytest.raises(Crash):
        accept(orgs, text)
    orgs._join_with_retry = original
    result = accept(orgs, text)
    assert calls[parsed.space_id] == calls[parsed.acl_space_id] == 1
    assert_done(orgs, text, result)


@pytest.mark.parametrize("space_kind", ["directory", "acl"])
def test_revoked_completed_membership_is_not_recreated(granted, space_kind):
    orgs, text, parsed, calls = granted
    result = accept(orgs, text)
    assert_done(orgs, text, result)
    space = parsed.space_id if space_kind == "directory" else parsed.acl_space_id
    orgs._fs.remove_member(space, "user:alice")
    with pytest.raises(OrgFsError) as error:
        accept(orgs, text)
    assert error.value.code == "not-a-member"
    assert error.value.details["status"] == "partial"
    assert calls == {}
    assert InviteService(orgs=orgs).list([
        {"payload": text.removeprefix("hyprial-invite:v2:")}
    ])["settled"] == 0


@pytest.mark.parametrize("missing", ["acl", "directory", "directory-write"])
def test_insufficient_membership_stays_partial(granted, missing):
    orgs, text, parsed, _calls = granted
    space = parsed.acl_space_id if missing == "acl" else parsed.space_id
    orgs._fs.remove_member(space, "user:alice")
    if missing == "directory-write":
        orgs._fs.invite(space, "user:alice", "ro")
    expected = "acl-applied" if missing == "acl" else "directory-committed"
    assert orgs.invite_missing_step(text) == expected

    def denied(space):
        raise OrgFsError("not-a-member", {"spaceId": space})

    orgs._join_with_retry = denied
    with pytest.raises(OrgFsError) as error:
        accept(orgs, text)
    assert error.value.details["status"] == "partial"
    assert error.value.details["missingStep"] == expected
    pending = InviteService(orgs=orgs).list([
        {"payload": text.removeprefix("hyprial-invite:v2:")}
    ])
    assert pending["settled"] == 0
    assert pending["pending"][0]["status"] == "partial"
    assert pending["pending"][0]["missingStep"] == expected
    assert AcceptJournal(orgs._home, invite_identifier(text)).record["state"] != "done"
