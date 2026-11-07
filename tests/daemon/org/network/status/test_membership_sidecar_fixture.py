"""Contract §3.2 fixture checks plus removal-order regression coverage."""
from copy import deepcopy

import pytest
from tests.support.tailnet_membership import confirmation, confirm_removal

from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from tests.support.tailnet_membership import FakeMembershipSidecar, member


def initialized():
    sidecar = FakeMembershipSidecar()
    authority = {k: sidecar.local[k] for k in (
        "owner", "deviceId", "masterPublic", "serverPublic", "address"
    )} | {"controlPort": 7448}
    params = {"operationId": "init", "tailnetId": "org", "authority": authority,
              "migration": {"source": "new", "sourceDigest": "", "completed": True},
              "members": [sidecar.local, member("bob@example", "node-b")]}
    assert sidecar.membership_request("membership-init", params) == {
        "tailnetId": "org", "revision": 1, "initialized": True,
    }
    return sidecar, params


def test_empty_status_is_unfiltered_only_like_native_go():
    sidecar = FakeMembershipSidecar()
    assert sidecar.membership_request('membership-status', {})['tailnets'] == []
    with pytest.raises(ForwardingSidecarError) as refused:
        sidecar.membership_request('membership-status', {'tailnetId': 'unknown'})
    assert refused.value.code == 'MEMBERSHIP_NOT_INITIALIZED'


def test_status_has_exact_unwrapped_contract_shape_and_copied_rows():
    sidecar, params = initialized()
    assert sidecar.membership_request("membership-init", params)["initialized"] is True
    result = sidecar.membership_request("membership-status", {})
    assert set(result) == {"readiness", "tailnets"}
    assert result["readiness"] == {"sidecar": True, "control": True, "member": True}
    assert result["tailnets"] == [{
        "tailnetId": "org", "role": "authority", "revision": 1,
        "authorityDeviceId": "node-a", "memberReady": True, "leaseRemainingSeconds": None,
        "activeCount": 2, "tombstoneCount": 0, "code": None,
        "members": [sidecar.local, member("bob@example", "node-b")],
    }]
    result["tailnets"][0]["members"].clear()
    assert sidecar.membership_request("membership-status", {})["tailnets"][0]["activeCount"] == 2
    with pytest.raises(ForwardingSidecarError) as refused:
        sidecar.membership_request("membership-init", params | {"operationId": "again"})
    assert refused.value.code == "MEMBERSHIP_ALREADY_INITIALIZED"


def test_non_authority_init_stays_unleased_and_roster_fetch_is_explicit():
    authority, params = initialized()
    joiner = FakeMembershipSidecar(owner="bob@example", device_id="node-b")
    assert joiner.membership_request("membership-init", params | {"members": []})["revision"] == 0
    status = joiner.membership_request("membership-status", {})["tailnets"][0]
    assert status["role"] == "member"
    assert status["members"] == []
    assert status["leaseRemainingSeconds"] == 0
    assert status["memberReady"] is False
    remote = authority.for_device("bob@example", "node-b")
    roster = remote.membership_request("roster-fetch", {"tailnetId": "org"})
    assert set(roster) == {"tailnetId", "authority", "revision", "members", "leaseSeconds"}
    assert roster["leaseSeconds"] == 300
    assert remote.membership_request("membership-status", {})["tailnets"][0]["memberReady"] is True
    remote.leases["org"] = 0
    assert remote.membership_request("membership-status", {})["tailnets"][0]["code"] == "MEMBERSHIP_LEASE_EXPIRED"


def test_announce_and_revoke_replay_before_revision_check():
    sidecar, _ = initialized()
    announce = {"operationId": "announce", "tailnetId": "org",
                "address": sidecar.local["address"], "clientPublicByServer": {}}
    first = sidecar.membership_request("member-announce", announce)
    assert set(first) == {"tailnetId", "revision", "member"}
    assert first["revision"] == 2
    assert len(first["member"]["clientPublicByServer"]) == 2
    assert sidecar.membership_request("member-announce", announce | {"operationId": "same-keys"}) == first
    revoke = {"operationId": "remove", "tailnetId": "org", "deviceIds": ["node-b"],
              "expectedRevision": 2, "confirmed": True, "reason": "test"}
    result = sidecar.membership_request("member-revoke", revoke)
    assert result == {"tailnetId": "org", "revision": 3, "deviceIds": ["node-b"],
                      "backupPath": "/fake/backups/remove.json", "propagation": "lease-bounded"}
    assert sidecar.membership_request("member-revoke", revoke) == result
    assert len(sidecar.backups) == 1
    row = sidecar.membership_request("membership-status", {})["tailnets"][0]
    assert (row["activeCount"], row["tombstoneCount"]) == (1, 1)
    assert row["members"][1]["address"] == ""
    assert row["members"][1]["clientPublicByServer"] == {}
    with pytest.raises(ForwardingSidecarError) as conflict:
        sidecar.membership_request("member-revoke", revoke | {"reason": "different"})
    assert conflict.value.code == "OPERATION_CONFLICT"
    with pytest.raises(ForwardingSidecarError) as stale:
        sidecar.membership_request("member-revoke", revoke | {"operationId": "fresh"})
    assert stale.value.code == "REVISION_CONFLICT"


def test_invite_binding_and_repin_guards():
    sidecar, params = initialized()
    invite = sidecar.membership_request("invite-create", {
        "operationId": "invite", "tailnetId": "org", "owner": "carol@example",
        "expiresAtMs": sidecar.now_ms + 3600000,
    })["invite"]
    assert set(invite) == {"version", "tailnetId", "authority", "inviteId", "token", "owner", "expiresAtMs"}
    assert invite["version"] == 2
    assert len(invite["token"]) == 43
    carol = sidecar.for_device("carol@example", "node-c")
    request = {"operationId": "redeem", "invite": invite}
    result = carol.membership_request("invite-redeem", request)
    assert set(result) == {"tailnetId", "authority", "revision", "members", "leaseSeconds", "deviceId"}
    assert result["deviceId"] == "node-c"
    assert carol.membership_request("invite-redeem", request) == result
    carol.membership_request("member-announce", {
        "operationId": "carol-announce", "tailnetId": "org",
        "address": carol.local["address"], "clientPublicByServer": {},
    })
    assert carol.membership_request("invite-redeem", request | {"operationId": "same-device"}) == result
    other = sidecar.for_device("carol@example", "node-d")
    with pytest.raises(ForwardingSidecarError) as consumed:
        other.membership_request("invite-redeem", request | {"operationId": "other"})
    assert consumed.value.code == "INVITE_CONSUMED"
    repin = {"operationId": "repin", "tailnetId": "org", "authority": params["authority"],
             "expectedRevision": sidecar.tailnets["org"]["revision"], "confirmed": True}
    carol.business_running = True
    with pytest.raises(ForwardingSidecarError) as running:
        carol.membership_request("authority-repin", repin)
    assert running.value.code == "NETWORK_NOT_QUIESCENT"
    carol.business_running = False
    assert carol.membership_request("authority-repin", repin) == {
        "tailnetId": "org", "revision": repin["expectedRevision"],
        "backupPath": "/fake/backups/repin.json", "restartRequired": True,
    }
    assert carol.leases["org"] == 0


@pytest.mark.parametrize("op,params", [
    ("allow", {}), ("membership-status", {"bogus": True}),
    ("roster-fetch", {}), ("member-announce", {"tailnetId": "org"}),
])
def test_fixture_does_not_accept_retired_or_incomplete_operations(op, params):
    with pytest.raises(ForwardingSidecarError) as refused:
        FakeMembershipSidecar().membership_request(op, params)
    assert refused.value.code == "BAD_REQUEST"


def test_removal_freezes_targets_and_cannot_revoke_before_upper_layer_commit(tmp_path):
    sidecar, _ = initialized()
    journal = RemovalJournal(tmp_path, sidecar.membership_request, local_device="node-a")
    upper_calls = []

    def upper():
        assert not any(op == "member-revoke" for op, _ in sidecar.calls)
        upper_calls.append("committed")

    pending = journal.remove("org", "bob@example", confirmed=False, upper=upper)
    assert pending["phase"] == "pending-confirmation"
    assert pending["deviceIds"] == ["node-b"]
    assert upper_calls == []
    sidecar.failures["member-revoke"] = "AUTHORITY_UNREACHABLE"
    with pytest.raises(ForwardingSidecarError):
        journal.remove("org", "bob@example", confirmed=True, upper=upper, **confirmation(pending))
    assert upper_calls == ["committed"]
    sent = deepcopy(sidecar.calls[-1])
    del sidecar.failures["member-revoke"]
    done = journal.remove("org", "bob@example", confirmed=True, upper=upper, **confirmation(pending))
    assert done["phase"] == "complete"
    assert done["operationId"] == pending["operationId"]
    assert done["zenohDeny"] == "not-implemented-0.5.5.1"
    assert sidecar.calls[-1] == sent
    assert upper_calls == ["committed"]


def test_non_authority_administrative_removal_stays_pending_without_upper_write(tmp_path):
    authority, _ = initialized()
    remote = authority.for_device("bob@example", "node-b")
    authority.tailnets["org"]["members"]["node-c"] = member("carol@example", "node-c")
    journal = RemovalJournal(tmp_path, remote.membership_request, local_device="node-b")

    def forbidden():
        pytest.fail("A non-authority cannot commit another member's removal")

    result = confirm_removal(journal.remove, "org", "carol@example", upper=forbidden)
    assert result["phase"] == "pending-authority"
    assert result["deviceIds"] == ["node-c"]
    assert not any(op == "member-revoke" for op, _ in remote.calls)


def test_projection_failure_cannot_fall_back_to_directory(tmp_path, monkeypatch):
    from ..test_org_network_service import FakeFs, _service
    from hyprial.daemon.impl.org.network.service import OrgNetworkError
    from hyprial.identity import DeviceRecord, write_device_record

    sidecar, _ = initialized()
    write_device_record(tmp_path, DeviceRecord(
        device_id="node-a", owner="owner@example", server_public=sidecar.local["serverPublic"],
        client_public=sidecar.local["masterPublic"],
    ))
    fs = FakeFs()
    service = _service(fs, tmp_path, membership_request=sidecar.membership_request)
    monkeypatch.setattr(service._store, "list_devices", lambda _: pytest.fail("Directory fallback"))
    sidecar.failures["membership-status"] = "AUTHORITY_UNREACHABLE"
    with pytest.raises(OrgNetworkError) as refused:
        service.directory_peers()
    assert refused.value.code == "AUTHORITY_UNREACHABLE"


def test_per_org_publication_failure_does_not_hide_healthy_org(tmp_path, monkeypatch):
    from ..test_org_network_service import FakeFs, _create_org_spaces, _service

    fs = FakeFs()
    _create_org_spaces(fs, "group-acme")
    _create_org_spaces(fs, "group-zeta")
    service = _service(fs, tmp_path)
    visited = []

    def publish(org):
        visited.append(org)
        if org == "group-acme":
            raise RuntimeError("test-only failure")
        return True

    monkeypatch.setattr(service, "_publish_into", publish)
    assert service.publish_self() == {
        "published": ["group-zeta"], "skipped": [],
        "errors": [{"org": "group-acme", "code": "PUBLISH_FAILED", "errorType": "RuntimeError"}],
    }
    assert visited == ["group-acme", "group-zeta"]
