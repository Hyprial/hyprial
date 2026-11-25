"""Confirmation must identify the exact bearer, not just its display fields."""
from dataclasses import replace

import pytest

from hyprial.daemon.impl.org.membership.invites import InviteService
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity import decode_invite, encode_invite, invite_view
from hyprial.kernel import DaemonRequestError, MethodError, validate_method_spec
from .test_selection import _invite, _Orgs, _App


def test_reissued_bearer_cannot_reuse_another_confirmation():
    payload, original = _invite()
    replacement = encode_invite(replace(decode_invite(original["link"]), token="x" * 32))
    orgs = _Orgs()
    with pytest.raises(ValueError, match="does not match"):
        InviteService(orgs=orgs).accept(
            invite_id=invite_view(payload).invite_id, link=replacement
        )
    assert orgs.joins == []


def test_leave_between_listing_and_accept_cannot_be_undone():
    payload, original = _invite()
    orgs = _Orgs(left=(original["org"],))
    with pytest.raises(ValueError, match="left"):
        InviteService(orgs=orgs).accept(
            invite_id=invite_view(payload).invite_id, link=original["link"]
        )
    assert orgs.joins == []


def test_method_spec_can_preserve_legacy_orgfs_error_spelling():
    template = METHOD_REGISTRY.get("hosts").spec
    validate_method_spec(replace(template, errors=(MethodError("no-holder-online"),)))


@pytest.mark.parametrize("code", ["mixedCase", "bad/code", "bad code", ""])
def test_method_spec_still_rejects_malformed_error_codes(code):
    template = METHOD_REGISTRY.get("hosts").spec
    with pytest.raises(ValueError):
        validate_method_spec(replace(template, errors=(MethodError(code),)))


def test_join_is_declared_and_preserves_invalid_invite_error(monkeypatch):
    from hyprial.daemon.impl.org.network import ipc

    monkeypatch.setattr(ipc, "_join_and_settle", lambda app, link, claims=None: decode_invite(link))
    assert "org.join" in METHOD_REGISTRY.names
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(_App(), "org.join", {"link": "v2.invalid"})
    assert error.value.code == "INVITE_MALFORMED"


@pytest.mark.parametrize("method", ["org.join", "org.invites.accept"])
@pytest.mark.parametrize("code", ["no-holder-online", "not-a-member", "AUTHORITY_UNREACHABLE"])
def test_join_errors_keep_their_published_codes(monkeypatch, method, code):
    from hyprial.daemon.impl.org.network import ipc
    from hyprial.daemon.impl.org.network.service import OrgNetworkError
    from hyprial.daemon.impl.orgfs.api import OrgFsError

    def fail(app, link, claims=None):
        if code.isupper():
            raise OrgNetworkError(code, "unavailable")
        raise OrgFsError(code, {"spaceId": "space-acme"})

    monkeypatch.setattr(ipc, "_join_and_settle", fail)
    monkeypatch.setattr(ipc, "_accept_and_settle", fail)
    monkeypatch.setattr(ipc, "_request_service", lambda app: _Orgs())
    payload, original = _invite()
    params = {"link": original["link"]}
    if method == "org.invites.accept":
        params["inviteId"] = invite_view(payload).invite_id
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(_App(), method, params)
    assert error.value.code == code
    if not code.isupper():
        assert error.value.data == {"spaceId": "space-acme"}
