"""Producer-backed Agent row and snapshot schemas shared by the agent specs.

Decision record (implBc round 2, 2026-11-02)
- revBc round 2 (Bc-3) requires real nested entity schemas on ``agent.create``.
  The create result carries three entity shapes produced elsewhere: the status
  row from ``DaemonApplication._agent_status_json`` (the same producer behind
  ``agent.list`` rows), and the identity snapshots from ``Agent.to_json()`` for
  ``resumePreviousSnapshot``/``resumeSnapshot``.  Both spec modules now consume
  these shapes from this one module instead of declaring parallel copies; the
  M-01 ``agent.list`` row stays byte-equal through ``agent_status_row``.
- The status row producer omits ``activityHints`` whenever it is called without
  activity hints (create never passes them) and omits ``lastActiveAtMs`` in the
  same case, while the restore projection can still emit ``lastActiveAtMs``
  (blocked/dormant rows).  ``with_activity=False`` therefore only drops
  ``activityHints`` from the closed property set and keeps ``lastActiveAtMs``
  optional.
- ``AGENT_IDENTITY_SNAPSHOT`` is the durable ``Agent.to_json()`` record exactly,
  including the ``entityToken`` incarnation fence the status row deliberately
  omits; the resume snapshots are round-trip forms, so the fence is part of
  their contract.
- ``capabilities`` stays an open object and ``config.sharedCredentials`` an
  open string map on purpose: other producers already extend these bags, and
  the legacy handlers own their value domains (M-01 compatibility rule).  All
  other nested objects are closed so a new field becomes a reviewed change.

Decision record (S2 second-submission merge, 2026-11-02)
- ``legacy_boundary.call_with_legacy_agent_errors`` moved here unchanged when
  the relations-methods merge (5cae74f971) pushed the package over the
  seven-direct-file structural gate; the one translation policy is unchanged
  and every binding keeps applying exactly this helper.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, get_args

from hyprial.daemon.impl.cells.observability.model import (
    BaseRunState, ObservationHealth, RunCertainty, RunScope, RunState,
)
from hyprial.identity import HOSTED_BY_VALUES, AgentError, AgentHomeError
from hyprial.kernel import DaemonRequestError, ipc_errors

_STRING = {"type": "string", "minLength": 1}

_CAPABILITIES = {"type": "object"}

_CONFIG_SOURCE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "required": {"type": "boolean"},
    },
    "required": ["path", "required"],
    "additionalProperties": False,
}

_CONFIG = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "sources": {"type": "array", "items": _CONFIG_SOURCE},
                "discovery": {"const": "explicit-only"},
                "sharedCredentials": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["sources", "discovery"],
            "additionalProperties": False,
        },
    ]
}

_HARNESS_ARGS = {
    "type": "object",
    "additionalProperties": {"type": "array", "items": {"type": "string"}},
}

_ACTIVITY_HINTS = {
    "type": "object",
    "properties": {
        "createdAtMs": {"type": "integer"},
        "hasLastSessionId": {"type": "boolean"},
        "agentHomeMtimeMs": {"type": ["integer", "null"]},
    },
    "required": ["createdAtMs", "hasLastSessionId", "agentHomeMtimeMs"],
    "additionalProperties": False,
}

_RUN_COVERAGE = {
    "type": "object",
    "properties": {
        "turnBoundaries": {"type": "boolean"},
        "permissionWait": {"type": "boolean"},
        "producerActivity": {"type": "boolean"},
        "processExit": {"type": "boolean"},
    },
    "required": [
        "turnBoundaries",
        "permissionWait",
        "producerActivity",
        "processExit",
    ],
    "additionalProperties": False,
}

_RUN_THRESHOLDS = {
    "type": "object",
    "properties": {
        "turnLongAfterMs": {"type": ["integer", "null"]},
        "permissionLongAfterMs": {"type": ["integer", "null"]},
        "quietAfterMs": {"type": ["integer", "null"]},
    },
    "required": [
        "turnLongAfterMs",
        "permissionLongAfterMs",
        "quietAfterMs",
    ],
    "additionalProperties": False,
}

_RUN_LAST_KNOWN = {
    "type": "object",
    "properties": {
        "state": {"type": "string"},
        "baseState": {"type": "string"},
        "sinceMs": {"type": ["integer", "null"]},
        "observedAtMs": {"type": "integer"},
        "runtimeIncarnation": {"type": "string"},
    },
    "required": [
        "state",
        "baseState",
        "sinceMs",
        "observedAtMs",
        "runtimeIncarnation",
    ],
    "additionalProperties": False,
}

_RUN_STATE = {
    "type": "object",
    "properties": {
        "state": {"enum": list(get_args(RunState))},
        "baseState": {"enum": list(get_args(BaseRunState))},
        "reason": {"type": "string", "minLength": 1},
        "certainty": {"enum": list(get_args(RunCertainty))},
        "scope": {"enum": list(get_args(RunScope))},
        "observedAtMs": {"type": "integer"},
        "sinceMs": {"type": ["integer", "null"]},
        "turnAgeMs": {"type": ["integer", "null"]},
        "lastActivityAtMs": {"type": ["integer", "null"]},
        "coverage": _RUN_COVERAGE,
        "observationHealth": {"enum": list(get_args(ObservationHealth))},
        "suspicions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "policyRevision": {"type": "integer"},
        "providerBlocked": {"type": "boolean"},
        "thresholds": _RUN_THRESHOLDS,
        "lastKnown": {"oneOf": [{"type": "null"}, _RUN_LAST_KNOWN]},
        "runtimeIncarnation": {"type": "string", "minLength": 1},
        "epoch": {"type": "string", "minLength": 1},
        "revision": {"type": "integer"},
    },
    "required": [
        "state",
        "baseState",
        "reason",
        "certainty",
        "scope",
        "observedAtMs",
        "sinceMs",
        "turnAgeMs",
        "lastActivityAtMs",
        "coverage",
        "observationHealth",
        "suspicions",
        "policyRevision",
        "providerBlocked",
        "thresholds",
        "lastKnown",
        "runtimeIncarnation",
        "epoch",
        "revision",
    ],
    "additionalProperties": False,
}

_SHARED_CREDENTIAL_ROW = {
    "type": "object",
    "properties": {
        "harness": {"type": "string", "minLength": 1},
        "authMode": {"const": "native-shared-link"},
        "nativePath": {"type": "string", "minLength": 1},
        "targetPath": {"type": "string", "minLength": 1},
        "status": {"enum": ["error", "warning", "ready"]},
        "code": {"type": "string", "minLength": 1},
        "error": {"type": "string", "minLength": 1},
        "warning": {"type": "string", "minLength": 1},
    },
    "required": ["harness", "authMode", "targetPath", "status"],
    "additionalProperties": False,
}

_RECOVERY_ERROR = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1},
        "errorType": {"type": "string", "minLength": 1},
        "error": {"type": "string"},
        "source": {"type": "string", "minLength": 1},
        "harness": {"type": ["string", "null"]},
        "runtime": {"type": ["string", "null"]},
        "sessionId": {"type": ["string", "null"]},
    },
    "required": [
        "code",
        "errorType",
        "error",
        "source",
        "harness",
        "runtime",
        "sessionId",
    ],
    "additionalProperties": False,
}

_RECOVERY_CLEANUP = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "minLength": 1},
        "harness": {"type": ["string", "null"]},
        "runtime": {"type": ["string", "null"]},
        "sessionId": {"type": ["string", "null"]},
        "processPid": {"type": ["integer", "null"]},
        "liveness": {"type": ["string", "null"]},
    },
    "required": [
        "source",
        "harness",
        "runtime",
        "sessionId",
        "processPid",
        "liveness",
    ],
    "additionalProperties": False,
}

_HOSTED_BY = {
    "type": ["string", "null"],
    "enum": [None, *HOSTED_BY_VALUES],
}

AGENT_IDENTITY_SNAPSHOT = {
    "type": "object",
    "description": (
        "The durable Agent record exactly as Agent.to_json() serializes it, "
        "including the entityToken incarnation fence."
    ),
    "properties": {
        "uri": _STRING,
        "actor": _STRING,
        "owner": _STRING,
        "machine": _STRING,
        "entityToken": _STRING,
        "cwd": {"type": ["string", "null"]},
        "config": _CONFIG,
        "provider": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "capabilities": _CAPABILITIES,
        "harnessArgs": _HARNESS_ARGS,
        "preferredHarness": {"type": ["string", "null"]},
        "lastHarness": {"type": ["string", "null"]},
        "lastSessionId": {"type": ["string", "null"]},
        "lastActiveAtMs": {"type": ["integer", "null"]},
        "createdAtMs": {"type": "integer"},
        "hosted": {"type": "boolean"},
        "hostedBy": _HOSTED_BY,
        "agentId": {"type": ["string", "null"]},
        "displayName": {"type": ["string", "null"]},
    },
    "required": [
        "uri",
        "actor",
        "owner",
        "machine",
        "entityToken",
        "cwd",
        "config",
        "provider",
        "model",
        "capabilities",
        "harnessArgs",
        "preferredHarness",
        "lastHarness",
        "lastSessionId",
        "lastActiveAtMs",
        "createdAtMs",
        "hosted",
        "hostedBy",
        "agentId",
        "displayName",
    ],
    "additionalProperties": False,
}


def agent_status_row(*, with_activity: bool) -> dict[str, Any]:
    """The ``_agent_status_json`` row shape.

    ``with_activity=True`` is the ``agent.list`` row (activity hints always
    attached, ``lastActiveAtMs`` always present).  ``with_activity=False`` is
    the create-result row: no activity hints, and ``lastActiveAtMs`` only when
    a restore projection emits it.
    """

    properties: dict[str, Any] = {
        "uri": _STRING,
        "actor": _STRING,
        "owner": _STRING,
        "machine": _STRING,
        "cwd": {"type": ["string", "null"]},
        "config": _CONFIG,
        "provider": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "capabilities": _CAPABILITIES,
        "harnessArgs": _HARNESS_ARGS,
        "preferredHarness": {"type": ["string", "null"]},
        "lastHarness": {"type": ["string", "null"]},
        "lastSessionId": {"type": ["string", "null"]},
        "lastActiveAtMs": {"type": ["integer", "null"]},
        "createdAtMs": {"type": "integer"},
        "hosted": {"type": "boolean"},
        "hostedBy": _HOSTED_BY,
        "agentId": {"type": ["string", "null"]},
        "displayName": {"type": ["string", "null"]},
        "activityHints": _ACTIVITY_HINTS,
        "userAgent": {
            "const": True,
            "description": "Present only on the owner's user agent: no harness; lifecycle and credential methods are refused.",
        },
        "userActor": {
            "type": "string",
            "minLength": 1,
            "description": "Present only on a user agent: the user:<owner> every machine's instance shares.",
        },
        "status": {"type": "string", "minLength": 1},
        "harness": {"type": ["string", "null"]},
        "runtime": {"type": ["string", "null"]},
        "sessionId": {"type": ["string", "null"]},
        "heartbeatAgeSeconds": {"type": ["number", "null"]},
        "runState": _RUN_STATE,
        "startupFailure": {
            "type": "object",
            "description": "Latest compensated startup failure for this incarnation and configuration. Cleared by a later lifecycle operation; not process-liveness evidence.",
            "properties": {
                "operationId": _STRING,
                "code": {"const": ipc_errors.HARNESS_START_FAILED},
                "message": _STRING,
                "failedAtMs": {"type": "integer"},
            },
            "required": ["operationId", "code", "message", "failedAtMs"],
            "additionalProperties": False,
        },
        "workerState": {"enum": ["blocked", "dormant"]},
        "restoreStatus": {"type": "string", "minLength": 1},
        "blockReason": {"type": "string", "minLength": 1},
        "blockedAtMs": {"type": "integer"},
        "blockedAgeMs": {"type": "integer"},
        "idleAgeMs": {"type": ["integer", "null"]},
        "restoreThresholdMs": {"type": "integer"},
        "restoreOverride": {"type": "string", "minLength": 1},
        "activityUnknown": {"type": "boolean"},
        "sharedCredentials": {
            "type": "array",
            "items": _SHARED_CREDENTIAL_ROW,
        },
        "recoveryStatus": {"enum": ["failed", "cleaned"]},
        "recoveryError": _RECOVERY_ERROR,
        "recoveryCleanup": _RECOVERY_CLEANUP,
    }
    required = [
        "uri",
        "actor",
        "owner",
        "machine",
        "cwd",
        "config",
        "provider",
        "model",
        "capabilities",
        "harnessArgs",
        "preferredHarness",
        "lastHarness",
        "lastSessionId",
        "lastActiveAtMs",
        "createdAtMs",
        "hosted",
        "hostedBy",
        "activityHints",
        "status",
        "harness",
        "runtime",
        "sessionId",
        "heartbeatAgeSeconds",
    ]
    if not with_activity:
        del properties["activityHints"]
        required = [
            name
            for name in required
            if name not in {"activityHints", "lastActiveAtMs"}
        ]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


@contextmanager
def translated_agent_errors() -> Iterator[None]:
    """The router's agent boundary: typed registry errors keep their IPC code."""

    from hyprial.daemon.impl.composition import DomainCommandError

    try:
        yield
    except (AgentError, DomainCommandError) as error:
        raise DaemonRequestError(
            error.code, str(error), data=getattr(error, "data", None)
        ) from error
    except AgentHomeError as error:
        raise DaemonRequestError(AgentError.code, str(error)) from error


def call_with_legacy_agent_errors(produce: Callable[[], Any]) -> Any:
    with translated_agent_errors():
        return produce()


__all__ = [
    "AGENT_IDENTITY_SNAPSHOT",
    "agent_status_row",
    "call_with_legacy_agent_errors",
    "translated_agent_errors",
]
