"""``agent.create`` / ``agent.stop`` / ``agent.start`` declarations and handlers.

Decision record (GUI v1 agents line, implBc, 2026-11-02)
- The three names join the exact-name partial ``agent`` migration alongside the
  M-01 reads and the M-02 provider slice; the ``agent`` family stays open, so
  every other legacy ``agent.*`` name keeps its router branch.  This module
  exports one binding tuple that ``request_catalog.registry`` combines; it does
  not touch ``PARTIAL_MIGRATED_METHODS`` (the M-01 read pair) to keep the
  package ``__init__`` free of a circular import.
- ``agent.create`` declares the existing IPC surface exactly as the handler
  accepts it, including the ``agent.provider.default`` merge wrapper: the
  registry handler calls ``app._handle_agent_create_with_default`` so
  ``providerDefault`` is part of every successful create result.  Legacy value
  domains (``existing``, ``harness``, ``runtime``, ``config``,
  ``includeResumeSnapshot``, unknown keys) stay permissive per the M-01/M-02
  compatibility rule: the handler owns coercion, requiredness and order.
- ``agent.stop`` is a new wire name that wraps the existing ``down`` handler
  for exactly one agent: ``{"target": <name>, "all": false, "agentsOnly":
  true}``.  The ``--all`` adapter-and-everything behaviour of the CLI stays a
  CLI concern and is deliberately not reachable through this method.
- ``agent.start`` is declared background-start-only (no terminal attach; the
  terminal methods in cli-inventory stay out of scope).  Until the daemon-side
  orchestration lands (implB2), the registered handler refuses every call with
  the typed ``DAEMON_NOT_READY`` error: the catalog lists and HTTP admits the
  route, but every call returns not-ready, never success.  A catalog-level
  ``not-ready`` state was rejected on purpose: the H layer turns any
  not-ready route into a gateway-wide 503, which would take the whole HTTP
  entry down until implB2 and break the parallel GUI lines.  The result schema
  (``accepted`` vs ``running``) is the contract implB2 implements against;
  ``running`` may only be true with positive runtime evidence.
- All three share the write profile of the provider slice: ``mutates=True``,
  ``retry="never"``, ``confirmation="none"``, audience including
  ``local-operator``, ``http_exposed=True`` with the no-selector
  ``IdentityBinding("none")`` binding, so HTTP admission (H-03) treats them as
  never-retry writes and replays can never be made safe.
- ``DAEMON_NOT_READY`` is the registry's not-ready code; no new error code is
  minted for the not-ready state.

Decision record (implB2, 2026-11-02; codex-daemon-request REWORK of PR #2270)
- ``agent.start`` is registered again with its real background-start handler:
  the CLI's fresh-headless orchestration (create-on-start through the
  registered ``agent.create`` authority, ``_refuse_if_running``, durable
  launch intent from desired state, the ``_ipc_lifecycle_start`` funnel with
  its registered-args merge, custody and readiness observation) moved into
  ``DaemonApplication._ipc_agent_start``; interactive/terminal starts and the
  resume/replace flows stay on ``lifecycle.start`` because the declared
  params carry no sessionRef/replaceInteractive/runtime args.
- The result distinguishes accepted from running: ``running`` is true only
  when the lifecycle funnel's readiness observation confirmed the harness
  actor came up; an accepted start whose readiness window elapsed reports
  ``running: false``.  The optional ``warnings`` array is an implB2 addition
  to the implBc result schema: the launch funnel publishes non-fatal harness
  warnings that the CLI previously surfaced from ``lifecycle.start``, and
  moving the orchestration must not silently drop them.
- Error set (round-4 mechanical enumeration applied to the start walk):
  the lifecycle codes are inherited from the stop set (same manager, ports
  and domain tails; descriptions carried over verbatim with the operation
  named), and every inherited code may carry ``data.lastError`` because
  ``_start_failure_with_harness_error`` attaches the harness's published
  refusal to any error leaving ``_run_lifecycle_operation``.
  ``SUBMIT_OUTCOME_UNKNOWN`` keeps ``operationId`` and may add ``lastError``.
  Direct start codes: ``USE_ADAPTER_COMMAND`` (adapter harness),
  ``TRANSFER_STARTS_HELD``/``TRANSFER_CUSTODY_UNAVAILABLE`` (custody blocks
  the explicit start), ``HARNESS_START_FAILED`` (readiness refusal), and the
  create-inherited set for the create-on-start path (``AGENT_EXISTS``,
  ``AGENT_ALREADY_RUNNING`` with the same binding data, commit/rollback and
  default-routine codes).  The inventory scanner now walks
  ``_ipc_agent_start`` and ``_ipc_lifecycle_start`` so a new code literal on
  the start walk fails the guard until declared.

Decision record (codex-daemon-request REWORK of PR #2270, 2026-11-02; evidence
`git:e9db07d440:docs/notes/gui-agent-review-b64ab67d-2026-11-02/README.md`)
- Once the branch is in a PR, the registered ``agent.start`` refusal is a
  served empty handler, which the rule document forbids; this supersedes the
  dispatcher's transient allowance. ``agent.start`` is unregistered: no
  binding in ``AGENT_LIFECYCLE_BINDINGS``, absent from ``daemon.catalog``, not
  admitted by HTTP; the legacy router falls through to ``_handle_agent``, which
  answers ``METHOD_NOT_FOUND``. ``AGENT_START_SPEC``, its schemas and the
  example set stay in this module, dormant, for implB2 to register together
  with the real background-start handler and the same mechanical error
  enumeration.

Decision record (implBc round 3, 2026-11-02; revBc round 2 REJECT @ 17c39514cc)
- Bc-r2-1: ``agent.stop`` now declares the complete fixed
  ``_ipc_down`` → ``_run_lifecycle_operation`` error set.  The boundary mints
  ``SUBMIT_OUTCOME_UNKNOWN`` (data ``{operationId}`` — the reconciliation id a
  never-retried stop needs when the reservation outcome is unknown) and
  ``DAEMON_START_FAILED`` (a live manager's admission refusal, or a
  non-completed lifecycle result without a code of its own); the manager's
  fixed step codes ``LIFECYCLE_OPERATION_TIMEOUT``, ``PORT_CLOSING``,
  ``PORT_OVERLOADED`` and ``LIFECYCLE_DOMAIN_FAILED`` reach ``result.error_code``
  verbatim and are declared with no data.  The four step codes became kernel
  constants in ``ipc_errors`` because the wire already carried them; only the
  contract was missing.  Every other lifecycle step code is a domain-owned
  event code (harness/agent/route mutation failures) with an open vocabulary;
  those surface as the declared ``DAEMON_ERROR`` exactly as the accepted M-01
  reads handle open ``DomainCommandError`` tails (round-2 Bc-2 policy).
- implB2 obligation: the same complete audit applies to ``agent.start``'s
  error set once its real handler lands; the declaration below is only the
  typed ``DAEMON_NOT_READY`` refusal and must be extended, not reused as-is.

Decision record (implBc round 4, 2026-11-02; revBc round 3 REJECT @ ef2a46a3f6)
- The stop error set is no longer maintained case-by-case.  An AST-based
  enumeration (tests/daemon/ipc/contracts/agent/test_stop_error_inventory.py)
  walks every producer site that can put a code into a terminal lifecycle
  result or raise a typed daemon error on the ``agent.stop`` walk and the
  ``agent.create`` walk.  A guard test fails when a producer module gains a
  code literal that is neither declared nor explicitly excluded with a
  recorded reason, so the next code cannot be missed silently.
- Round 4 findings (each was erasable to ``DAEMON_ERROR`` before):
  ``LIFECYCLE_COMPENSATION_TIMEOUT`` (coordinator compensation deadline),
  ``DOMAIN_COMMAND_TIMEOUT`` (domain correlation wait), the route tail
  (``ROUTE_LEASE_STALE``, ``ROUTE_IO_FAILED``,
  ``ROUTE_CLOSED_BEFORE_COMPLETION``, ``TRANSFER_CUSTODY_UNAVAILABLE``,
  ``AGENT_INCARNATION_MISMATCH``, ``LIFECYCLE_INCARNATION_UNSUPPORTED``), the
  harness tail (``HARNESS_RUNTIME_CLOSING``,
  ``HARNESS_LIFECYCLE_IN_PROGRESS``, ``LIFECYCLE_ATTEMPT_SETTLED``,
  ``HARNESS_LIFECYCLE_RECEIPT_MISMATCH``, ``HARNESS_IO_FAILED``,
  ``STALE_HARNESS_IO_COMPLETION``, ``HARNESS_START_TIMEOUT``) and the
  agent-domain tail (the codes the agent owner's lifecycle/command rejection
  site can emit, plus the actor-retirement undelivered stamps).  Every
  domain-step code carries no data on this boundary: the rejection keeps only
  code and detail, so ``error.data`` is absent by construction.
- ``agent.create`` gets the same enumeration: its agent-domain command tail
  (admission, wait timeout, undelivered stamps, registry rejection codes) is
  declared instead of surfacing as ``DAEMON_ERROR``.  The create walk's
  persona-route failures raise ``RouteCommandError``, which is not a
  ``DaemonRequestError``; that raw-exception tail is shared with the legacy
  CLI path and is recorded in the evidence note, not declared.
- implB2 obligation: the same mechanical enumeration applies to
  ``agent.start`` once its real handler lands.

Decision record (implB3, 2026-11-02; revB2 REJECT @ dd52727e49)
- The ``agent.start`` declaration moved to ``start.py``: the forward launch
  enumeration (B2-1) pushed this module over the 1000-line structural gate,
  and the create/stop declarations stay here.  The full B2-1/B2-3 decision
  records live at the head of ``start.py``; the launch producer enumeration
  lives in ``tests/daemon/ipc/contracts/agent/inventory/``.
- B2-2 belongs to ``DaemonApplication._ipc_agent_start`` (decision record
  there): every start — fresh and existing — routes through the registered
  ``agent.create`` authority (``existing="reuse"``) before the lifecycle
  attempt, restoring the CLI's create-before-start ordering: a failed start
  still leaves the requested provider/model and runtime-capability updates
  applied, and the harness-bound create's own ``_refuse_if_running`` keeps
  the running refusal before any mutation.  The binary-pin, adapter and
  user-proxy preflights stay ahead of the create so a refused start still
  leaves no trace.

Decision record (implBc round 2, 2026-11-02; revBc REJECT @ a598bd911a)
- Bc-1: ``agent.create`` now declares ``AGENT_ALREADY_RUNNING`` with its real
  ``error.data`` schema (``actor``, ``existing``, ``requestedHarness``,
  ``requestedRuntime``).  The harness-bound create path raises it from
  ``_refuse_if_running``; before this fix the registry replaced the code with
  ``DAEMON_ERROR`` and discarded the data.  The audit of the same boundary
  found no other undeclared code: INVALID_ARGUMENT (name/param readers,
  adapter-name collision), AGENT_EXISTS (+data), AGENT_ERROR via the legacy
  translation, HARNESS_START_FAILED, AGENT_DEFAULT_ROUTINE_PENDING and
  AGENT_DEFAULT_ROUTINE_FAILED were already declared; the port-admission
  ``DomainCommandError`` codes surface as the registry's DAEMON_ERROR exactly
  as for the accepted M-01 reads.  ``agent.stop``'s ``_ipc_down`` path raises
  only declared codes (INVALID_ARGUMENT, LIFECYCLE_MANAGER_UNAVAILABLE,
  LIFECYCLE_OPERATION_UNSETTLED); ``agent.start`` raises only the typed
  refusal.
- Bc-2: the create binding now reuses the single legacy ``agent.*`` exception
  translation (the same helper the M-01 reads use; since the S2 merge it
  lives in ``entity.call_with_legacy_agent_errors``), so ``AgentError``/
  ``DomainCommandError``/``AgentHomeError`` surface as their declared codes
  instead of leaking raw domain exceptions.  ``agent.stop`` needs no
  translation (``down`` sits
  outside the router's ``agent.*`` boundary and raises declared codes only);
  ``agent.start`` raises ``DaemonRequestError`` directly.
- Bc-3: the create result now declares real nested shapes from
  ``entity.py``: the status row built by ``agent_status_row(with_activity=
  False)`` for ``agent`` (the producer omits activity hints on this path),
  ``AGENT_IDENTITY_SNAPSHOT`` for ``resumePreviousSnapshot``/
  ``resumeSnapshot`` (the ``Agent.to_json()`` round-trip form), and the three
  real ``providerDefault`` branches (null, captured Claude default object with
  ``provider``/``credentialSource``/``defaultCredential``, historical
  provider-name string).  ``capabilities`` and ``harnessArgs`` are declared in
  the create params with their real shapes; the open ``additionalProperties``
  allowance stays only for genuinely unknown legacy extension keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import AgentAlreadyRunning, AgentError
from hyprial.kernel import (
    HttpBinding,
    IdempotencySpec,
    IdentityBinding,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from .entity import AGENT_IDENTITY_SNAPSHOT, agent_status_row

AGENT_CREATE = "agent.create"
AGENT_STOP = "agent.stop"
AGENT_START = "agent.start"

AGENT_LIFECYCLE_METHODS = frozenset({AGENT_CREATE, AGENT_STOP, AGENT_START})

_STRING = {"type": "string", "minLength": 1}
_COMPAT_VALUE = {
    "description": (
        "Legacy value domain: the existing handler owns the real coercion, "
        "requiredness and validation order; this declaration must not pre-empt it."
    ),
    "type": ["string", "number", "boolean", "null", "array", "object"],
}

_CREATE_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            **_STRING,
            "description": "Agent name, unique on this machine; required first.",
        },
        "existing": {
            **_COMPAT_VALUE,
            "description": (
                'Only the exact string "reuse" hands back an existing agent; '
                "every other value is a fresh-create attempt that refuses a "
                "taken name."
            ),
        },
        "harness": {
            **_COMPAT_VALUE,
            "description": (
                "A non-empty string harness also ensures/spawns that harness "
                "after the identity exists; anything else creates the "
                "identity only."
            ),
        },
        "runtime": {
            **_COMPAT_VALUE,
            "description": (
                "The legacy interactive marker; every other value is the "
                "headless runtime."
            ),
        },
        "cwd": {
            "type": ["string", "null"],
            "description": (
                "Working directory for the agent; null/empty resolves the "
                "agent home workspace."
            ),
        },
        "config": {
            **_COMPAT_VALUE,
            "description": (
                "Personality config directory descriptor used on first "
                "creation; the handler owns its shape."
            ),
        },
        "provider": {
            "type": ["string", "null"],
            "description": "Model vendor preference; empty normalizes to absent.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "Model id preference; empty normalizes to absent.",
        },
        "preferredHarness": {
            "type": ["string", "null"],
            "description": (
                "Default harness for later starts; empty normalizes to absent."
            ),
        },
        "includeResumeSnapshot": {
            **_COMPAT_VALUE,
            "description": "Only exact true attaches the resume snapshot pair.",
        },
        "capabilities": {
            "type": ["object", "null"],
            "additionalProperties": _COMPAT_VALUE,
            "description": (
                "JSON facts stored on the identity; the handler normalizes the "
                "value domain and refuses non-JSON values with AGENT_ERROR."
            ),
        },
        "harnessArgs": {
            "type": ["object", "null"],
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
            },
            "description": (
                "Per-harness launch arguments as {harness: [arg, ...]}; the "
                "handler normalizes the value domain."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": _COMPAT_VALUE,
}

_HANDOVER = {
    "type": "object",
    "properties": {
        "actor": _STRING,
        "previousHarness": _STRING,
        "previousSessionId": {"type": ["string", "null"]},
        "nextHarness": _STRING,
        "contextCarriedOver": {"const": False},
        "notice": {"type": "string", "minLength": 1},
    },
    "required": [
        "actor",
        "previousHarness",
        "previousSessionId",
        "nextHarness",
        "contextCarriedOver",
        "notice",
    ],
    "additionalProperties": False,
}

_AGENT_STATUS_ROW = agent_status_row(with_activity=False)

_PROVIDER_DEFAULT_CREDENTIAL = {
    "type": "object",
    "properties": {
        "ownerActor": {"type": "string", "minLength": 1},
        "ownerEntityToken": {"type": "string", "minLength": 1},
        "entryId": {"type": "string", "minLength": 1},
        "fieldName": {"type": "string", "minLength": 1},
        "kind": {"enum": ["setup-token", "native"]},
    },
    "required": [
        "ownerActor",
        "ownerEntityToken",
        "entryId",
        "fieldName",
        "kind",
    ],
    "additionalProperties": False,
}

_CREATE_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "created": {"type": "boolean"},
        "agent": {
            **_AGENT_STATUS_ROW,
            "description": (
                "The agent status row for the created or reused agent, as "
                "_agent_status_json serializes it without activity hints."
            ),
        },
        "providerDefault": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Historical provider-name default from the legacy "
                        "provider-default.json store."
                    ),
                },
                {
                    "type": "object",
                    "properties": {
                        "provider": {"const": "claude"},
                        "credentialSource": {
                            "enum": ["setup-token", "native"]
                        },
                        "defaultCredential": _PROVIDER_DEFAULT_CREDENTIAL,
                    },
                    "required": ["provider", "credentialSource", "defaultCredential"],
                    "additionalProperties": False,
                    "description": (
                        "The captured Claude default seeded onto the agent; the "
                        "reference names the fenced owner credential."
                    ),
                },
            ],
            "description": (
                "Provider default observed by the captured-default policy: the "
                "captured object, the historical name, or null."
            ),
        },
        "providerDefaultError": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Present only when the provider-default read failed; the "
                "create itself still succeeded."
            ),
        },
        "resumePreviousSnapshot": {
            "oneOf": [
                {"type": "null"},
                {
                    **AGENT_IDENTITY_SNAPSHOT,
                    "description": (
                        "The Agent record as it was before the harness-bound "
                        "create touched it."
                    ),
                },
            ],
        },
        "resumeSnapshot": {
            **AGENT_IDENTITY_SNAPSHOT,
            "description": "The Agent record after the harness-bound create.",
        },
        "harnessHandover": _HANDOVER,
        "routineWarning": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1},
            },
            "required": ["code", "message"],
            "description": "Non-fatal warning from resident-agent finalization.",
        },
    },
    "required": ["ok", "created", "agent", "providerDefault"],
    "additionalProperties": False,
}

_STOP_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            **_STRING,
            "description": (
                "The one agent to stop; matched against the harness specs in "
                "desired state, never against adapters."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_STOP_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "removed": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "The deactivated harness:name rows.",
        },
    },
    "required": ["ok", "removed"],
    "additionalProperties": False,
}

_DOMAIN_COMMAND_TAIL_CODES = (
    ipc_errors.PORT_CLOSING,
    ipc_errors.PORT_OVERLOADED,
    ipc_errors.DOMAIN_COMMAND_TIMEOUT,
    ipc_errors.ACTOR_STOPPED_UNDELIVERED,
    ipc_errors.ACTOR_RESTARTED_UNDELIVERED,
    ipc_errors.ACTOR_QUARANTINED_UNDELIVERED,
    AgentAlreadyRunning.code,
    ipc_errors.AGENT_HOME_ACCESS_FAILED,
    ipc_errors.AGENT_RUNTIME_CONTEXT_STALE,
    ipc_errors.AGENT_RUNTIME_LAUNCH_MISMATCH,
    ipc_errors.AGENT_LAUNCH_IN_PROGRESS,
    ipc_errors.AGENT_MIGRATION_BUSY,
    ipc_errors.SENDER_UNRESOLVED,
    ipc_errors.AGENT_DESTROY_SETTLEMENT_UNKNOWN,
    ipc_errors.AGENT_VERSION_CONFLICT,
    ipc_errors.INVALID_AGENT_NAME,
    ipc_errors.AGENT_EXISTS,
)

_STOP_ROUTE_TAIL_CODES = (
    ipc_errors.ROUTE_LEASE_STALE,
    ipc_errors.ROUTE_IO_FAILED,
    ipc_errors.ROUTE_CLOSED_BEFORE_COMPLETION,
    ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
    ipc_errors.AGENT_INCARNATION_MISMATCH,
    ipc_errors.LIFECYCLE_INCARNATION_UNSUPPORTED,
)

_STOP_HARNESS_TAIL_CODES = (
    ipc_errors.HARNESS_RUNTIME_CLOSING,
    ipc_errors.HARNESS_LIFECYCLE_IN_PROGRESS,
    ipc_errors.LIFECYCLE_ATTEMPT_SETTLED,
    ipc_errors.HARNESS_LIFECYCLE_RECEIPT_MISMATCH,
    ipc_errors.HARNESS_LIFECYCLE_STORE_MISSING,
    ipc_errors.HARNESS_IO_FAILED,
    ipc_errors.STALE_HARNESS_IO_COMPLETION,
    ipc_errors.HARNESS_START_TIMEOUT,
    ipc_errors.HARNESS_START_FAILED,
    ipc_errors.HARNESS_COMPLETION_HANDOFF_FAILED,
)


_CREATE_TAIL_EXCLUSIONS = frozenset(
    {
        ipc_errors.AGENT_EXISTS,
        AgentAlreadyRunning.code,
    }
)


def _domain_tail_errors(
    exclude: frozenset[str] = frozenset(),
) -> tuple[MethodError, ...]:
    return tuple(
        MethodError(
            code,
            description=(
                "The agent-domain owner rejected the lifecycle or create "
                f"command with registry code {code}; the write did not take "
                "effect."
            ),
        )
        for code in _DOMAIN_COMMAND_TAIL_CODES
        if code not in exclude
    )



def _error(code: str) -> MethodError:
    return MethodError(code, description="No data: this error carries no data payload.")


_WRITE_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT),
    _error(ipc_errors.CALLER_NOT_AUTHORIZED),
    _error(ipc_errors.TARGET_NOT_AUTHORIZED),
    _error(ipc_errors.AGENT_NOT_FOUND),
    _error(AgentError.code),
    _error(ipc_errors.DAEMON_ERROR),
)

_CREATE_ERRORS = _WRITE_ERRORS + (
    MethodError(
        ipc_errors.AGENT_EXISTS,
        data_schema={
            "type": "object",
            "properties": {
                "actor": {"type": "string", "minLength": 1},
                "agent": {"type": "string", "minLength": 1},
            },
            "required": ["actor", "agent"],
            "additionalProperties": False,
        },
        description="The name is already taken on this node; data names the existing agent.",
    ),
    MethodError(
        AgentAlreadyRunning.code,
        data_schema={
            "type": "object",
            "properties": {
                "actor": {"type": "string", "minLength": 1},
                "existing": {
                    "type": "object",
                    "properties": {
                        "actor": {"type": "string", "minLength": 1},
                        "harness": {"type": "string", "minLength": 1},
                        "runtime": {"type": "string", "minLength": 1},
                        "sessionId": {"type": ["string", "null"]},
                        "boundAtMs": {"type": "integer"},
                    },
                    "required": [
                        "actor",
                        "harness",
                        "runtime",
                        "sessionId",
                        "boundAtMs",
                    ],
                    "additionalProperties": False,
                },
                "requestedHarness": {"type": "string", "minLength": 1},
                "requestedRuntime": {"type": "string", "minLength": 1},
            },
            "required": [
                "actor",
                "existing",
                "requestedHarness",
                "requestedRuntime",
            ],
            "additionalProperties": False,
        },
        description=(
            "A harness-bound create refuses a served agent; data carries the "
            "live binding and what this create asked for."
        ),
    ),
    _error(ipc_errors.HARNESS_START_FAILED),
    MethodError(
        ipc_errors.AGENT_DEFAULT_ROUTINE_PENDING,
        data_schema={
            "type": "object",
            "properties": {
                "actor": {"type": "string", "minLength": 1},
                "operationId": {"type": "string", "minLength": 1},
            },
            "required": ["actor"],
            "additionalProperties": False,
        },
        description=(
            "The agent creation remains accepted while its default routine "
            "registration is still pending; data names the actor and custody."
        ),
    ),
    MethodError(
        ipc_errors.AGENT_DEFAULT_ROUTINE_FAILED,
        data_schema={
            "type": "object",
            "properties": {
                "actor": {"type": "string", "minLength": 1},
                "routineError": {"type": "string", "minLength": 1},
            },
            "required": ["actor", "routineError"],
            "additionalProperties": False,
        },
        description=(
            "The creation rolled back because the default routine could not "
            "be registered; data names the actor and the routine error code."
        ),
    ),
    *_domain_tail_errors(_CREATE_TAIL_EXCLUSIONS),
)

_STOP_ERRORS = _WRITE_ERRORS + (
    _error(ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE),
    _error(ipc_errors.LIFECYCLE_OPERATION_UNSETTLED),
    MethodError(
        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        data_schema={
            "type": "object",
            "properties": {
                "operationId": {"type": "string", "minLength": 1},
            },
            "required": ["operationId"],
            "additionalProperties": False,
        },
        description=(
            "The lifecycle reservation outcome is unknown: the stop may or may "
            "not have taken effect and is never retried; data names the "
            "operation id to reconcile against lifecycle records."
        ),
    ),
    MethodError(
        ipc_errors.DAEMON_START_FAILED,
        description=(
            "The live lifecycle manager refused the stop's admission, or a "
            "non-completed lifecycle result carried no code of its own; the "
            "stop did not take effect."
        ),
    ),
    MethodError(
        ipc_errors.LIFECYCLE_OPERATION_TIMEOUT,
        description=(
            "The lifecycle manager exceeded its operation deadline and its "
            "settled work was compensated; the stop did not complete."
        ),
    ),
    MethodError(
        ipc_errors.PORT_CLOSING,
        description=(
            "A lifecycle step was dispatched while its domain port was "
            "closing; the stop did not run."
        ),
    ),
    MethodError(
        ipc_errors.PORT_OVERLOADED,
        description=(
            "A lifecycle step was refused because its domain port was "
            "overloaded; the stop did not run."
        ),
    ),
    MethodError(
        ipc_errors.LIFECYCLE_DOMAIN_FAILED,
        description=(
            "A lifecycle step's domain dispatch failed without a code of its "
            "own; the stop did not complete."
        ),
    ),
    _error(ipc_errors.LIFECYCLE_COMPENSATION_TIMEOUT),
    _error(ipc_errors.DOMAIN_COMMAND_TIMEOUT),
    *(
        _error(code)
        for code in _STOP_ROUTE_TAIL_CODES + _STOP_HARNESS_TAIL_CODES
    ),
    *_domain_tail_errors(frozenset({
        ipc_errors.PORT_CLOSING,
        ipc_errors.PORT_OVERLOADED,
        ipc_errors.DOMAIN_COMMAND_TIMEOUT,
    })),
)


AGENT_CREATE_SPEC = MethodSpec(
    name=AGENT_CREATE,
    owner="daemon.agent",
    params_schema=_CREATE_PARAMS,
    result_schema=_CREATE_RESULT,
    errors=_CREATE_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-create"),
)

AGENT_STOP_SPEC = MethodSpec(
    name=AGENT_STOP,
    owner="daemon.agent.lifecycle",
    params_schema=_STOP_PARAMS,
    result_schema=_STOP_RESULT,
    errors=(*_STOP_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload.")),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-stop"),
)


def agent_create(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    from .entity import call_with_legacy_agent_errors

    return call_with_legacy_agent_errors(
        lambda: app._handle_agent_create_with_default(dict(params))
    )


def agent_stop(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    return app._ipc_down(
        {"target": params["name"], "all": False, "agentsOnly": True}
    )


def _path(error: Any) -> list[str]:
    return [str(part) for part in getattr(error, "absolute_path", ())]


def _name_param_error(error: Any) -> str | None:
    validator = getattr(error, "validator", None)
    if "name" in _path(error) or (
        validator == "required"
        and "name" in tuple(getattr(error, "validator_value", ()) or ())
    ):
        if validator in {"required", "type", "minLength"}:
            return "name must be a non-empty string"
    return None


def _create_param_error(error: Any, params: Mapping[str, Any]) -> str:
    field_message = _name_param_error(error)
    if field_message is not None:
        return field_message
    path_text = "/".join(_path(error)) or "<root>"
    return f"{AGENT_CREATE} params do not match the declared schema at {path_text}"


def _stop_param_error(error: Any, params: Mapping[str, Any]) -> str:
    field_message = _name_param_error(error)
    if field_message is not None:
        return field_message
    path_text = "/".join(_path(error)) or "<root>"
    return f"{AGENT_STOP} params do not match the declared schema at {path_text}"


# The late import is the cycle breaker: ``start.py`` imports this module's
# create/stop error tuples at its own top, so it may only load after every
# name it needs exists here.
from .start import AGENT_START_SPEC, _start_param_error, agent_start  # noqa: E402

AGENT_LIFECYCLE_BINDINGS = (
    (AGENT_CREATE_SPEC, agent_create, _create_param_error),
    (AGENT_STOP_SPEC, agent_stop, _stop_param_error),
    (AGENT_START_SPEC, agent_start, _start_param_error),
)

__all__ = [
    "AGENT_CREATE",
    "AGENT_CREATE_SPEC",
    "AGENT_LIFECYCLE_BINDINGS",
    "AGENT_LIFECYCLE_METHODS",
    "AGENT_START",
    "AGENT_START_SPEC",
    "AGENT_STOP",
    "AGENT_STOP_SPEC",
    "agent_create",
    "agent_start",
    "agent_stop",
]
