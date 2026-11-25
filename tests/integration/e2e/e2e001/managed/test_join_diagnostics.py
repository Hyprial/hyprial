"""Managed-org failures retain useful public diagnostics without invite secrets."""

import json
import hashlib
from datetime import UTC, datetime, timedelta
from subprocess import CompletedProcess

import pytest

from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner


INVITE = "https://join.invalid/invite?token=ephemeral-QUERY#v1.inviteBLOB"


def failure(monkeypatch, payload, *, raw=False, invite=INVITE):
    runner = load_runner()
    monkeypatch.setattr(runner, "_ORG_NAMES", {"space": "group-control"})
    monkeypatch.setattr(runner, "_INVITES", {("space", "e2e"): invite})
    monkeypatch.setattr(runner, "owner_of_node", lambda node: "e2e")

    def execute(argv, **kwargs):
        assert invite not in str(argv)
        assert kwargs["input"] == invite
        stdout = payload if raw else json.dumps(payload)
        return CompletedProcess(argv, 1, stdout, "untrusted stderr bearer SHOULD_NOT_APPEAR")

    monkeypatch.setattr(runner.subprocess, "run", execute)
    with pytest.raises(AssertionError) as caught:
        runner.join_org_space(runner.NODES[1], "space", actor="fixture-actor")
    return str(caught.value)


def test_valid_v2_invite_removes_decoded_bearers_and_private_addresses(monkeypatch):
    from hyprial.identity import InviteLink, invite_https_url

    expires = datetime.now(UTC) + timedelta(hours=1)
    workflow = "private workflow"
    address = "127.0.0.1:7448#PRIVATE_ADDRESS_PSK"
    org_token = "PRIVATE_ORG_TOKEN"
    tailnet_token = "TAILNET_PRIVATE_TOKEN_" + "x" * 21
    link = InviteLink(
        org="group-control", space_id="space", acl_space_id="acl-space",
        inviter="e2e", inviter_device="node-a", inviter_address=address,
        token=org_token, workflow_yaml=workflow,
        workflow_sha256=hashlib.sha256(workflow.encode()).hexdigest(),
        expires_at=expires.isoformat().replace("+00:00", "Z"),
        tailnet={
            "version": 2, "tailnetId": "space", "inviteId": "invite-1",
            "token": tailnet_token, "owner": "e2e",
            "expiresAtMs": int(expires.timestamp() * 1000),
            "authority": {"owner": "e2e", "deviceId": "node-a",
                          "masterPublic": "nodekey:" + "a" * 64,
                          "serverPublic": "nodekey:" + "b" * 64,
                          "address": address, "controlPort": 7448},
        },
    )
    invite = invite_https_url(link, "https://join.invalid")
    message = failure(monkeypatch, {
        "code": "DAEMON_ERROR", "error": f"rejected {org_token} {address} {tailnet_token} PRIVATE_ADDRESS_PSK",
    }, invite=invite)
    assert "rejected" in message
    for secret in (org_token, address, tailnet_token, invite, "PRIVATE_ADDRESS_PSK"):
        assert secret not in message


def test_failed_join_preserves_bounded_error_and_step_identity(monkeypatch):
    message = failure(monkeypatch, {
        "ok": False,
        "code": "DAEMON_ERROR",
        "error": "membership-status unavailable",
        "steps": [{"kind": "join-org", "ok": False, "code": "no-holder-online", "error": "supplier unavailable"}],
        "data": {"token": "SHOULD_NOT_APPEAR"},
    })
    for expected in ("DAEMON_ERROR", "membership-status unavailable", "join-org", "no-holder-online", "supplier unavailable"):
        assert expected in message
    assert "SHOULD_NOT_APPEAR" not in message


@pytest.mark.parametrize("error", [
    f"invite rejected: {INVITE}; fragment=v1.inviteBLOB token=ephemeral-QUERY",
    'authorization="Bearer PRIVATE_AUTH" credential=PRIVATE_CREDENTIAL secret=PRIVATE_SECRET password=PRIVATE_PASSWORD private_key=PRIVATE_KEY',
    'other invite https://other.invalid/j/PRIVATE_OTHER#PRIVATE_FRAGMENT v1.PRIVATE_BARE',
])
def test_join_diagnostics_remove_links_fragments_and_sensitive_values(monkeypatch, error):
    message = failure(monkeypatch, {
        "ok": False, "code": "DAEMON_ERROR", "error": error,
        "token": "PRIVATE_TOP",
        "steps": [{"kind": "register-device", "ok": False, "error": error, "credential": "PRIVATE_STEP"}],
    })
    assert "register-device" in message
    assert "DAEMON_ERROR" in message
    for secret in (INVITE, "v1.inviteBLOB", "ephemeral-QUERY", "PRIVATE_AUTH", "PRIVATE_CREDENTIAL", "PRIVATE_SECRET", "PRIVATE_PASSWORD", "PRIVATE_KEY", "PRIVATE_OTHER", "PRIVATE_FRAGMENT", "PRIVATE_BARE", "PRIVATE_TOP", "PRIVATE_STEP", "SHOULD_NOT_APPEAR"):
        assert secret not in message


def test_nested_error_payload_is_not_coerced_into_diagnostics(monkeypatch):
    message = failure(monkeypatch, {
        "ok": False, "code": "DAEMON_ERROR", "error": {"token": "PRIVATE_NESTED"},
        "steps": [{"kind": "join-org", "error": {"secret": "PRIVATE_STEP"}}],
    })
    assert "join-org" in message
    assert "PRIVATE_" not in message


def test_join_diagnostics_bound_untrusted_fields_and_step_count(monkeypatch):
    message = failure(monkeypatch, {
        "ok": False, "code": "DAEMON_ERROR", "error": "x" * 100_000,
        "steps": [{"kind": "join-org", "error": "y" * 100_000}] * 100,
    })
    assert "join-org" in message
    assert len(message) < 8000


def test_non_json_failure_does_not_echo_stdout_or_stderr(monkeypatch):
    message = failure(monkeypatch, f"raw {INVITE} SHOULD_NOT_APPEAR", raw=True)
    assert "JSON" in message
    assert INVITE not in message
    assert "SHOULD_NOT_APPEAR" not in message
