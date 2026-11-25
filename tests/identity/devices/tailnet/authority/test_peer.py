"""Local capability gates and pre-authentication request failures."""

from dataclasses import replace
import json

import pytest

from hyprial.identity import TailnetCredentialError

from .support import LocalContext, body, call, inventory


@pytest.mark.parametrize("uid", [True, 7, [], None])
def test_non_string_uid_is_bad_request_without_operation_receipt(tmp_path, uid):
    context = LocalContext(tmp_path / "home")
    value = json.loads(body(context))
    value["deviceUid"] = uid
    status, raw = call(context, json.dumps(value).encode())
    result = json.loads(raw)
    assert status == 400
    assert result["error"]["code"] == "BAD_REQUEST"
    assert result["receipt"] is None


@pytest.mark.parametrize("change", ["pin", "tombstone", "member-role"])
def test_invalid_local_authority_cannot_initialize_ca(tmp_path, change):
    context = LocalContext(tmp_path / "home")
    if change == "pin":
        pin = replace(context.pin("tailnet-A"), server_public="different")
        context.observations["tailnet-A"] = replace(context.membership("tailnet-A"), authority_pin=pin)
    elif change == "tombstone":
        context.rows_override = (replace(context.members("tailnet-A")[0], state="tombstone", address=""),)
    else:
        context.observations["tailnet-A"] = replace(context.membership("tailnet-A"), role="member", lease_remaining_seconds=0)
    before = inventory(context.home)
    with pytest.raises(TailnetCredentialError):
        context.provision()
    after = inventory(context.home)
    assert all("authority/" not in path for path in set(after) - set(before))


def test_remote_entrypoints_do_not_accept_local_flags_or_handmade_peer_objects(tmp_path):
    context = LocalContext(tmp_path / "home")
    facade = context.facade()
    before = inventory(context.home)
    for value in ({"local": True}, {"peerKey": context.record.client_public}, object()):
        with pytest.raises(TailnetCredentialError) as caught:
            facade.handle(connection=value, tailnet_id="tailnet-A", route="/v1/tailnet-credentials/enroll", request_body=body(context))
        assert caught.value.code == "TAILNET_CONNECTION_UNTRUSTED"
        with pytest.raises(TailnetCredentialError):
            facade.peer_member(value, tailnet_id="tailnet-A")
    assert inventory(context.home) == before
