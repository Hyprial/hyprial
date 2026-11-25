"""Pure synthetic management values; no runtime, configuration or credential reads.

The invitation uses the public encoder. Receipt digests below are fixed values
for the synthetic nodekey:333… and nodekey:444… identities, independently
compared with RemovalJournal in the producer tests.
"""

from hashlib import sha256

from hyprial.identity import (
    InviteLink,
    bootstrap_yaml,
    default_bootstrap,
    encode_invite,
)


def invite_link(*, expired: bool) -> str:
    workflow = bootstrap_yaml(default_bootstrap())
    expiry = "2000-01-01T00:00:00+00:00" if expired else "9999-01-01T00:00:00+00:00"
    return encode_invite(
        InviteLink(
            org="group-hyprial-dev",
            space_id="space-hyprial-dev",
            acl_space_id="space-hyprial-dev-acl",
            inviter="alice",
            inviter_device="node-authority",
            inviter_address="tcp://127.0.0.1:7447#synthetic",
            token="synthetic-example",
            workflow_yaml=workflow,
            workflow_sha256=sha256(workflow.encode()).hexdigest(),
            expires_at=expiry,
            tailnet={
                "version": 2,
                "tailnetId": "space-hyprial-dev",
                "inviteId": "invite-example",
                "token": "s" * 43,
                "owner": "bob",
                "expiresAtMs": 946684800000 if expired else 253370764800000,
                "authority": {
                    "owner": "alice",
                    "deviceId": "node-authority",
                    "masterPublic": "nodekey:" + "1" * 64,
                    "serverPublic": "nodekey:" + "2" * 64,
                    "address": "tcp://127.0.0.1:7447#synthetic",
                    "controlPort": 7448,
                },
            },
        )
    )


def removal_receipt(user: str | None) -> dict[str, object]:
    owner = "alice" if user is None else "alex"
    device = "node-1" if user is None else "node-alex"
    return {
        "operationId": "00000000000000000000000000000001",
        "tailnetId": "space-hyprial-dev",
        "owner": owner,
        "deviceIds": [device],
        "expectedRevision": 3,
        "authorityDeviceId": "node-authority",
        "role": "member" if user is None else "authority",
        "phase": "pending-confirmation",
        "reason": "operator-removal",
        "confirmed": False,
        "targetIdentityDigests": {device: _IDENTITY_DIGESTS[owner]},
        "targetKeyDigests": _KEY_DIGESTS,
    }


def delete_preview() -> dict[str, object]:
    return {
        "org": "group-hyprial-dev",
        "spaceId": "space-hyprial-dev",
        "operationId": "00000000000000000000000000000002",
        "phase": "pending-confirmation",
        "deviceIds": ["node-1"],
        "confirmed": False,
        "irreversible": True,
        "migrationDoc": "docs/guides/migration-oc7-org-lifecycle.md",
        "expectedRevision": 3,
        "authorityDeviceId": "node-authority",
        "orgfsMarked": False,
        "tailnetPhase": None,
        "tailnetClosed": False,
        "tailnetForgotten": False,
    }


def delete_complete() -> dict[str, object]:
    return {
        "org": "group-hyprial-dev",
        "spaceId": "space-hyprial-dev",
        "operationId": "00000000000000000000000000000002",
        "phase": "complete",
        "deviceIds": ["node-1"],
        "confirmed": True,
        "irreversible": True,
        "migrationDoc": "docs/guides/migration-oc7-org-lifecycle.md",
        "expectedRevision": 3,
        "authorityDeviceId": "node-authority",
        "orgfsMarked": True,
        "tailnetPhase": "shutdown",
        "tailnetClosed": True,
        "tailnetForgotten": False,
        "deletedRecord": "/home/alice/.hyprial/state/org/deleted-orgs/group-hyprial-dev.json",
    }


_IDENTITY_DIGESTS = {
    "alice": "ff0c09a66b307adf9ec40813a6e4c8d67836af263c12da35b6096ec152dfe2a2",
    "alex": "39dc5614918a3ca7b85bc3374a1350ca5f5f04fef5f3b412769bfee2ce45c553",
}
_KEY_DIGESTS = [
    "35d8856a0dad8d8bef23ca0d9d9c83b1172c3eb5002ed169d22cf1045339e263",
    "6c3e7d35f1eb9c2a438420045de6c6aa636cceb6ad52e7ca4df3af0d9816d88f",
]
