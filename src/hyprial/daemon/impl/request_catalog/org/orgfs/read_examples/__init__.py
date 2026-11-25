"""OrgFS read examples replayed against the facade, runtime and wire producers.

Decision record (daemon-request-examples, 2026-11-03): use canonical identifiers,
relative paths and real opaque versions. Empty collections are declared only
where the producer returns them; missing paths are errors. Synthetic identifiers
and timestamps are normalized by the producer replay tests, never output fields.
"""

from __future__ import annotations

from hyprial.kernel import (
    MethodExample, MethodExampleError, MethodExampleSet, canonical_orgfs_uri, ipc_errors,
)

__all__ = ["ORGFS_READ_EXAMPLES"]

_SPACE_ID = "b363d762-a06e-496d-94d0-e4695b66e206"
_NODE_ID = "89fd8adf4f8f43f48f475bef6914c61f"
_VERSION = "eyJjb250ZW50RnJvbnRpZXIiOjEsInRyZWVGcm9udGllciI6MX0"
_REMOVED_VERSION = "eyJjb250ZW50RnJvbnRpZXIiOjIsInRyZWVGcm9udGllciI6Mn0"
_PLAN_ID = 'b9d31a5e951e7342d974a2491e41fe4e04624c0a685364d94412e43c4b31f878'
_AT = "2026-11-03T09:00:00Z"
_SPACE = {
    "space_id": _SPACE_ID, "name": "example-space", "owner": "user:example",
    "created_at": _AT,
}
_NODE = {
    "space_id": _SPACE_ID, "node_id": _NODE_ID, "kind": "doc",
    "name": "readme.txt", "path": "readme.txt", "size": 12,
    "blob_hash": None, "doc_id": "doc-b6fa1d65-6c38-4232-a3ab-15cdc45f8949",
    "version": _VERSION, "modified_by": "user:example", "modified_via": None,
    "name_conflict": False, "deleted": False, "contentState": "arrived",
    "uri": canonical_orgfs_uri("example", _SPACE_ID, _NODE_ID),
}
_HISTORY_NODE = {key: value for key, value in _NODE.items() if key != "contentState"}
_HISTORY_NODE["content_state"] = "arrived"
_MEMBER = {
    "space_id": _SPACE_ID, "user": "user:example", "mode": "rw",
    "added_by": "user:example", "added_at": _AT,
}
_REF = {"spaceId": _SPACE_ID, "node": _NODE["uri"]}
_PATH = {"spaceId": _SPACE_ID, "path": "readme.txt"}
_DOC = "daemon/impl/orgfs/document/"
_BRIDGE = "daemon/impl/application/messaging/orgfs_bridge.py:_handle_orgfs/_orgfs_json"
_UNKNOWN = MethodExampleError("unknown-space", "unknown-space: unknown-space", {"spaceId": "missing-space"})
_INVALID = MethodExampleError(ipc_errors.INVALID_ARGUMENT, "body must be a JSON object")


def _error(name, params, code, data, producer, *, message=None, kind="semantic-param-error"):
    return MethodExample(
        name=name, kind=kind, params=params,
        description=f"producer: {producer}; schema-valid input reaches the producer",
        error=MethodExampleError(code, message or f"{code}: {code}", data),
    )


def _set(method, params, result, *, producer, empty=None, empty_params=None,
         empty_reason="", gap="", errors=(), domain_gap=""):
    cases = [
        MethodExample(
            name="producer-success", kind="success", params=params, result=result,
            description=f"producer: {producer}; serialized by {_BRIDGE}",
        ),
        MethodExample(
            name="schema-invalid", kind="invalid-params", params=[], error=_INVALID,
            description="request_http/app.py:_read_params rejects a non-object before dispatch",
        ),
    ]
    if empty is not None:
        cases.append(MethodExample(
            name="producer-empty", kind="empty", params=empty_params or params,
            result=empty, description=f"producer: {producer}; {empty_reason}",
            reason=empty_reason,
        ))
    if method != "orgfs.spaces":
        cases.append(MethodExample(
            name="unknown-space", kind="domain-error",
            params={**params, "spaceId": "missing-space"}, error=_UNKNOWN,
            description=f"producer: {_DOC}projection.py:FacadeProjection._space; no loaded space matches the supplied ID",
        ))
    cases.extend(errors)
    return MethodExampleSet(
        method=method, cases=tuple(cases), empty_not_applicable=gap,
        domain_errors_not_applicable=domain_gap,
    )


def _absolute_path():
    return _error(
        "absolute-path", {"spaceId": _SPACE_ID, "path": "/readme.txt"},
        ipc_errors.INVALID_ARGUMENT, {}, f"{_DOC}projection.py:_validate_path",
        message="invalid-argument: absolute paths are not accepted",
    )


def _unknown_version():
    return _error(
        "unknown-version", {**_REF, "version": "unknown-version"},
        "unknown-doc", {"version": "unknown-version"},
        f"{_DOC}operations/tree.py:FacadeTreeHistory.read_at/stat_at",
    )


ORGFS_READ_EXAMPLES = (
    _set(
        "orgfs.spaces", {}, {"spaces": [_SPACE]},
        producer=f"{_DOC}operations/spaces.py:FacadeSpaces.spaces",
        empty={"spaces": []}, empty_reason="a fresh local runtime has no loaded spaces",
        domain_gap="FacadeSpaces.spaces enumerates loaded SpaceInfo records without a space argument or domain refusal; initialization/infrastructure failures are outside this producer",
    ),
    _set(
        "orgfs.resolve", _PATH, {"nodes": [_NODE]},
        producer=f"{_DOC}projection.py:FacadeProjection.resolve/_resolve_ids",
        gap="_resolve_ids returns at least one node or raises unknown-doc; a missing path is not an empty match",
        errors=(_absolute_path(),),
    ),
    _set(
        "orgfs.ls", {"spaceId": _SPACE_ID, "path": ""}, {"nodes": [_NODE]},
        producer=f"{_DOC}operations/content.py:FacadeContent.listdir",
        empty={"nodes": []}, empty_reason="a newly created root directory has no children",
        errors=(_absolute_path(),),
    ),
    _set(
        "orgfs.stat", _REF, _NODE,
        producer=f"{_DOC}operations/content.py:FacadeContent.stat",
        gap="stat returns one NodeInfo or a domain error, never an absent node object",
        errors=(_absolute_path(),),
    ),
    _set(
        "orgfs.read", {**_REF, "waitSeconds": 0},
        {"node": _NODE, "text": "hello world!", "version": _VERSION},
        producer=f"{_DOC}operations/content.py:FacadeContent.stat/read_text",
        empty={"node": {**_NODE, "size": 0}, "text": "", "version": _VERSION},
        empty_reason="a zero-byte text document is present and returns empty text with its node and version",
        errors=(_absolute_path(),),
    ),
    _set(
        "orgfs.read_at", {**_REF, "version": _VERSION}, {"contentB64": "aGVsbG8gd29ybGQh"},
        producer=f"{_DOC}operations/tree.py:FacadeTreeHistory.read_at",
        empty={"contentB64": ""}, empty_reason="a saved zero-byte document snapshot has empty base64 content",
        errors=(_unknown_version(),),
    ),
    _set(
        "orgfs.stat_at", {**_REF, "version": _VERSION}, _NODE,
        producer=f"{_DOC}operations/tree.py:FacadeTreeHistory.stat_at/_snapshot_info",
        gap="stat_at returns one historical NodeInfo or unknown-doc, never an absent object",
        errors=(_unknown_version(),),
    ),
    _set(
        "orgfs.history", {**_REF, "limit": 50},
        {"history": [{"node": _HISTORY_NODE, "changed": "created", "author": "user:example", "actor": None, "at": _AT}]},
        producer=f"{_DOC}operations/tree.py:FacadeTreeHistory.history",
        empty={"history": []}, empty_params={**_REF, "before": _VERSION, "limit": 50},
        empty_reason="before excludes the only retained creation version",
        errors=(_absolute_path(),),
    ),
    _set(
        "orgfs.status", {"spaceId": _SPACE_ID},
        {"space_id": _SPACE_ID, "unconfirmed_commits": 5, "durable_holders_seen": [], "holders_online": [], "parked_commits": []},
        producer="daemon/impl/orgfs/composition/content.py:RuntimeContent.status -> FacadeContent.status (fresh durable space plus one write, no remote holders)",
        gap="status always returns a populated space status record; empty holder arrays are fields, not an absent status",
    ),
    _set(
        "orgfs.trash", {"spaceId": _SPACE_ID, "limit": 100},
        {"nodes": [{**_NODE, "deleted": True, "version": _REMOVED_VERSION}]},
        producer=f"{_DOC}operations/tree.py:FacadeTreeHistory.trash",
        empty={"nodes": []}, empty_reason="the space has no removed nodes",
    ),
    _set(
        "orgfs.watch", {"spaceId": _SPACE_ID, "glob": "*"},
        {"events": [{"spaceId": _SPACE_ID, "kind": "created", "node": _NODE, "oldPath": None}]},
        producer="daemon/impl/orgfs/composition/lifecycle.py:RuntimeLifecycle.watch_events -> FacadeContent.changes_since",
        empty={"events": []}, empty_params={"spaceId": _SPACE_ID, "glob": "*", "sinceVersion": _VERSION},
        empty_reason="the cursor already includes the sole creation event",
    ),
    _set(
        "orgfs.members", {"spaceId": _SPACE_ID}, {"members": [_MEMBER]},
        producer=f"{_DOC}operations/spaces.py:FacadeSpaces.members",
        gap="normal created/joined spaces retain their owner membership; remove_member refuses removing the owner, so this catalog does not invent a memberless space",
    ),
    _set(
        "orgfs.purge_status", {"spaceId": _SPACE_ID, "planId": _PLAN_ID},
        {"plan_id": _PLAN_ID, "acknowledged": [], "pending": ["node-example"]},
        producer="daemon/impl/orgfs/composition/checkout.py:RuntimeCheckout.purge_status -> FacadePurge.purge_status (created plan, not yet executed)",
        gap="purge_status returns a plan receipt or stale-plan; absent plans are errors",
        errors=(_error(
            "stale-plan", {"spaceId": _SPACE_ID, "planId": "missing-plan"},
            "stale-plan", {"planId": "missing-plan"},
            f"{_DOC}operations/purge.py:FacadePurge.purge_status",
        ),),
    ),
)
