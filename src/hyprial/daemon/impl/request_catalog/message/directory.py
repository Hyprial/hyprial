# Decision record (UA1b, 2026-11-18; GUI owner review; UA4): a user agent,
# this machine's (its registry marker) or another machine's name form
# (``user_agent.naming.is_user_agent_address``), is never listed as an agent
# chat target; the server leaves it out.  A later
# slice lists ``user:<owner>`` itself as the deliverable target.
# Slice GC6: the ``targets`` example lists alice's agents reviewer and
# worker with the ``a.`` ids the group examples use for them
# (request_catalog/conversations/group_examples.py), so a group's members
# are agents the directory shows.
# I4b (owner items Q0-a and Q0-d, 2026-11-23): ``channel_route`` leaves the
# target kinds with the retired ``route:<adapter>:<route>`` address.
"""Deliverable target directory projected from the daemon's existing authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    HttpBinding, IdentityBinding, MethodError, MethodExample, MethodExampleError,
    MethodExampleSet, MethodSpec, ipc_errors,
)

_STRING = {"type": "string", "minLength": 1}
_TARGET = {
    "type": "object",
    "properties": {
        "targetKind": {"enum": ["agent", "user"]},
        "targetUri": _STRING,
        "actor": _STRING,
        "status": _STRING,
        "deliverable": {"const": True},
        "hosted": {"type": "boolean"},
        "hostedBy": {"type": ["string", "null"]},
        # Local agent rows only: the stable a.<id> address and the registry
        # display name (falling back to the actor name).
        "agentId": {"type": ["string", "null"]},
        "displayName": {"type": ["string", "null"]},
        "orgs": {"type": "array", "items": _STRING, "uniqueItems": True},
    },
    "required": ["targetKind", "targetUri", "actor", "status", "deliverable"],
    "additionalProperties": False,
}

TARGETS_SPEC = MethodSpec(
    name="targets",
    owner="daemon.messaging.directory",
    params_schema={
        "type": "object",
        "properties": {
            "kind": {"enum": ["agent", "user", None]},
            "actor": {**_STRING, "x-hyprial-http-forbidden": True},
            "from": {**_STRING, "x-hyprial-http-forbidden": True},
            "sessionRef": {**_STRING, "x-hyprial-http-forbidden": True},
        },
        "additionalProperties": False,
    },
    result_schema={
        "type": "object",
        "properties": {
            "targets": {"type": "array", "items": _TARGET},
            "orgDirectoryStatus": {
                "type": "object",
                "additionalProperties": {"enum": ["ok", "partial", "unavailable"]},
            },
        },
        "required": ["targets"],
        "additionalProperties": False,
    },
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT),
        MethodError(ipc_errors.STALE_SESSION),
        MethodError(ipc_errors.SESSION_SUPERSEDED),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    ipc_param_validation="handler",
)


def targets(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_targets(dict(params))


CHAT_DIRECTORY_BINDINGS = ((TARGETS_SPEC, targets),)

TARGETS_EXAMPLES = MethodExampleSet(
    method="targets",
    cases=(
        MethodExample(name="deliverable-agent", kind="success",
                      description=(
                          "the daemon lists alice's two deliverable agents, ordered by "
                          "address; they are the members of the group examples"
                      ),
                      params={"kind": "agent"}, result={"targets": [{
                          "targetKind": "agent", "targetUri": "agent:alice:node:reviewer",
                          "actor": "agent:alice:node:reviewer", "status": "online",
                          "deliverable": True, "hosted": False, "hostedBy": None,
                          "agentId": "a.revieweraaaaaaaaaaaaaaaaaa",
                          "displayName": "reviewer",
                      }, {
                          "targetKind": "agent", "targetUri": "agent:alice:node:worker",
                          "actor": "agent:alice:node:worker", "status": "online",
                          "deliverable": True, "hosted": False, "hostedBy": None,
                          "agentId": "a.workeraaaaaaaaaaaaaaaaaaaa",
                          "displayName": "worker",
                      }]}),
        MethodExample(name="shared-organization-agent", kind="success",
                      description="a confirmed member's Agent appears in the existing chat directory",
                      params={"kind": "agent"}, result={
                          "targets": [{
                              "targetKind": "agent", "targetUri": "agent:bob:node:a." + "b" * 26,
                              "actor": "agent:bob:node:a." + "b" * 26,
                              "status": "unknown", "deliverable": True,
                              "agentId": "a." + "b" * 26,
                              "displayName": "peer", "orgs": ["group-team"],
                          }],
                          "orgDirectoryStatus": {"group-team": "partial"},
                      }),
        MethodExample(name="no-targets", kind="empty",
                      description="a target kind has no configured recipients",
                      params={"kind": "user"}, result={"targets": []},
                      reason="no user agent on this node is a valid directory state"),
        MethodExample(name="non-object-params", kind="invalid-params",
                      description="the registry rejects non-object params before legacy admission",
                      params=[],
                      error=MethodExampleError(
                          ipc_errors.INVALID_ARGUMENT,
                          "targets params must be an object",
                      )),
    ),
    domain_errors_not_applicable="a valid directory query has no domain error",
)
