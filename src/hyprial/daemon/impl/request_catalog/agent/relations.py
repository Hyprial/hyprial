"""GUI v1 Agent detail tabs: four migrated read methods and their real handlers.

Decision record (implRel, 2026-11-02)
- Scope is exactly ``routine.list``, ``routine.status``, ``workflow.list`` and
  ``agent.home-census`` — the read set of the Agent detail page tabs
  (概览 | 凭据与授权 | 运行(routine、home、cell) | 关联(组织、PAC、adapter、路由、link)).
  Every other ``routine.*``/``workflow.*`` name keeps the legacy router branch;
  ``agent`` stays a partial family and gains ``agent.home-census`` in
  ``PARTIAL_MIGRATED_METHODS``.  No write path is declared; cells are a later
  phase; adapter pins/routes and org membership belong to their owning lines
  and are not re-declared here.
- Each handler delegates to the single legacy implementation
  (``app._ipc_routine_list``, ``app._ipc_routine_status``,
  ``app._ipc_workflow_status_group``, ``app._handle_agent``) so visibility,
  enrichment and result shapes stay one-sourced.  The registry binds the
  exact names, so dispatch no longer reaches the legacy ``routine.`` /
  ``workflow.list`` / ``agent.*`` router branches for these four; the legacy
  branches remain for every sibling and are still exercised by the CLI tests.
- The GUI is the local operator over HTTP and cannot present the legacy
  ``actor``/``sessionRef`` caller binding (the HTTP selector projects a
  canonical agent URI, never ``user:<owner>``).  Each method therefore gains
  ONE optional business selector.  The routine and workflow methods name it
  ``agent`` because their ``actor`` param is the legacy caller identity and
  must keep that meaning byte-for-byte (the brief's ``actor`` suggestion is
  the selector name only where no caller identity exists).  A selector call
  answers that agent's rows under the same trust posture as the accepted
  ``agent.secret.list`` actor selector; it is resolved against this node's
  registry and a foreign-node URI or unknown name is AGENT_NOT_FOUND.
- Filters run in the daemon: ``workflow.list`` passes the selector as the
  service ``viewer`` (created_by OR node owner) so the limit applies after
  the filter; ``routine.list``/``routine.status`` match rows in the handler
  after the legacy enrichment; ``agent.home-census`` filters rows and
  recomputes ``totals`` from the surviving rows so the aggregate always
  describes exactly the returned rows.
- Params schemas declare the legacy admission: unknown keys stay tolerated,
  ``limit`` stays any integer (the service clamps to 1..500), and ``all``
  keeps its legacy boolean check.  Without the selector, caller behaviour is
  byte-identical to the pre-migration router, including the
  ``routine.status`` ownership gate and the ``PAC_PRINCIPAL_UNVERIFIED``
  wrap of every ``_workflow_caller`` refusal.
- Reachable errors are enumerated mechanically in
  ``tests/daemon/ipc/contracts/agent/test_relations_specs.py``; codes the
  scanner finds but that cannot reach the boundary are excluded there with
  the reason.  Raw storage failures surface as the registry's DAEMON_ERROR
  replacement, which each spec declares.

Rel2 corrections (implRel2, 2026-11-02 — revRel REJECT ddca55d782)
- F1: revRel proved the round-1 "byte-identical" claim false for the legacy
  caller fields: the params schema constraints on ``actor``/``sessionRef``
  pre-empted ``_workflow_caller``'s PAC_PRINCIPAL_UNVERIFIED wrap, and the
  ``routine.status`` ``name`` minLength pre-empted the degraded ordering
  (service-unavailable before name validation).  These properties are now
  documentation-only; the original handlers keep admitting them, pinned by
  legacy-vs-registry equivalence tests on the same application.
- F2: ``_resolve_agent_selector`` now translates every ``AgentError`` from
  the registry reads (``normalize_actor``/``get``) into a DaemonRequestError
  carrying the error's own code, so a corrupt registry row can no longer
  escape the MethodRegistry; AGENT_ERROR is declared on the three
  routine/workflow specs (``agent.home-census`` already declared it), with
  fault-injection tests on all four selector paths.
- F3: the routine ``launch`` row is declared from the real producer
  ``daemon.impl.pac.contracts.workflow.launch``: fixed key set
  {tier, harness, provider, model, cwd, cwdPolicy, args}; tier mode adds
  tier plus the resolved harness/provider/model, explicit mode keeps
  harness; ``harness`` and the always-present ``args`` string array are
  required, the rest follows producer optionality; additionalProperties
  refused.  Verified against real non-empty launch rows (tier default and
  explicit harness forms).
- The ``workflow.list`` closed-run example is a success case (a real
  non-empty list), not an "empty" case.

S3 (implS3, 2026-11-02 — codex-daemon-request REWORK of 394effc0bf, reviewer
647c820f76)
- R1: the selector paths skipped ``_workflow_caller`` entirely, so a
  synthetic ``actor`` with a non-existent ``sessionRef`` plus
  ``agent=<target>`` read the target's rows that the base refuses with
  PAC_PRINCIPAL_UNVERIFIED{cause: CALLER_NOT_AUTHORIZED}.  The fence order is
  admission first, selector second: a claims-carrying request (``actor`` or
  ``sessionRef`` present) must pass ``_workflow_caller`` before any selector
  work and is refused exactly as the base on failure.  A verified
  non-operator caller may only select itself (a foreign target is
  CALLER_NOT_AUTHORIZED, now declared on ``routine.list``/``workflow.list``;
  ``routine.status`` already declared it) — the selector never widens a bound
  caller's view.  The claim-free request keeps the accepted
  ``agent.secret.list`` operator posture (the trusted HTTP local-operator
  admission stays transport-level and distinct from the selector), and the
  ``user:<owner>`` positive control still answers.  The no-selector paths are
  byte-identical to the base.

I2 (UUID addressing merge into 0.5.5.4)
- Selectors resolve through the registry (``agents.get``), never by minting
  ``canonical_agent_uri(owner, node, name)``: a bare name, live display name,
  ``a.`` id, name URI and id URI of one agent all reach its record, whose
  stored URI is its official address and whose ``actor`` is the key the
  census rows carry.  Malformed bare values keep the INVALID_ARGUMENT
  grammar answer; a URI nothing local stores stays AGENT_NOT_FOUND.
- Rows are matched by actor key (``_routine_actor_key``), so a routine bound
  at the name URI minted by ``routine.add`` and one at the coordinator's id
  URI both belong to the agent.  ``workflow.list`` views at the official
  address and the live agent's name URI (the spelling workflow YAML names
  it by; a run naming an earlier same-name agent is listed too, KP-117), merged
  in the service order with the service clamp.
- A bound caller may select itself by any spelling (``_routine_caller_is``
  against the official address), and the no-selector
  ``routine.status`` gate is the router's ``_routine_caller_is`` check
  (router.py routine.* fence), which the registry dispatch otherwise skips.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from hyprial.daemon.impl.ipc.params import _required_string
from hyprial.identity import (
    PAC_PRINCIPAL_UNVERIFIED,
    AgentError,
    InvalidAgentNameError,
    PacError,
)
from hyprial.kernel import (
    DaemonRequestError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    canonical_agent_uri,
    canonical_user_uri,
    ipc_errors,
    parse_agent_uri,
)

JsonObject = dict[str, Any]

_AUDIENCE = ("local-operator", "bound-agent")

_ANY_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}

_SELECTORS = {"agent", "actor"}

_AGENT_SELECTOR = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Business selector: limit the result to this one agent, a bare "
        "registry actor name or this node's canonical "
        "agent:<owner>:<node>:<actor> URI. When present it defines the row "
        "set after the legacy caller admission; a bound caller may only "
        "select itself."
    ),
}

_CENSUS_SELECTOR = {
    "type": ["string", "null"],
    "minLength": 1,
    "description": (
        "Business selector: answer the census of this one agent, a bare "
        "registry actor name or this node's canonical agent URI. Totals are "
        "recomputed from the returned rows; omitted or null answers the "
        "whole-node census."
    ),
}

_LEGACY_CALLER_FIELDS = {
    "actor": {
        "type": _ANY_JSON["type"],
        "description": (
            "Legacy caller identity; admitted by the original handler (a "
            "malformed claim is wrapped into PAC_PRINCIPAL_UNVERIFIED), not "
            "by this schema."
        ),
    },
    "sessionRef": {
        "type": _ANY_JSON["type"],
        "description": (
            "Legacy caller session reference; admitted by the original "
            "handler, not by this schema."
        ),
    },
}

_ROUTINE_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "all": {
            "type": "boolean",
            "description": (
                "Legacy caller-scoping switch; a non-boolean is refused. "
                "Ignored when agent is present."
            ),
        },
        **_LEGACY_CALLER_FIELDS,
        "agent": _AGENT_SELECTOR,
    },
    "additionalProperties": _ANY_JSON,
}

_ROUTINE_STATUS_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            "type": _ANY_JSON["type"],
            "description": (
                "The routine to answer. Requiredness and shape stay with the "
                "handler: a degraded routine service answers "
                "ROUTINE_UNAVAILABLE before the name is read, the legacy "
                "admission order."
            ),
        },
        **_LEGACY_CALLER_FIELDS,
        "agent": _AGENT_SELECTOR,
    },
    "additionalProperties": _ANY_JSON,
}

_WORKFLOW_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "description": (
                "Maximum rows; any integer is admitted and the service "
                "clamps to 1..500 (default 50)."
            ),
        },
        "all": {
            "type": "boolean",
            "description": (
                "Legacy caller-scoping switch; a non-boolean is refused. "
                "Ignored when agent is present."
            ),
        },
        **_LEGACY_CALLER_FIELDS,
        "agent": _AGENT_SELECTOR,
    },
    "additionalProperties": _ANY_JSON,
}

_CENSUS_PARAMS = {
    "type": "object",
    "properties": {
        "actor": _CENSUS_SELECTOR,
    },
    "additionalProperties": _ANY_JSON,
}

_IN_FLIGHT_ROW = {
    "type": "object",
    "properties": {
        "taskUuid": {"type": "string"},
        "runId": {"type": "string"},
        "target": {"type": "string"},
    },
    "required": ["taskUuid", "runId", "target"],
    "additionalProperties": False,
}

_LAUNCH = {
    "type": ["object", "null"],
    "description": (
        "The producer daemon.impl.pac.contracts.workflow.launch emits a fixed "
        "key set: tier mode adds tier plus the resolved harness/provider/model "
        "(the resolved provider may be null), explicit mode keeps harness "
        "(provider/model optional); cwd/cwdPolicy only when declared; args is "
        "always a string array."
    ),
    "properties": {
        "tier": {"enum": ["fast", "strong", "super"]},
        "harness": {"type": "string", "minLength": 1},
        "provider": {"type": ["string", "null"], "minLength": 1},
        "model": {"type": "string", "minLength": 1},
        "cwd": {"type": "string", "minLength": 1},
        "cwdPolicy": {"enum": ["require", "create"]},
        "args": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["harness", "args"],
    "additionalProperties": False,
}

_ROUTINE_ROW_PROPERTIES: dict[str, Any] = {
    "name": {"type": "string", "minLength": 1},
    "owner": {"type": "string", "minLength": 1},
    "enabled": {"type": "boolean"},
    "nextDueMs": {"type": "integer"},
    "sourceErrorStreak": {"type": "integer", "minimum": 0},
    "outcomes": {"type": "array", "items": {"type": "string"}},
    "registrationId": {"type": ["string", "null"]},
    "mode": {"type": "string", "minLength": 1},
    "role": {"type": "string", "minLength": 1},
    "actor": {"type": "string", "minLength": 1},
    "actorOwnership": {"type": "string", "minLength": 1},
    "launch": _LAUNCH,
    "overlap": {"type": ["string", "null"]},
    "missedPeriods": {"type": ["string", "null"]},
    "inFlight": {"type": "array", "items": _IN_FLIGHT_ROW},
    "lastDispatchAtMs": {"type": ["integer", "null"]},
    "lastOutcome": {"type": ["string", "null"]},
    "produces": {"type": "string", "minLength": 1},
    "schemaError": {"type": "string"},
    "readable": {"const": False},
    "quarantined": {"const": True},
    "quarantineReason": {"type": "string"},
}

_ROUTINE_ROW = {
    "type": "object",
    "properties": _ROUTINE_ROW_PROPERTIES,
    "required": [
        "name",
        "owner",
        "enabled",
        "nextDueMs",
        "sourceErrorStreak",
        "outcomes",
        "registrationId",
        "mode",
        "role",
        "actor",
        "actorOwnership",
        "launch",
        "overlap",
        "missedPeriods",
        "inFlight",
        "lastDispatchAtMs",
        "lastOutcome",
    ],
    "additionalProperties": False,
}

_SCHEDULE_EVENT = {
    "type": "object",
    "properties": {
        "routine": {"type": "string", "minLength": 1},
        "slot_ms": {"type": "integer"},
        "disposition": {"type": "string", "minLength": 1},
        "count": {"type": "integer", "minimum": 0},
        "reason": {"type": "string"},
        "recorded_at_ms": {"type": "integer"},
    },
    "required": [
        "routine",
        "slot_ms",
        "disposition",
        "count",
        "reason",
        "recorded_at_ms",
    ],
    "additionalProperties": False,
}

_CURRENT_NODE = {
    "type": ["object", "null"],
    "properties": {
        "nodeId": {"type": "string", "minLength": 1},
        "state": {"type": "string", "minLength": 1},
    },
    "required": ["nodeId", "state"],
    "additionalProperties": False,
}

_RUN_ROW = {
    "type": "object",
    "properties": {
        "runId": {"type": "string", "minLength": 1},
        "graphId": {"type": "string", "minLength": 1},
        "backend": {"const": "pac"},
        "name": {"type": "string", "minLength": 1},
        "state": {"type": "string", "minLength": 1},
        "sender": {"type": "string", "minLength": 1},
        "createdAtMs": {"type": "integer"},
        "closedAtMs": {"type": ["integer", "null"]},
        "onFailure": {"type": "string"},
        "routineName": {"type": ["string", "null"]},
        "parentGraphId": {"type": "string", "minLength": 1},
        "parentNodeId": {"type": "string", "minLength": 1},
        "lastProgressAtMs": {"type": ["integer", "null"]},
        "currentNode": _CURRENT_NODE,
    },
    "required": [
        "runId",
        "graphId",
        "backend",
        "name",
        "state",
        "sender",
        "createdAtMs",
        "closedAtMs",
        "onFailure",
        "routineName",
        "lastProgressAtMs",
        "currentNode",
    ],
    "additionalProperties": False,
}

_CENSUS_ROW = {
    "type": "object",
    "properties": {
        "actor": {"type": "string", "minLength": 1},
        "uri": {"type": "string", "minLength": 1},
        "preferredHarness": {"type": ["string", "null"]},
        "lastHarness": {"type": ["string", "null"]},
        "harness": {"type": ["string", "null"]},
        "homeProvisioned": {"type": "boolean"},
        "explicitConfig": {"type": "boolean"},
        "configInsideHome": {"type": "boolean"},
        "cwdInsideHome": {"type": "boolean"},
        "residentOrDsh": {"type": "boolean"},
        "transferReceived": {"type": "boolean"},
        "mode": {"enum": ["p2", "legacy"]},
        "legacyReason": {"type": ["string", "null"]},
    },
    "required": [
        "actor",
        "uri",
        "preferredHarness",
        "lastHarness",
        "harness",
        "homeProvisioned",
        "explicitConfig",
        "configInsideHome",
        "cwdInsideHome",
        "residentOrDsh",
        "transferReceived",
        "mode",
        "legacyReason",
    ],
    "additionalProperties": False,
}

_MODE_HARNESS_COUNT = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "minLength": 1},
        "harness": {"type": "string", "minLength": 1},
        "count": {"type": "integer", "minimum": 1},
    },
    "required": ["mode", "harness", "count"],
    "additionalProperties": False,
}

_CENSUS_TOTALS = {
    "type": "object",
    "properties": {
        "agents": {"type": "integer", "minimum": 0},
        "byModeHarness": {"type": "array", "items": _MODE_HARNESS_COUNT},
        "homeUnprovisioned": {"type": "integer", "minimum": 0},
    },
    "required": ["agents", "byModeHarness", "homeUnprovisioned"],
    "additionalProperties": False,
}

_ROUTINE_LIST_RESULT = {
    "type": "object",
    "properties": {
        "routines": {"type": "array", "items": _ROUTINE_ROW},
    },
    "required": ["routines"],
    "additionalProperties": False,
}

_ROUTINE_STATUS_PROPERTIES: dict[str, Any] = {
    key: rule
    for key, rule in _ROUTINE_ROW_PROPERTIES.items()
    if key not in {"lastDispatchAtMs", "lastOutcome"}
}

_ROUTINE_STATUS_RESULT = {
    "type": "object",
    "properties": {
        **_ROUTINE_STATUS_PROPERTIES,
        "scheduleEvents": {"type": "array", "items": _SCHEDULE_EVENT},
    },
    "required": [
        "name",
        "owner",
        "enabled",
        "nextDueMs",
        "sourceErrorStreak",
        "outcomes",
        "registrationId",
        "mode",
        "role",
        "actor",
        "actorOwnership",
        "launch",
        "overlap",
        "missedPeriods",
        "inFlight",
        "scheduleEvents",
    ],
    "additionalProperties": False,
}

_WORKFLOW_LIST_RESULT = {
    "type": "object",
    "properties": {
        "runs": {"type": "array", "items": _RUN_ROW},
    },
    "required": ["runs"],
    "additionalProperties": False,
}

_CENSUS_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "readAtMs": {"type": "integer"},
        "registry": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "version": {"type": "integer"},
            },
            "required": ["path", "version"],
            "additionalProperties": False,
        },
        "agents": {"type": "array", "items": _CENSUS_ROW},
        "totals": _CENSUS_TOTALS,
    },
    "required": ["ok", "readAtMs", "registry", "agents", "totals"],
    "additionalProperties": False,
}


def _error(code: str, description: str) -> MethodError:
    return MethodError(code, description=description)


_ROUTINE_UNAVAILABLE = _error(
    ipc_errors.ROUTINE_UNAVAILABLE, "the routine service is not running; no data payload."
)

_NOT_FOUND = _error(
    "ROUTINE_NOT_FOUND",
    "no such routine, or the agent selector does not cover it; no data payload.",
)

_PRINCIPAL_DATA = {
    "type": "object",
    "properties": {"cause": {"type": "string"}},
    "required": ["cause"],
    "additionalProperties": False,
}

_PRINCIPAL = MethodError(
    PAC_PRINCIPAL_UNVERIFIED,
    _PRINCIPAL_DATA,
    "the actor claim has no matching live daemon session; data carries the underlying cause code.",
)

_REGISTRY_ERROR = _error(
    AgentError.code,
    "the local agent registry could not be read while resolving the agent "
    "selector; no data payload.",
)

_CALLER = _error(
    ipc_errors.CALLER_NOT_AUTHORIZED,
    "a bound caller selected an agent other than itself; the selector never "
    "widens a caller's view; no data payload.",
)

_ROUTINE_LIST_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        "all must be a boolean, or the agent selector is not a bare agent "
        "name or canonical local URI; no data payload.",
    ),
    _ROUTINE_UNAVAILABLE,
    _PRINCIPAL,
    _CALLER,
    _error(
        ipc_errors.AGENT_NOT_FOUND,
        "the agent selector names no agent registered on this machine; no data payload.",
    ),
    _REGISTRY_ERROR,
    _error(ipc_errors.DAEMON_ERROR, "a routine or workflow storage read failed."),
)

_ROUTINE_STATUS_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        "name must be a non-empty string, or the agent selector is not a "
        "bare agent name or canonical local URI; no data payload.",
    ),
    _ROUTINE_UNAVAILABLE,
    _NOT_FOUND,
    _error(
        ipc_errors.CALLER_NOT_AUTHORIZED,
        "the caller does not own this routine; no data payload.",
    ),
    _PRINCIPAL,
    _error(
        ipc_errors.AGENT_NOT_FOUND,
        "the agent selector names no agent registered on this machine; no data payload.",
    ),
    _REGISTRY_ERROR,
    _error(ipc_errors.DAEMON_ERROR, "a routine storage read failed."),
)

_WORKFLOW_LIST_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        "all must be a boolean, or the agent selector is not a bare agent "
        "name or canonical local URI; no data payload.",
    ),
    _error(
        ipc_errors.WORKFLOW_UNAVAILABLE,
        "the workflow service is not running or its store cannot be opened; no data payload.",
    ),
    _PRINCIPAL,
    _CALLER,
    _error(
        ipc_errors.AGENT_NOT_FOUND,
        "the agent selector names no agent registered on this machine; no data payload.",
    ),
    _REGISTRY_ERROR,
    _error(ipc_errors.DAEMON_ERROR, "a workflow storage read failed."),
)

_CENSUS_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        "the actor selector is not a bare agent name or canonical local URI; no data payload.",
    ),
    _error(
        ipc_errors.AGENT_NOT_FOUND,
        "the actor selector names no agent registered on this machine; no data payload.",
    ),
    _error(
        AgentError.code,
        "the agent registry or a home receipt could not be read, in the "
        "census read or the actor selector resolution; no data payload.",
    ),
    _error(ipc_errors.DAEMON_ERROR, "a registry storage read failed."),
)


ROUTINE_LIST_SPEC = MethodSpec(
    name="routine.list",
    owner="daemon.routine",
    params_schema=_ROUTINE_LIST_PARAMS,
    result_schema=_ROUTINE_LIST_RESULT,
    errors=_ROUTINE_LIST_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=_AUDIENCE,
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

ROUTINE_STATUS_SPEC = MethodSpec(
    name="routine.status",
    owner="daemon.routine",
    params_schema=_ROUTINE_STATUS_PARAMS,
    result_schema=_ROUTINE_STATUS_RESULT,
    errors=_ROUTINE_STATUS_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=_AUDIENCE,
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

WORKFLOW_LIST_SPEC = MethodSpec(
    name="workflow.list",
    owner="daemon.workflow",
    params_schema=_WORKFLOW_LIST_PARAMS,
    result_schema=_WORKFLOW_LIST_RESULT,
    errors=_WORKFLOW_LIST_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=_AUDIENCE,
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

AGENT_HOME_CENSUS_SPEC = MethodSpec(
    name="agent.home-census",
    owner="daemon.agent",
    params_schema=_CENSUS_PARAMS,
    result_schema=_CENSUS_RESULT,
    errors=_CENSUS_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=_AUDIENCE,
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)


def _resolve_agent_selector(app: Any, value: Any) -> tuple[str, str]:
    """Official address and actor key for one agent selector value.

    Every spelling the registry answers (name, live display name, ``a.`` id,
    name URI, id URI) reaches the one record; its stored URI is the official
    address.  A non-URI value must still match the registry name grammar.
    The named agent must be registered here, so a typo cannot silently
    answer an empty list.
    """

    if parse_agent_uri(value) is None:
        try:
            app.agents.normalize_actor(value)
        except InvalidAgentNameError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"agent selector must be a bare agent name or a canonical local "
                f"agent URI: {error}",
            ) from error
        except AgentError as error:
            raise _registry_failure(error) from error
    record = _registry_get(app, value)
    if record is None:
        raise DaemonRequestError(
            ipc_errors.AGENT_NOT_FOUND,
            "no agent with that name is registered on this machine",
        )
    return record.uri, record.actor


def _names_actor(app: Any, value: Any, actor: str) -> bool:
    """Whether a stored routine binding names the local agent ``actor``."""

    return isinstance(value, str) and app._routine_actor_key(value) == ("local", actor)


def _registry_get(app: Any, name: str) -> Any:
    try:
        return app.agents.get(name)
    except AgentError as error:
        raise _registry_failure(error) from error


def _registry_failure(error: AgentError) -> DaemonRequestError:
    return DaemonRequestError(error.code, str(error))


def _routine_port_error(error: Any) -> DaemonRequestError:
    return DaemonRequestError(
        error.code, str(error), dict(error.data) if error.data else None
    )


def _selector_claims_caller(app: Any, params: Any, selector: Any) -> str | None:
    """Admission before any selector work; the verified caller, or None.

    A claims-carrying request must pass the legacy caller fence exactly as
    the base does (the PAC_PRINCIPAL_UNVERIFIED wrap included); a claim-free
    request is the trusted local-operator posture and needs no caller
    identity.  Returns None when the no-selector legacy path does its own
    admission.
    """

    if selector is None or ("actor" not in params and "sessionRef" not in params):
        return None
    return app._workflow_caller(params)


def _refuse_foreign_selector(app: Any, caller: str | None, uri: str) -> None:
    """A verified non-operator caller may only select itself."""

    if caller is None or caller == canonical_user_uri(app.owner):
        return
    if not app._routine_caller_is(caller, uri):
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "a bound caller may only select itself",
        )


def routine_list(
    app: Any,
    params: Any,
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon import RoutinePortError

    selector = params.get("agent")
    caller = _selector_claims_caller(app, params, selector)
    try:
        if selector is None:
            return app._ipc_routine_list(params, app._workflow_caller(params))
        uri, actor = _resolve_agent_selector(app, selector)
        _refuse_foreign_selector(app, caller, uri)
        rows = app._ipc_routine_list(
            {"all": True}, canonical_user_uri(app.owner)
        )["routines"]
        return {
            "routines": [
                row
                for row in rows
                if _names_actor(app, row.get("owner"), actor)
                or _names_actor(app, row.get("actor"), actor)
            ]
        }
    except RoutinePortError as error:
        raise _routine_port_error(error) from error


def routine_status(
    app: Any,
    params: Any,
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon import RoutinePortError

    selector = params.get("agent")
    caller = _selector_claims_caller(app, params, selector)
    try:
        if selector is None:
            caller = app._workflow_caller(params)
            if app._routine_service is not None:
                current = app._routine_service.status(
                    name=_required_string(params.get("name"), "name")
                )
                if caller != canonical_user_uri(
                    app.owner
                ) and not app._routine_caller_is(
                    caller, current["owner"], current.get("actor")
                ):
                    raise DaemonRequestError(
                        ipc_errors.CALLER_NOT_AUTHORIZED,
                        "caller does not own this routine",
                    )
            return app._ipc_routine_status(params)
        uri, actor = _resolve_agent_selector(app, selector)
        _refuse_foreign_selector(app, caller, uri)
        current = app._ipc_routine_status({"name": params.get("name")})
        if not (
            _names_actor(app, current.get("owner"), actor)
            or _names_actor(app, current.get("actor"), actor)
        ):
            raise DaemonRequestError(
                "ROUTINE_NOT_FOUND",
                f"no such routine: {current.get('name')}",
            )
        return current
    except RoutinePortError as error:
        raise _routine_port_error(error) from error


def workflow_list(
    app: Any,
    params: Any,
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.pac.workflows.runtime import WorkflowServiceError

    selector = params.get("agent")
    caller = _selector_claims_caller(app, params, selector)
    try:
        if selector is None:
            return app._ipc_workflow_status_group("workflow.list", dict(params))
        uri, actor = _resolve_agent_selector(app, selector)
        _refuse_foreign_selector(app, caller, uri)
        if app._workflow_service is None:
            raise DaemonRequestError(
                ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is not running"
            )
        located = parse_agent_uri(uri)
        named = None if located is None else canonical_agent_uri(located[0], located[1], actor)
        return _workflow_runs_viewed_by(app, (uri, named), params.get("limit", 50))
    except (WorkflowServiceError, PacError) as error:
        raise DaemonRequestError(error.code, str(error)) from error


def _workflow_runs_viewed_by(
    app: Any, viewers: tuple[str | None, ...], limit: int
) -> JsonObject:
    """``workflow.list`` for every spelling one agent is stored under.

    One viewer is the plain service call.  Otherwise each spelling is
    listed and the runs are merged in the service order (newest first, then
    graph id) under the service's own 1..500 clamp.
    """

    spellings = tuple(dict.fromkeys(viewer for viewer in viewers if viewer))
    if len(spellings) == 1:
        return app._workflow_service.list(limit=limit, viewer=spellings[0])
    runs: dict[str, Any] = {}
    for viewer in spellings:
        for run in app._workflow_service.list(limit=limit, viewer=viewer)["runs"]:
            runs.setdefault(run["runId"], run)
    ordered = sorted(runs.values(), key=lambda run: (-run["createdAtMs"], run["graphId"]))
    return {"runs": ordered[: min(max(limit, 1), 500)]}


def agent_home_census(
    app: Any,
    params: Any,
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from . import _legacy_agent_call

    selector = params.get("actor")
    result = _legacy_agent_call(app, "agent.home-census", dict(params))
    if selector is None:
        return result
    _uri, actor = _resolve_agent_selector(app, selector)
    rows = [row for row in result["agents"] if row["actor"] == actor]
    counts: Counter[tuple[str, str]] = Counter(
        (row["mode"], row["harness"] or "unknown") for row in rows
    )
    totals = {
        "agents": len(rows),
        "byModeHarness": [
            {"mode": mode, "harness": harness, "count": count}
            for (mode, harness), count in sorted(counts.items())
        ],
        "homeUnprovisioned": sum(
            1 for row in rows if row["homeProvisioned"] is False
        ),
    }
    return {**result, "agents": rows, "totals": totals}


def _routine_list_param_error(error: Any, params: Any) -> str:
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    field = path[0] if path else ""
    if field == "all":
        return "all must be a boolean"
    if field == "agent":
        return (
            "agent selector must be a bare agent name or a canonical local agent URI"
        )
    return _generic_param_error(ROUTINE_LIST_SPEC.name, error)


def _routine_status_param_error(error: Any, params: Any) -> str:
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    field = path[0] if path else ""
    if field == "agent":
        return (
            "agent selector must be a bare agent name or a canonical local agent URI"
        )
    return _generic_param_error(ROUTINE_STATUS_SPEC.name, error)


def _workflow_list_param_error(error: Any, params: Any) -> str:
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    field = path[0] if path else ""
    if field == "all":
        return "all must be a boolean"
    if field == "limit":
        return "limit must be an integer"
    if field == "agent":
        return (
            "agent selector must be a bare agent name or a canonical local agent URI"
        )
    return _generic_param_error(WORKFLOW_LIST_SPEC.name, error)


def _census_param_error(error: Any, params: Any) -> str:
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    if path[:1] == ["actor"]:
        return "actor selector must be a bare agent name or a canonical local agent URI"
    return _generic_param_error(AGENT_HOME_CENSUS_SPEC.name, error)


def _generic_param_error(method: str, error: Any) -> str:
    path = "/".join(str(part) for part in getattr(error, "absolute_path", ()))
    return f"{method} params do not match the declared schema at {path or '<root>'}"


RELATIONS_BINDINGS = (
    (ROUTINE_LIST_SPEC, routine_list, _routine_list_param_error),
    (ROUTINE_STATUS_SPEC, routine_status, _routine_status_param_error),
    (WORKFLOW_LIST_SPEC, workflow_list, _workflow_list_param_error),
    (AGENT_HOME_CENSUS_SPEC, agent_home_census, _census_param_error),
)

__all__ = [
    "AGENT_HOME_CENSUS_SPEC",
    "RELATIONS_BINDINGS",
    "ROUTINE_LIST_SPEC",
    "ROUTINE_STATUS_SPEC",
    "WORKFLOW_LIST_SPEC",
    "agent_home_census",
    "routine_list",
    "routine_status",
    "workflow_list",
]
