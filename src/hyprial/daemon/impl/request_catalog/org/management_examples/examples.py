"""Synthetic, schema-checked examples for the management request group.

Decision record (daemon-request examples, 2026-11-03)
- Every case is a projection of the adjacent MethodSpec and uses only values
  observed from the corresponding daemon producer or a legal producer state.
- Paths, identities, links, timestamps and peer names are synthetic.  No case
  reads a home, socket, credential store or live daemon.
- ``org.delete`` is the OC7 two-phase deletion: the preview case binds the
  journal operation, the confirmed case records the completed tombstone and
  tailnet shutdown, and the error cases pin the stale-confirmation and
  already-deleted refusals.
"""

from __future__ import annotations

from .values import delete_complete, delete_preview, invite_link, removal_receipt

from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
)

_ORG = "group-hyprial-dev"
_SPACE = "space-hyprial-dev"
_ACTOR = "agent:acme:node-1:worker"
_PEER = "agent:acme:node-2:worker"

_TOP_RESULT = {
    "daemon": {
        "pid": 4242,
        "nodeId": "node-1",
        "owner": "alice",
        "epoch": "epoch-example",
        "dispatchWithoutPacCount": 0,
        "dispatchConversationCount": 0,
    },
    "nowMs": 1762075800000,
    "actors": [],
    "stranded": [],
    "quota": {"sources": []},
}


_NETWORK_RESULT = {
    "orgs": [_ORG],
    "unavailableOrgs": [],
    "dataProblems": [],
    "directory": {
        _ORG: [
            {
                "deviceId": "node-1",
                "owner": "alice",
                "serverPublic": "nodekey:" + "1" * 64,
                "clientPublic": "nodekey:" + "2" * 64,
                "clientPublicByServer": {},
                "addressPresent": True,
                "keyGeneration": 1,
                "updatedAt": "2026-11-03T00:00:00+00:00",
                "presence": "unknown",
            }
        ]
    },
    "directoryPending": {_ORG: {"contentPending": [], "parkedCommits": None}},
    "self": {
        "deviceId": "node-1",
        # Null until P1 provisioning mints the device uid; a provisioned,
        # id-form device also stamps it on its own directory row.
        "deviceUid": None,
        "owner": "alice",
        "serverPublic": "nodekey:" + "1" * 64,
        "addressPresent": True,
    },
    "mappedPeers": [
        {"peer": "node-2", "endpoint": "tcp/127.0.0.1:41002", "dialed": False}
    ],
    "sidecar": {
        "policy": {"mode": "auto", "source": "default"},
        "endpoints": ["tcp/127.0.0.1:41002"],
        "dialed": [],
        "restartRequired": True,
        "lastPoll": {
            "atMs": 1762075800000,
            "ok": True,
            "error": None,
            "peersReported": True,
            "peerCount": 1,
            "derpMapURL": None,
            "derpMapSource": None,
        },
        "slots": None,
        "connectionTelemetry": {"state": "unavailable", "reason": "capability-absent"},
        "derpMapURL": None,
        "derpMapSource": None,
    },
    "paneStatus": {
        "directory": {"available": True},
        "mappedPeers": {"available": True},
        "sidecar": {"available": True},
    },
    "sources": {
        "directory": "orgfs",
        "mappedPeers": "forwarding-sidecar",
        "sidecar": "forwarding-sidecar",
    },
}

_REPAIR_CANDIDATE = {
    "org": _ORG,
    "spaceId": _SPACE,
    "reason": "missing-acl-policy",
    "repairable": True,
}
_REPAIRED_ORG = {
    "org": _ORG,
    "spaceId": _SPACE,
    "aclSpaceId": "space-hyprial-dev-acl",
    "space": {
        "spaceId": _SPACE,
        "name": _ORG,
        "owner": "user:alice",
        "createdAt": "2026-11-03T00:00:00+00:00",
    },
    "aclSpace": {
        "spaceId": "space-hyprial-dev-acl",
        "name": "group-hyprial-dev-acl",
        "owner": "user:alice",
        "createdAt": "2026-11-03T00:00:00+00:00",
    },
    "existed": True,
    "aclExisted": True,
    "casdoorGroup": "not-configured",
    "tailnetInitialized": True,
    "published": False,
    "devicePublished": False,
}


def _invalid(
    method: str,
    params: object,
    message: str = "parameters do not match the declared schema",
    data: dict[str, object] | None = None,
) -> MethodExample:
    return MethodExample(
        name="schema-invalid",
        kind="invalid-params",
        description=f"{method} rejects a schema-invalid request before dispatch",
        params=params,
        error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, message, data),
    )


def _not_ready(
    method: str, params: dict[str, object], message: str | None = None
) -> MethodExample:
    return MethodExample(
        name="service-not-ready",
        kind="domain-error",
        description=f"the producer for {method} is not wired",
        params=params,
        error=MethodExampleError(
            ipc_errors.DAEMON_NOT_READY, message or f"{method} is not wired"
        ),
    )


_HOSTS = MethodExampleSet(
    method="hosts",
    cases=(
        MethodExample(
            name="online-host",
            kind="success",
            description="the status producer returns one online host",
            params={},
            result={
                "hosts": [
                    {
                        "nodeId": "node-1",
                        "status": "online",
                        "observedAtMs": 1762075800000,
                        "ageMs": 0,
                        "stale": False,
                        "lastError": None,
                    }
                ]
            },
        ),
        MethodExample(
            name="no-hosts",
            kind="empty",
            description="the presence snapshot has no remote hosts",
            params={},
            result={"hosts": []},
            reason="the status producer legally returns an empty host projection",
        ),
        _invalid("hosts", {"refresh": "yes"}),
        _not_ready("hosts", {"refresh": False}),
    ),
)

_TOP = MethodExampleSet(
    method="top.snapshot",
    cases=(
        MethodExample(
            name="empty-runtime",
            kind="success",
            description="the status producer returns the required diagnostic envelope",
            params={},
            result=_TOP_RESULT,
        ),
        MethodExample(
            name="no-actors",
            kind="empty",
            description="the diagnostic snapshot has no registered actors",
            params={},
            result=_TOP_RESULT,
            reason="empty actor and stranded projections are legal status output",
        ),
        _invalid("top.snapshot", {"verbose": True}),
        _not_ready("top.snapshot", {}),
    ),
)


_ORG_EXECUTE = MethodExampleSet(
    method="org.execute",
    empty_not_applicable="an execute either returns the four bootstrap steps or a typed error",
    cases=(
        MethodExample(
            name="bootstrap-complete",
            kind="success",
            description="the real join producer reports each completed bootstrap step",
            params={"link": invite_link(expired=False)},
            result={
                "org": _ORG,
                "spaceId": _SPACE,
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
                        "detail": "mapped 1 member device(s)",
                    },
                ],
            },
        ),
        _invalid("org.execute", {"link": 3}),
        MethodExample(
            name="expired-invite",
            kind="domain-error",
            description="the invite producer rejects an expired link",
            params={"link": invite_link(expired=True)},
            error=MethodExampleError(
                "INVITE_EXPIRED", "invite expired at 2000-01-01T00:00:00Z"
            ),
        ),
    ),
)


def _removal_set(method: str, *, user: str | None = None) -> MethodExampleSet:
    params = {"org": _ORG} | ({"user": user} if user is not None else {})
    return MethodExampleSet(
        method=method,
        empty_not_applicable="the removal producer returns a receipt or a typed error; it has no legal empty result",
        cases=(
            MethodExample(
                name="pending-confirmation",
                kind="success",
                description="the removal producer returns the required organization receipt",
                params=params,
                result={
                    "org": _ORG,
                    "spaceId": _SPACE,
                    **removal_receipt(user),
                    **({"user": user} if user is not None else {}),
                },
            ),
            _invalid(method, {"org": 4} if user is None else {"org": _ORG, "user": 4}),
            MethodExample(
                name="authority-refused",
                kind="domain-error",
                description="the producer refuses an authority-changing operation",
                params=params,
                error=MethodExampleError(
                    "LAST_AUTHORITY_REFUSED",
                    "Authority cannot be removed; use a transfer maintenance procedure.",
                ),
            ),
        ),
    )


_ORG_DELETE = MethodExampleSet(
    method="org.delete",
    empty_not_applicable="org.delete has no legal empty result",
    cases=(
        MethodExample(
            name="preview",
            kind="success",
            description="the delete producer binds a durable journal operation awaiting confirmation",
            params={"org": _ORG, "confirmed": False},
            result=delete_preview(),
        ),
        MethodExample(
            name="confirmed-complete",
            kind="success",
            description="the confirmed delete tombstones the org and shuts its tailnet down",
            params={"org": _ORG, "confirmed": True, "operationId": "00000000000000000000000000000002"},
            result=delete_complete(),
        ),
        _invalid("org.delete", {"org": 4}),
        MethodExample(
            name="confirmation-stale",
            kind="domain-error",
            description="the confirmation does not name the journaled preview operation",
            params={"org": _ORG, "confirmed": True, "operationId": "stale"},
            error=MethodExampleError(
                "REMOVAL_CONFIRMATION_STALE",
                "Deletion confirmation for org 'group-hyprial-dev' is stale; re-run the preview and confirm its exact operation.",
            ),
        ),
        MethodExample(
            name="already-deleted",
            kind="domain-error",
            description="the org is already marked deleted on this home",
            params={"org": _ORG},
            error=MethodExampleError(
                ipc_errors.ORG_ALREADY_DELETED,
                "org 'group-hyprial-dev' is already marked deleted; follow docs/guides/migration-oc7-org-lifecycle.md",
            ),
        ),
    ),
)

_ORG_NETWORK = MethodExampleSet(
    method="org.network",
    cases=(
        MethodExample(
            name="configured-network",
            kind="success",
            description="the network producer returns a complete configured three-pane projection",
            params={"org": _ORG},
            result=_NETWORK_RESULT,
        ),
        MethodExample(
            name="no-organizations",
            kind="empty",
            description="the directory has no organizations and forwarding is off",
            params={},
            result={
                **_NETWORK_RESULT,
                "orgs": [],
                "directory": {},
                "directoryPending": {},
                "mappedPeers": None,
                "sidecar": {
                    "policy": {"mode": "auto", "source": "default"},
                    "endpoints": None,
                    "dialed": None,
                    "restartRequired": None,
                    "lastPoll": None,
                    "slots": None,
                    "connectionTelemetry": {
                        "state": "unavailable",
                        "reason": "capability-absent",
                    },
                    "derpMapURL": None,
                    "derpMapSource": None,
                },
            },
            reason="a provisioned device may belong to no organization",
        ),
        _invalid("org.network", {"peerKey": 9}),
        MethodExample(
            name="unknown-space",
            kind="domain-error",
            description="the network producer reports an unavailable organization space",
            params={"org": _ORG},
            error=MethodExampleError(
                "unknown-space",
                "unknown-space: unknown-space",
                {"org": _ORG, "space": _ORG},
            ),
        ),
    ),
)

_ORG_PUBLISH = MethodExampleSet(
    method="org.publish",
    empty_not_applicable="publish returns publication flags or a typed error; it has no legal empty result",
    cases=(
        MethodExample(
            name="published",
            kind="success",
            description="the publish producer reports both publication flags",
            params={},
            result={"published": True, "orgfsPublished": True},
        ),
        _invalid("org.publish", {"org": _ORG}),
        _not_ready("org.publish", {}, "org-context publish is not wired"),
    ),
)

_ORG_REPAIR = MethodExampleSet(
    method="org.repair",
    cases=(
        MethodExample(
            name="repairable-candidate",
            kind="success",
            description="the repair producer previews and applies one creator-owned incomplete org",
            params={"apply": True},
            result={
                "apply": True,
                "candidates": [_REPAIR_CANDIDATE],
                "repaired": [_REPAIRED_ORG],
            },
        ),
        MethodExample(
            name="empty-candidate-list",
            kind="empty",
            description="the repair scan legally has no incomplete organizations",
            params={"apply": False},
            result={"apply": False, "candidates": [], "repaired": []},
            reason="repair_candidates returns an empty list when no repair is needed",
        ),
        _invalid("org.repair", {"apply": "yes"}),
        MethodExample(
            name="orgfs-closing",
            kind="domain-error",
            description="the orgfs directory read refuses while its lifecycle is closing",
            params={"apply": False},
            error=MethodExampleError(
                "unavailable", "unavailable: orgfs runtime is closing", {}
            ),
        ),
    ),
)

ORG_MANAGEMENT_EXAMPLES = (
    _HOSTS,
    _TOP,
    _ORG_DELETE,
    _ORG_EXECUTE,
    _removal_set("org.leave"),
    _ORG_NETWORK,
    _ORG_PUBLISH,
    _removal_set("org.remove", user="user:alex"),
    _ORG_REPAIR,
)

__all__ = ["ORG_MANAGEMENT_EXAMPLES"]

TOP_INSPECT_EXAMPLES = (MethodExampleSet(
    method="top.inspect",
    cases=(
        MethodExample(name="one-actor", kind="success", description="One observed actor has process metrics.", params={},
            result={**_TOP_RESULT, "actors": [{"actor": _ACTOR, "name": "worker", "runtime": "pi", "status": "online",
                "processState": "running", "sources": [], "running": True, "pid": 123,
                "processStartedAtMs": None, "turnCount": None, "lastTurnEndedAtMs": None,
                "recentTurnDurationsMs": None, "recentTurnOutcomes": None, "recentFailureRatio": None,
                "openTurnStartedAtMs": None, "pendingCount": 0, "oldestPendingAgeMs": None,
                "state": "ok",
                "processMetrics": {"cpuPercent": 1.0, "rssKb": 2048, "cpuSeconds": 2.0, "elapsedSeconds": 10.0}}]}),
        MethodExample(name="no-process-sample", kind="success", description="An actor has no available process sample.",
            params={}, result={**_TOP_RESULT, "actors": [{"actor": _ACTOR, "name": "worker", "runtime": "pi",
                "status": "offline", "processState": "stopped", "sources": [], "running": False, "pid": None,
                "processStartedAtMs": None, "turnCount": None, "lastTurnEndedAtMs": None,
                "recentTurnDurationsMs": None, "recentTurnOutcomes": None, "recentFailureRatio": None,
                "openTurnStartedAtMs": None, "pendingCount": 0, "oldestPendingAgeMs": None,
                "state": "stopped", "processMetrics": None}]}),
        MethodExample(name="no-actors", kind="empty", description="There are no registered actors.",
            params={}, result=_TOP_RESULT, reason="The snapshot may contain no actors."),
        _invalid("top.inspect", {"pid": 123}, "top.inspect params do not match the declared schema at <root>"),
        _not_ready("top.inspect", {}, "top.snapshot is not wired"),
    ),
),)
