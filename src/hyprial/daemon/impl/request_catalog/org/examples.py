"""Same-source Contract Mock cases for the declared org request methods.

Decision record (org-members batch 1, 2026-11-02)
- Example data lives beside the declarations and is validated against the
  owning ``MethodSpec`` schemas by the exporter; there is no second org DTO.
- ``org.list`` covers the empty organization list, a work-org owner view with
  a null role and a null space id, and the orgfs ambiguity rejection.
- ``org.fetch`` covers the two real result shapes: the mesh result and the
  orgfs result with its optional ``source``/``refreshed``/``spaceId`` fields,
Decision record (org lifecycle batch 2, 2026-11-02)
- ``org.create`` carries the two real result situations the daemon returns:
  a first create that published this device, and a resumed create whose
  device row was already current. Both publish flags confirm the requested
  state, including the idempotent case without another write.
- ``org.invite`` covers the three link forms plus the ACL modes; the link
  strings here are synthetic and are not redeemable credentials.
- ``org.join`` shows the four bootstrap steps in execution order and the
  primary domain failures: a malformed link, an expired link, and an offline
  holder (``no-holder-online`` with the space it could not reach).
- Error messages are the ones the handlers really produce; no example invents
  a success for reject/rename/update, which have no daemon method.
"""

from __future__ import annotations

from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
)

__all__ = ["ORG_REQUEST_EXAMPLES"]

_SYNTHETIC_PENDING_PATH = "/tmp/hyprial-example/org-context/pending.json"
_SYNTHETIC_RECEIVED_AT = "2026-11-02T09:30:00Z"

_ORG_LIST = MethodExampleSet(
    method="org.list",
    cases=(
        MethodExample(
            name="work-org-owner",
            kind="success",
            description="one joined work organization with an owner role",
            params={},
            result={
                "ok": True,
                "notices": [],
                "orgs": [
                    {
                        "org": "group-hyprial-dev",
                        "spaceId": "space-hyprial-dev",
                        "role": "owner",
                        "devices": 3,
                        "state": "active",
                        "authorityDevice": True,
                        "kind": "work",
                        "current": False,
                    }
                ],
                "leftOrgs": [],
            },
        ),
        MethodExample(
            name="no-joined-orgs",
            kind="empty",
            description="this node has no joined organization yet",
            params={},
            result={"ok": True, "notices": [], "orgs": [], "leftOrgs": ["group-acme-lab"]},
            reason="a node that left its only organization has a legal empty list",
        ),
        MethodExample(
            name="non-object-body",
            kind="invalid-params",
            description="the empty object schema still rejects a non-object body",
            params=[],
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "params must be a JSON object"
            ),
        ),
        MethodExample(
            name="network-fallback",
            kind="domain-error",
            description="an unexpected orgfs failure is reported without claiming a specific code",
            params={},
            error=MethodExampleError(
                "ORG_NETWORK_ERROR",
                "the organization directory could not be read",
            ),
        ),
        MethodExample(
            name="ambiguous-space",
            kind="domain-error",
            description="two spaces carry the same org directory name",
            params={},
            error=MethodExampleError(
                "ambiguous-space",
                "more than one space carries the org directory name",
                {"space": "group-hyprial-dev", "count": 2},
            ),
        ),
    ),
)

_ORG_FETCH = MethodExampleSet(
    method="org.fetch",
    cases=(
        MethodExample(
            name="mesh-with-candidates",
            kind="success",
            description="the mesh answered with two candidates, one was rejected",
            params={"timeoutSeconds": 2, "requestMode": "neighbors"},
            result={
                "requestedSources": ["node-2", "node-3"],
                "responseCount": 2,
                "receivedCount": 1,
                "rejectedCount": 1,
                "candidates": [
                    {
                        "path": _SYNTHETIC_PENDING_PATH,
                        "version": 7,
                        "publisher": "user:lin",
                        "receivedAt": _SYNTHETIC_RECEIVED_AT,
                        "source": "node-2",
                    }
                ],
            },
        ),
        MethodExample(
            name="mesh-no-candidates",
            kind="empty",
            description="no neighbor offered a candidate before the timeout",
            params={"timeoutSeconds": 2, "requestMode": "neighbors"},
            result={
                "requestedSources": [],
                "responseCount": 0,
                "receivedCount": 0,
                "rejectedCount": 0,
                "candidates": [],
            },
            reason="a mesh with no answering source is a legal successful fetch",
        ),
        MethodExample(
            name="orgfs-refreshed",
            kind="success",
            description="the orgfs fetch shape with a deprecated source target",
            params={"timeoutSeconds": 2, "from": "node-2"},
            result={
                "requestedSources": ["node-2"],
                "responseCount": 1,
                "receivedCount": 1,
                "rejectedCount": 0,
                "candidates": [
                    {
                        "path": _SYNTHETIC_PENDING_PATH,
                        "version": 8,
                        "publisher": "user:lin",
                        "receivedAt": _SYNTHETIC_RECEIVED_AT,
                        "source": "node-2",
                    }
                ],
                "source": "orgfs",
                "refreshed": True,
                "spaceId": "space-hyprial-context",
                "deprecatedSourceTarget": True,
                "sourceTargetHint": "use --from user:<name>",
            },
        ),
        MethodExample(
            name="timeout-not-positive",
            kind="invalid-params",
            description="the declared timeout has an exclusive lower bound",
            params={"timeoutSeconds": 0},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than 0 and at most 30",
            ),
        ),
        MethodExample(
            name="source-unreachable",
            kind="domain-error",
            description="the selected source is not an online target",
            params={"timeoutSeconds": 2, "from": "node-9"},
            error=MethodExampleError(
                ipc_errors.ORG_SOURCE_UNREACHABLE,
                "org-context source is not an online hyprial target",
                {"availableSources": ["node-2"]},
            ),
        ),
        MethodExample(
            name="space-not-joined",
            kind="domain-error",
            description="this node has not joined the org-context space",
            params={"timeoutSeconds": 2},
            error=MethodExampleError(
                ipc_errors.ORG_SPACE_NOT_JOINED,
                "this node has not joined the org-context space",
                {"hint": "hyprial org fs space join <spaceId>"},
            ),
        ),
        MethodExample(
            name="ambiguous-space",
            kind="domain-error",
            description="two spaces carry the same org directory name",
            params={"timeoutSeconds": 2, "requestMode": "neighbors"},
            error=MethodExampleError(
                "ambiguous-space",
                "more than one space carries the org directory name",
                {"org": "group-hyprial-dev", "space": "group-hyprial-dev", "count": 2},
            ),
        ),
    ),
)

_SYNTHETIC_SPACE_ID = "space-hyprial-dev"
_SYNTHETIC_ACL_SPACE_ID = "space-hyprial-dev-acl"
_SYNTHETIC_CREATED_AT = "2026-11-02T09:30:00Z"
_SYNTHETIC_EXPIRES_AT = "2026-11-02T10:30:00Z"

_ORG_CREATE = MethodExampleSet(
    method="org.create",
    empty_not_applicable="a create either returns both spaces or a typed error",
    cases=(
        MethodExample(
            name="created-and-published",
            kind="success",
            description="a first create with a current device row",
            params={"org": "group-hyprial-dev"},
            result={
                "org": "group-hyprial-dev",
                "spaceId": _SYNTHETIC_SPACE_ID,
                "aclSpaceId": _SYNTHETIC_ACL_SPACE_ID,
                "space": {
                    "spaceId": _SYNTHETIC_SPACE_ID,
                    "name": "group-hyprial-dev",
                    "owner": "user:lin",
                    "createdAt": _SYNTHETIC_CREATED_AT,
                },
                "aclSpace": {
                    "spaceId": _SYNTHETIC_ACL_SPACE_ID,
                    "name": "group-hyprial-dev-acl",
                    "owner": "user:lin",
                    "createdAt": _SYNTHETIC_CREATED_AT,
                },
                "existed": False,
                "aclExisted": False,
                "casdoorGroup": "verified",
                "tailnetInitialized": True,
                "published": True,
                "devicePublished": True,
            },
        ),
        MethodExample(
            name="resumed-without-republishing",
            kind="success",
            description=(
                "a resumed create whose device row was already current, so both "
                "publish flags confirm the requested state without another write"
            ),
            params={"org": "group-hyprial-dev"},
            result={
                "org": "group-hyprial-dev",
                "spaceId": _SYNTHETIC_SPACE_ID,
                "aclSpaceId": _SYNTHETIC_ACL_SPACE_ID,
                "space": {
                    "spaceId": _SYNTHETIC_SPACE_ID,
                    "name": "group-hyprial-dev",
                    "owner": "user:lin",
                    "createdAt": _SYNTHETIC_CREATED_AT,
                },
                "aclSpace": {
                    "spaceId": _SYNTHETIC_ACL_SPACE_ID,
                    "name": "group-hyprial-dev-acl",
                    "owner": "user:lin",
                    "createdAt": _SYNTHETIC_CREATED_AT,
                },
                "existed": True,
                "aclExisted": True,
                "casdoorGroup": "not-configured",
                "tailnetInitialized": True,
                "published": True,
                "devicePublished": True,
            },
        ),
        MethodExample(
            name="org-missing",
            kind="invalid-params",
            description="org is required",
            params={},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "org must be a non-empty string"
            ),
        ),
        MethodExample(
            name="org-name-rejected",
            kind="semantic-param-error",
            description="the org name must be a group-<slug> name",
            params={"org": "Not A Name"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "org name must start with 'group-', match [a-z0-9][a-z0-9-]{0,62}, "
                "and not end with reserved '-acl': 'Not A Name'",
            ),
        ),
        MethodExample(
            name="ambiguous-directory-space",
            kind="domain-error",
            description="two spaces carry the same org directory name",
            params={"org": "group-hyprial-dev"},
            error=MethodExampleError(
                "ambiguous-space",
                "ambiguous-space: ambiguous-space",
                {"space": "group-hyprial-dev", "count": 2},
            ),
        ),
        MethodExample(
            name="device-record-missing",
            kind="domain-error",
            description="the node has no device key to seed the new tailnet with",
            params={"org": "group-hyprial-dev"},
            error=MethodExampleError(
                "DEVICE_RECORD_MISSING",
                "no device record under the hyprial home; run login to provision "
                "the device key first",
            ),
        ),
    ),
)

_ORG_INVITE = MethodExampleSet(
    method="org.invite",
    empty_not_applicable="an invite either returns the three link forms or a typed error",
    cases=(
        MethodExample(
            name="invite-created",
            kind="success",
            description="one invite with the https, deep-link and token forms",
            params={"org": "group-hyprial-dev", "user": "user:bob@example"},
            result={
                "org": "group-hyprial-dev",
                "spaceId": _SYNTHETIC_SPACE_ID,
                "aclSpaceId": _SYNTHETIC_ACL_SPACE_ID,
                "user": "user:bob@example",
                "link": "https://hyprial.ai/join#v2.example-payload",
                "deepLink": "hyprial://join#v2.example-payload",
                "command": "hyprial org join 'https://hyprial.ai/join#v2.example-payload'",
                "invite": "hyprial-invite:v2:example-payload",
                "expiresAt": _SYNTHETIC_EXPIRES_AT,
                "member": {
                    "spaceId": _SYNTHETIC_SPACE_ID,
                    "user": "user:bob@example",
                    "mode": "rw",
                },
                "aclMember": {
                    "spaceId": _SYNTHETIC_ACL_SPACE_ID,
                    "user": "user:bob@example",
                    "mode": "ro",
                },
            },
        ),
        MethodExample(
            name="user-missing",
            kind="invalid-params",
            description="user is required alongside org",
            params={"org": "group-hyprial-dev"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "user must be a non-empty string"
            ),
        ),
        MethodExample(
            name="user-not-a-user-uri",
            kind="semantic-param-error",
            description="a user:<owner> URI may not carry another colon",
            params={"org": "group-hyprial-dev", "user": "a:b"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "user identity owner must not contain ':'",
            ),
        ),
        MethodExample(
            name="policy-denies-missing-org",
            kind="domain-error",
            description=(
                "a missing org fails closed in the ACL policy before any space "
                "lookup, so the code is authorization, not unknown-space"
            ),
            params={"org": "group-missing", "user": "user:bob@example"},
            error=MethodExampleError(
                "ORG_ACTION_NOT_AUTHORIZED",
                "'invite' on 'org:members' in org 'group-missing' is not allowed",
            ),
        ),
        MethodExample(
            name="address-not-written",
            kind="domain-error",
            description="the forwarding sidecar has not written this device's address",
            params={"org": "group-hyprial-dev", "user": "user:bob@example"},
            error=MethodExampleError(
                "DEVICE_ADDRESS_UNAVAILABLE",
                "the forwarding sidecar has not written this device's Tailcat "
                "address yet; retry once the daemon is connected",
            ),
        ),
    ),
)

_ORG_JOIN = MethodExampleSet(
    method="org.join",
    empty_not_applicable="a join returns the executed bootstrap steps",
    cases=(
        MethodExample(
            name="all-steps-executed",
            kind="success",
            description="the standard four-step bootstrap ran in order",
            params={"link": "https://hyprial.ai/join#v2.example-payload"},
            result={
                "org": "group-hyprial-dev",
                "spaceId": _SYNTHETIC_SPACE_ID,
                "steps": [
                    {
                        "kind": "connect-inviter",
                        "ok": True,
                        "detail": "tailnet redeemed; selected roster candidates mapped",
                    },
                    {
                        "kind": "join-org",
                        "ok": True,
                        "detail": "joined the org directory and ACL spaces",
                    },
                    {
                        "kind": "register-device",
                        "ok": True,
                        "detail": "registered this device in the org directory",
                    },
                    {
                        "kind": "connect-members",
                        "ok": True,
                        "detail": "mapped 2 member device(s)",
                    },
                ],
            },
        ),
        MethodExample(
            name="link-missing",
            kind="invalid-params",
            description="link is required",
            params={},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "link must be a non-empty string"
            ),
        ),
        MethodExample(
            name="malformed-link",
            kind="domain-error",
            description="an https URL without the /join path is not an invite",
            params={"link": "https://invite.example.org/wrong#v2.abc"},
            error=MethodExampleError(
                "INVITE_MALFORMED",
                "invite https URL path must end with '/join', got "
                "'https://invite.example.org/wrong'",
            ),
        ),
        MethodExample(
            name="expired-link",
            kind="domain-error",
            description="an otherwise valid link whose expiry passed",
            params={"link": "https://hyprial.ai/join#v2.expired-payload"},
            error=MethodExampleError(
                "INVITE_EXPIRED", "invite expired at 2026-11-02T09:00:00Z"
            ),
        ),
        MethodExample(
            name="holder-offline",
            kind="domain-error",
            description="no other holder of the space answered within the join budget",
            params={"link": "https://hyprial.ai/join#v2.example-payload"},
            error=MethodExampleError(
                "no-holder-online",
                "no-holder-online: sync timed out waiting for a holder",
                {"spaceId": _SYNTHETIC_SPACE_ID},
            ),
        ),
    ),
)

ORG_REQUEST_EXAMPLES: tuple[MethodExampleSet, ...] = (
    _ORG_CREATE,
    _ORG_LIST,
    _ORG_FETCH,
    _ORG_INVITE,
    _ORG_JOIN,
)
