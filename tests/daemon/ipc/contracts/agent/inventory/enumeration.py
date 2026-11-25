"""The mechanical error-code enumeration for agent.stop/create/start, shared.

Decision record (implBc round 4 + implB3, 2026-11-02)
- Three rounds of revBc each found a code the previous round missed, so the
  enumeration is no longer done case-by-case.  This module is the mechanism:
  ``scan_candidates`` statically extracts every error-code literal that any
  producer on the ``agent.stop`` / ``agent.create`` / ``agent.start`` walks
  can put into a terminal lifecycle result or raise as a typed daemon error,
  reading the ASTs of the producer modules; nothing is hand-copied from them.
- ``_INVENTORY`` classifies every candidate as reachable (declared on the
  method) or excluded (with the reason it can never reach the boundary).
  The implB3 launch family covers the forward ``harness.ensure`` producers:
  the harness IO port's launch-cwd validation, the credential preflights,
  the pi P2 project-trust resolution, launch-time secret resolution, the
  runtime-launch custody lease, the launch shared-credential check, the
  legacy Claude native-root preparation and the per-launch worker-proxy
  read.
- The guard tests fail when a producer module gains a code literal that is
  neither declared nor excluded with a recorded decision, when a declared
  code drifts from the enumeration, or when a reachable code stops surviving
  the legacy path, the registry and the IPC router.

The scanner rules and the module set are the enumeration of record; the
evidence notes docs/notes/gui-agents-implBc-2026-11-02.md (round 4) and
docs/notes/gui-agents-implB3-2026-11-02.md carry the human-readable tables
derived from them.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import ipc_errors, PortAdmission

REPO_ROOT = Path(__file__).resolve().parents[6]
SRC_ROOT = REPO_ROOT / "src" / "hyprial"

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

_PRODUCER_CALLS = frozenset(
    {
        "PortCommandRejected",
        "RouteMutationFailed",
        "LifecycleMutationFailed",
        "DomainCommandError",
        "TerminalizeHarnessLifecycleCommand",
        "LifecycleStepFailed",
        "LifecycleResult",
        "set_state",
        "_reject",
        "_reject_correlation",
        "_begin_failed_removal",
        "_finish_failed_removal",
        "_finish_failed_ensure",
        "fail_lifecycle",
        "interrupt_running_operations",
        "_notify_undelivered",
        "NativeCredentialError",
        "WorkerProxyError",
    }
)

#: Every producer module the stop/create walks reach for error codes.  The
#: set is the enumeration's scope; the evidence note lists each file's role.
_PRODUCER_MODULES = (
    "daemon/impl/lifecycle/lifecycle_coordinator.py",
    "daemon/impl/lifecycle/atomic_lifecycle_ports.py",
    "daemon/impl/lifecycle_manager/process.py",
    "daemon/impl/lifecycle_manager/store.py",
    "daemon/impl/composition/events.py",
    "daemon/impl/composition/domains.py",
    "daemon/impl/route_registration/io.py",
    "daemon/impl/harnesses/actor/actor.py",
    "daemon/impl/harnesses/actor/behaviors/lifecycle.py",
    "daemon/impl/harnesses/actor/behaviors/desired.py",
    "daemon/impl/harnesses/actor/behaviors/process_events.py",
    "daemon/impl/harnesses/actor/behaviors/delivery/__init__.py",
    "daemon/impl/harnesses/actor/behaviors/delivery/gate.py",
    "daemon/impl/harnesses/actor/io_port.py",
    "daemon/impl/harnesses/claude/runtime.py",
    "daemon/impl/harnesses/pi/loader.py",
    "daemon/impl/harnesses/codex/app_server.py",
    "daemon/impl/harnesses/codex/native_env.py",
    "daemon/impl/application/actors/agents_admin/registry_ops.py",
    "daemon/impl/application/actors/harness_args/__init__.py",
    "identity/impl/agents/runtime/context.py",
    "identity/impl/agents/runtime/secrets.py",
    "identity/impl/agents/home/native_credential.py",
    "identity/impl/agents/state/worker_proxy.py",
    "identity/impl/agents/actor/_core.py",
    "identity/impl/agents/actor/_generation_ops.py",
    "identity/impl/agents/actor/_base.py",
    "identity/impl/agents/registry/_base.py",
    "identity/impl/agents/state/liveness.py",
    "kernel/impl/actor_runtime/guardian.py",
)

#: The typed-raise walk from the registry binding down to the coordinator.
#: ``_ipc_lifecycle_start`` is the shared launch funnel behind both the
#: legacy ``lifecycle.start`` and ``agent.start``; ``_refuse_if_running`` is
#: the running refusal reused by create-on-start.  The S8 custody guards
#: (``refuse_external_launch`` on the create/start walks and in the launch
#: funnel, ``refuse_reserved_capabilities`` on create) are boundary raises
#: too, so a code change there fails the guard until declared.
_BOUNDARY_FUNCTIONS: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "daemon/impl/request_catalog/agent/lifecycle.py",
        "agent_stop",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/request_catalog/agent/lifecycle.py",
        "agent_start",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/application/actors/agent_start/__init__.py",
        "_ipc_agent_start",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/application/actors/lifecycle_ops.py",
        "_ipc_down",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/application/actors/lifecycle_ops.py",
        "_run_lifecycle_operation",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/application/actors/lifecycle_ops.py",
        "_lifecycle_spec",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/application/actors/lifecycle_ops.py",
        "_ipc_lifecycle_start",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/application/actors/agents_admin/destroys.py",
        "_refuse_if_running",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/cells/runtime/agents.py",
        "refuse_external_launch",
        frozenset({"DaemonRequestError"}),
    ),
    (
        "daemon/impl/cells/runtime/agents.py",
        "refuse_reserved_capabilities",
        frozenset({"DaemonRequestError"}),
    ),
)

_PORT_ADMISSION_CODES = frozenset({"PORT_CLOSING", "PORT_OVERLOADED"})

#: Modules where an ``ipc_errors.X`` attribute anywhere is a producer code
#: (the route IO fails commands through locally-bound code variables).
_MODULE_WIDE_IPC_ERRORS = frozenset(
    {"daemon/impl/route_registration/io.py"}
)


def _literal_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _code_literal(node: ast.AST) -> str | None:
    value = _literal_str(node)
    if value is not None and _CODE_RE.match(value):
        return value
    return None


def _resolve_ipc_errors_attr(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ipc_errors"
    ):
        return getattr(ipc_errors, node.attr)
    return None


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scan_module(path: Path, module_wide_ipc_errors: bool = False) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if module_wide_ipc_errors:
            resolved = _resolve_ipc_errors_attr(node)
            if resolved is not None and _CODE_RE.match(resolved):
                found.add(resolved)
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _PRODUCER_CALLS:
                for child in ast.walk(node):
                    literal = _code_literal(child)
                    if literal is not None:
                        found.add(literal)
                    resolved = _resolve_ipc_errors_attr(child)
                    if resolved is not None and _CODE_RE.match(resolved):
                        found.add(resolved)
                if isinstance(node.func, ast.JoinedStr) or any(
                    isinstance(value, ast.JoinedStr)
                    for value in node.keywords
                ):
                    continue
            for keyword in node.keywords:
                if keyword.arg in {"code", "error_code"}:
                    literal = _code_literal(keyword.value)
                    if literal is not None:
                        found.add(literal)
                    resolved = _resolve_ipc_errors_attr(keyword.value)
                    if resolved is not None:
                        found.add(resolved)
                    if (
                        isinstance(keyword.value, ast.JoinedStr)
                        and any(
                            isinstance(part, ast.Constant)
                            and part.value == "PORT_"
                            for part in keyword.value.values
                        )
                    ):
                        found.update(_PORT_ADMISSION_CODES)
        elif isinstance(node, ast.Assign):
            target_name = None
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
            elif (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
            ):
                target_name = node.targets[0].attr
            literal = _code_literal(node.value)
            resolved = _resolve_ipc_errors_attr(node.value)
            if target_name == "code" and (literal or resolved):
                found.add(literal or resolved)
            elif (
                target_name is not None
                and (target_name.isupper() or target_name.endswith("reason"))
                and (literal or resolved)
            ):
                found.add(literal or resolved)
        elif isinstance(node, ast.Compare):
            for comparator in node.comparators:
                literal = _code_literal(comparator)
                if literal is not None:
                    found.add(literal)
        elif isinstance(node, ast.JoinedStr):
            parts = [
                part.value
                for part in node.values
                if isinstance(part, ast.Constant)
            ]
            if "PORT_" in parts:
                found.update(_PORT_ADMISSION_CODES)
    if "PORT_" in found:
        found.discard("PORT_")
        found.update(_PORT_ADMISSION_CODES)
    return found


def _scan_boundary(path: Path, function: str, errors: frozenset[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == function):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Raise) or child.exc is None:
                continue
            exc = child.exc
            if isinstance(exc, ast.Call) and _call_name(exc) in errors:
                if exc.args:
                    found.update(_resolve_code_expr(exc.args[0]))
    return found


def _resolve_code_expr(node: ast.AST) -> set[str]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Or):
        return _resolve_code_expr(node.right)
    if isinstance(node, ast.IfExp):
        return _resolve_code_expr(node.body) | _resolve_code_expr(node.orelse)
    literal = _code_literal(node)
    if literal is not None:
        return {literal}
    resolved = _resolve_ipc_errors_attr(node)
    if resolved is not None:
        return {resolved}
    return set()


def scan_candidates() -> dict[str, set[str]]:
    """Code literal -> producer modules, per the rules above."""

    candidates: dict[str, set[str]] = {}
    for relative in _PRODUCER_MODULES:
        for literal in _scan_module(
            SRC_ROOT / relative,
            module_wide_ipc_errors=relative in _MODULE_WIDE_IPC_ERRORS,
        ):
            candidates.setdefault(literal, set()).add(relative)
    for relative, function, errors in _BOUNDARY_FUNCTIONS:
        for literal in _scan_boundary(SRC_ROOT / relative, function, errors):
            candidates.setdefault(literal, set()).add(f"{relative}:{function}")
    return candidates


@dataclass(frozen=True)
class _CodeFact:
    code: str
    producer: str
    data: str


#: Every reachable code, with its producer and the data it carries on this
#: boundary.  Domain-step rejections keep only code and detail, so they carry
#: no data; only the boundary-minted reservation-unknown error has data.
_INVENTORY: tuple[_CodeFact, ...] = (
    _CodeFact("INVALID_ARGUMENT", "lifecycle_ops._ipc_down/_lifecycle_spec; domain tails; S8 custody guards (cells.runtime.agents refuse_external_launch/refuse_reserved_capabilities)", "none"),
    _CodeFact("LIFECYCLE_MANAGER_UNAVAILABLE", "lifecycle_ops._run_lifecycle_operation", "none"),
    _CodeFact("SUBMIT_OUTCOME_UNKNOWN", "lifecycle_ops._run_lifecycle_operation", "{operationId}"),
    _CodeFact("DAEMON_START_FAILED", "lifecycle_ops._run_lifecycle_operation (admission refusal; code-less terminal result; code-less step failure)", "none"),
    _CodeFact("LIFECYCLE_OPERATION_UNSETTLED", "lifecycle_ops._run_lifecycle_operation (wait timeout)", "none"),
    _CodeFact("LIFECYCLE_OPERATION_TIMEOUT", "lifecycle_coordinator._begin_deadline_compensation/_settle_unresolved", "none"),
    _CodeFact("LIFECYCLE_COMPENSATION_TIMEOUT", "lifecycle_coordinator._settle_unresolved_compensation", "none"),
    _CodeFact("SAGA_INTERRUPTED_BY_RESTART", "lifecycle_manager.store.interrupt_running_operations", "none"),
    _CodeFact("PORT_CLOSING", "atomic_lifecycle_ports; composition.domains; harnesses.actor", "none"),
    _CodeFact("PORT_OVERLOADED", "atomic_lifecycle_ports; composition.domains; harnesses.actor", "none"),
    _CodeFact("LIFECYCLE_DOMAIN_FAILED", "atomic_lifecycle_ports wait fallback", "none"),
    _CodeFact("DOMAIN_COMMAND_TIMEOUT", "composition.events CorrelatedDomainEvents.wait", "none"),
    _CodeFact("ROUTE_PARTIAL_CLEANUP", "route_registration.io._fail_owned", "none"),
    _CodeFact("ROUTE_LEASE_STALE", "route_registration.io._fail_owned", "none"),
    _CodeFact("ROUTE_IO_FAILED", "route_registration.io._fail_owned", "none"),
    _CodeFact("ROUTE_CLOSED_BEFORE_COMPLETION", "route_registration.io._invalidate_route_completions", "none"),
    _CodeFact("LIFECYCLE_INCARNATION_UNSUPPORTED", "route_registration.io incarnation fence; registry AgentLifecycleFenceUnsupported", "none"),
    _CodeFact("TRANSFER_CUSTODY_UNAVAILABLE", "route_registration.io incarnation fence", "none"),
    _CodeFact("AGENT_INCARNATION_MISMATCH", "route_registration.io incarnation fence", "none"),
    _CodeFact("HARNESS_RUNTIME_CLOSING", "harnesses.actor submit-closing refusal; behaviors/lifecycle terminalize", "none"),
    _CodeFact("HARNESS_LIFECYCLE_IN_PROGRESS", "harnesses behaviors/lifecycle ensure/remove refusal", "none"),
    _CodeFact("LIFECYCLE_ATTEMPT_SETTLED", "harnesses behaviors/process_events settled-attempt refusal", "none"),
    _CodeFact("HARNESS_LIFECYCLE_STORE_MISSING", "harnesses behaviors/lifecycle store-missing refusal", "none"),
    _CodeFact("HARNESS_LIFECYCLE_RECEIPT_MISMATCH", "harnesses behaviors/desired receipt settlement refusal", "none"),
    _CodeFact("HARNESS_IO_FAILED", "harnesses behaviors/delivery stop-call failure (reaches the removal failure after the retry budget)", "none"),
    _CodeFact("STALE_HARNESS_IO_COMPLETION", "harnesses behaviors/delivery stale I/O completion", "none"),
    _CodeFact("HARNESS_IO_CANCELLED", "harnesses behaviors/delivery cancelled I/O completion", "none"),
    _CodeFact("HARNESS_START_TIMEOUT", "harnesses behaviors/process_events start-observation timeout (compensation ensure)", "none"),
    _CodeFact("HARNESS_START_FAILED", "harnesses behaviors/process_events start-observation failure fallback (compensation ensure)", "none"),
    _CodeFact("HARNESS_COMPLETION_HANDOFF_FAILED", "harnesses behaviors/process_events completion-delivery failure", "none"),
    _CodeFact("AGENT_ERROR", "registry AgentError base; _generation_ops rejection fallback", "none"),
    _CodeFact("AGENT_EXISTS", "registry AgentExistsError", "none"),
    _CodeFact("AGENT_NOT_FOUND", "registry AgentNotFoundError", "none"),
    _CodeFact("AGENT_DESTROY_SETTLEMENT_UNKNOWN", "registry AgentDestroySettlementUnknown", "none"),
    _CodeFact("AGENT_VERSION_CONFLICT", "registry AgentEntityConflict; actor AgentUpdateConflict", "none"),
    _CodeFact("INVALID_AGENT_NAME", "registry InvalidAgentNameError", "none"),
    _CodeFact("AGENT_ALREADY_RUNNING", "state/liveness AgentAlreadyRunning; actor bind refusal", "none"),
    _CodeFact("AGENT_HOME_ACCESS_FAILED", "actor AgentHomeAccessError; home-effect pass-through", "none"),
    _CodeFact("AGENT_RUNTIME_CONTEXT_STALE", "actor AgentRuntimeContextStale", "none"),
    _CodeFact("AGENT_RUNTIME_LAUNCH_MISMATCH", "actor AgentRuntimeLaunchMismatch", "none"),
    _CodeFact("AGENT_LAUNCH_IN_PROGRESS", "actor AgentLaunchInProgress", "none"),
    _CodeFact("AGENT_MIGRATION_BUSY", "actor migration-lease refusal", "none"),
    _CodeFact("SENDER_UNRESOLVED", "actor SenderIdentityError", "none"),
    _CodeFact("ACTOR_STOPPED_UNDELIVERED", "guardian undelivered sink via agent actor _undelivered", "none"),
    _CodeFact("ACTOR_RESTARTED_UNDELIVERED", "guardian undelivered sink via agent actor _undelivered", "none"),
    _CodeFact("ACTOR_QUARANTINED_UNDELIVERED", "guardian undelivered sink via agent actor _undelivered", "none"),
    _CodeFact("USE_ADAPTER_COMMAND", "agent_start._ipc_agent_start/lifecycle_ops._ipc_lifecycle_start adapter-harness refusal", "none"),
    _CodeFact("HARNESS_BINARY_MISSING", "agent_start._ipc_agent_start executable preflight before Agent mutation", "none"),
    _CodeFact("TRANSFER_STARTS_HELD", "lifecycle_ops._ipc_lifecycle_start custody-held refusal", "none"),
    _CodeFact("HARNESS_ARGS_INVALID", "application harness-args merge/validation and actor IO preflight", "launch"),
    _CodeFact("HARNESS_CWD_MISSING", "harnesses/actor/io_port launch-cwd validation (forward ensure)", "launch"),
    _CodeFact("CLAUDE_CREDENTIAL_MISSING", "harnesses/claude/runtime credential preflight", "launch"),
    _CodeFact("CODEX_CREDENTIAL_MISSING", "identity runtime/context Codex credential preflight; preserved by actor home-effect dispatch", "launch"),
    _CodeFact("PI_P2_LAUNCH_UNTRUSTED", "harnesses/pi/loader project-trust resolution", "launch"),
    _CodeFact("AGENT_SECRET_RESOLUTION_FAILED", "identity runtime/secrets resolver; agents_admin registry_ops custody materializer", "launch"),
    _CodeFact("AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED", "agents_admin/registry_ops runtime-launch custody lease", "launch"),
    _CodeFact("SHARED_CREDENTIAL_CONFLICT", "identity runtime/context launch shared-credential environment check", "launch"),
    _CodeFact("PROVIDER_UNSAFE_PATH", "identity agents/home/native_credential legacy Claude native-root preparation", "launch"),
    _CodeFact("UNSUPPORTED", "identity agents/home/native_credential descriptor-relative I/O prerequisite", "launch"),
    _CodeFact("WORKER_PROXY_SETTINGS_INVALID", "identity agents/state/worker_proxy per-launch read", "launch"),
    _CodeFact("WORKER_PROXY_URL_INVALID", "identity agents/state/worker_proxy per-launch read", "launch"),
    _CodeFact("WORKER_PROXY_VENDORS_INVALID", "identity agents/state/worker_proxy per-launch read", "launch"),
)

#: Candidates the enumeration proves cannot reach the agent.stop /
#: agent.create boundary, with the reason.  The guard fails when a new
#: literal appears, so every exclusion is a recorded decision, not an
#: oversight.
_EXCLUSIONS: dict[str, str] = {
    "LIFECYCLE_FAIL_CLOSED": (
        "process.py refuses only operation ids prefixed cell-reconcile:, whose "
        "effects the cell reconcile actor runs; agent.stop and agent.create "
        "submit their own fresh operation ids, so their results never carry it."
    ),
    "LEGACY_OPERATION_UNSUPPORTED": (
        "Marks persisted incomplete operations that no longer parse (store "
        "sweep and process.py planning); agent.stop and agent.create submit a "
        "fresh, parseable operation, so their results never carry it."
    ),
    "SAGA_INTERRUPTED_BY_RESTART": (
        "interrupt_running_operations only marks operations that were still "
        "RUNNING when the coordinator was constructed; an agent.stop submits "
        "a fresh operation id, so its result can never carry this code."
    ),
    "ROUTE_PARTIAL_CLEANUP": (
        "process.py turns this RouteMutationFailed code into "
        "LifecycleStepUnresolved, not into a result code; the operation "
        "later settles as LIFECYCLE_OPERATION_TIMEOUT or "
        "LIFECYCLE_COMPENSATION_TIMEOUT."
    ),
    "HARNESS_IO_CANCELLED": (
        "Published only when the correlation's pending waiter was already "
        "popped, so no manager waiter can receive it as a step event."
    ),
    "HARNESS_RESULT_CLAIM_OVERLOADED": (
        "Result-claim waiters lane; the stop walk dispatches no result "
        "claims."
    ),
    "HARNESS_RUNTIME_STOPPED": (
        "Delivery/observation lane refusal; lifecycle commands are refused "
        "at admission with HARNESS_RUNTIME_CLOSING instead."
    ),
    "HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED": (
        "Observation authorization lane; the stop walk observes nothing."
    ),
    "HARNESS_START_SUPERSEDED": (
        "Explicit-start correlation lane; lifecycle-command correlations are "
        "minted by the manager and never carry an explicit start id."
    ),
    "HARNESS_PERSISTENCE_UNAVAILABLE": (
        "Desired-state persistence prerequisite of harness state commands; "
        "lifecycle commands are not persisted at admission, and receipt "
        "persistence failures surface through the failed-removal/ensure "
        "flows whose producer codes are inventoried."
    ),
    "HARNESS_PERSISTENCE_OVERLOADED": (
        "Same persistence-prerequisite lane as "
        "HARNESS_PERSISTENCE_UNAVAILABLE."
    ),
    "HARNESS_PERSISTENCE_FENCE_INVALID": (
        "Same persistence-prerequisite lane as "
        "HARNESS_PERSISTENCE_UNAVAILABLE."
    ),
    "HARNESS_PERSISTENCE_FAILED": (
        "Same persistence-prerequisite lane as "
        "HARNESS_PERSISTENCE_UNAVAILABLE (restore mark-failed and terminal "
        "persistence contexts)."
    ),
    "DOMAIN_CORRELATION_CONFLICT": (
        "Raised only by the settled-join helpers (reserve_settlement/"
        "wait_settled); the stop and create walks join through plain wait."
    ),
    "DOMAIN_EVENT_CAPACITY": (
        "Same settled-join helpers as DOMAIN_CORRELATION_CONFLICT."
    ),
    "DOMAIN_EVENTS_CLOSED": (
        "Same settled-join helpers as DOMAIN_CORRELATION_CONFLICT."
    ),
    "STALE_LIFECYCLE_RECEIPT": (
        "Receipt-retirement fence check (port retire_receipt), not a step "
        "wait; a retirement failure escapes advance as a non-step error, so "
        "the operation stays RUNNING and the boundary answers "
        "LIFECYCLE_OPERATION_UNSETTLED."
    ),
    "STALE_AGENT_RECEIPT": (
        "Same receipt-retirement path as STALE_LIFECYCLE_RECEIPT."
    ),
    "ANTHROPIC_OAUTH_TOKEN": (
        "OAuth environment-name constant in the Claude credential modules; "
        "an upper-case name, not an error code."
    ),
    "CLAUDE_CODE_OAUTH_TOKEN": (
        "OAuth environment-name constant, as ANTHROPIC_OAUTH_TOKEN."
    ),
    "WORKER_PROXY_KEY_UNKNOWN": (
        "write_worker_proxy_field (config set) only; the launch read path "
        "(read_worker_proxy) never raises it."
    ),
    "SHARED_CREDENTIAL_UNSUPPORTED": (
        "validate_shared_credential_binding runs only on the agent actor's "
        "home-effect lane and the read-only shared_credential_status "
        "projection; the home-effect completion maps every non-CODEX error "
        "code to AGENT_HOME_ACCESS_FAILED (declared) and the projection "
        "converts refusals into status rows, so the raw code never reaches "
        "a start boundary."
    ),
    "SHARED_CREDENTIAL_INVALID": (
        "Same home-effect/projection lane as SHARED_CREDENTIAL_UNSUPPORTED."
    ),
    "SHARED_CREDENTIAL_DIVERGED": (
        "Same home-effect/projection lane as SHARED_CREDENTIAL_UNSUPPORTED."
    ),
    "PROVIDER_CREDENTIAL_EXISTS": (
        "native_credential install path (grant/login management); the start "
        "walk only reads credentials."
    ),
    "PROVIDER_SOURCE_MISSING": (
        "Same grant/login management lane as PROVIDER_CREDENTIAL_EXISTS."
    ),
    "PROVIDER_UNSUPPORTED": (
        "native_credential _allowed gate for install/remove management; "
        "prepare_legacy_claude_native_root (the launch-path producer) raises "
        "only UNSUPPORTED and PROVIDER_UNSAFE_PATH."
    ),
    "DAEMON_ERROR": (
        "native_credential remove-path literal; DAEMON_ERROR is the "
        "registry's own replacement code, declared in _WRITE_ERRORS, never "
        "a producer-declared code on these walks."
    ),
    "STALE_AGENT_GRANT": (
        "agents_admin grant/revoke completion-fence stamp; the start walk "
        "dispatches no grant or revoke commands."
    ),
    "STALE_AGENT_SECRET_GRANT": (
        "Same grant/revoke fence lane as STALE_AGENT_GRANT."
    ),
    "STALE_AGENT_HOME_CLEANUP": (
        "agents_admin home-cleanup fence stamp; the start walk dispatches "
        "no cleanup commands."
    ),
    "CODEX_RESUME_OUTSIDE_SCOPE": (
        "codex app-server resume-RPC lane on an already-started server; the "
        "fresh launch path fails through HarnessStartError (no code) into "
        "the declared HARNESS_START_FAILED fallback."
    ),
    "CODEX_RESUME_RPC_FAILED": (
        "Same resume-RPC lane as CODEX_RESUME_OUTSIDE_SCOPE."
    ),
    "CODEX_RESUME_THREAD_UNKNOWN": (
        "Same resume-RPC lane as CODEX_RESUME_OUTSIDE_SCOPE."
    ),
    "CODEX_CUSTOM_PROVIDER_AUTH_BACKUP": (
        "An AgentRuntimeNotice code (non-fatal notice), not an error; "
        "notices travel in the result warnings, never as error codes."
    ),
}

_STOP_METHOD = "agent.stop"
_CREATE_METHOD = "agent.create"
_START_METHOD = "agent.start"

#: Codes only the start walk can produce: the launch funnel refuses adapter
#: harnesses and custody-held agents before any lifecycle operation runs,
#: and the forward plan's ``harness.ensure`` reaches the launch producers
#: (IO-port cwd validation, credential preflights, secret resolution,
#: runtime-launch custody, shared-credential check, native-root preparation,
#: per-launch worker-proxy read) that the stop walk reaches only as
#: compensation, where the coordinator preserves the original forward code.
_START_ONLY_CODES = frozenset(
    {
        "USE_ADAPTER_COMMAND",
        "HARNESS_BINARY_MISSING",
        "TRANSFER_STARTS_HELD",
        "HARNESS_ARGS_INVALID",
        "HARNESS_CWD_MISSING",
        "CLAUDE_CREDENTIAL_MISSING",
        "CODEX_CREDENTIAL_MISSING",
        "PI_P2_LAUNCH_UNTRUSTED",
        "AGENT_SECRET_RESOLUTION_FAILED",
        "AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED",
        "SHARED_CREDENTIAL_CONFLICT",
        "PROVIDER_UNSAFE_PATH",
        "UNSUPPORTED",
        "WORKER_PROXY_SETTINGS_INVALID",
        "WORKER_PROXY_URL_INVALID",
        "WORKER_PROXY_VENDORS_INVALID",
    }
)

#: Codes only the stop walk can produce: it runs the deactivate saga (the
#: compensation half, the harness and route steps); create does not.
_STOP_ONLY_CODES = frozenset(
    {
        "LIFECYCLE_MANAGER_UNAVAILABLE",
        "SUBMIT_OUTCOME_UNKNOWN",
        "DAEMON_START_FAILED",
        "LIFECYCLE_OPERATION_UNSETTLED",
        "LIFECYCLE_OPERATION_TIMEOUT",
        "LIFECYCLE_COMPENSATION_TIMEOUT",
        "ROUTE_LEASE_STALE",
        "ROUTE_IO_FAILED",
        "ROUTE_CLOSED_BEFORE_COMPLETION",
        "LIFECYCLE_INCARNATION_UNSUPPORTED",
        "TRANSFER_CUSTODY_UNAVAILABLE",
        "AGENT_INCARNATION_MISMATCH",
        "HARNESS_RUNTIME_CLOSING",
        "HARNESS_LIFECYCLE_IN_PROGRESS",
        "LIFECYCLE_ATTEMPT_SETTLED",
        "HARNESS_LIFECYCLE_STORE_MISSING",
        "HARNESS_LIFECYCLE_RECEIPT_MISMATCH",
        "HARNESS_IO_FAILED",
        "STALE_HARNESS_IO_COMPLETION",
        "HARNESS_IO_CANCELLED",
        "HARNESS_START_TIMEOUT",
        "HARNESS_START_FAILED",
        "HARNESS_COMPLETION_HANDOFF_FAILED",
        "LIFECYCLE_DOMAIN_FAILED",
    }
)


def _declared_codes(method: str) -> set[str]:
    spec = METHOD_REGISTRY.get(method).spec
    return {error.code for error in spec.errors}


def _inventory_reachable() -> set[str]:
    return {
        fact.code
        for fact in _INVENTORY
        if fact.code not in _EXCLUSIONS
    }


# --- shared walk fixtures -----------------------------------------------------


class _Unused:
    pass


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()

def _application(root: Path):
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    app = DaemonApplication(
        state_dir=root,
        socket_path=root / "daemon.sock",
        node_id="inventory-node",
        owner="inventory-owner",
        **daemon_dependencies(),
    )
    app._inbox = _Unused()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Unused()  # type: ignore[assignment]
    return app


def _desired_harness(app) -> None:
    from hyprial.kernel import HarnessLaunchSpec

    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )


class _TerminalManager:
    """Manager seam whose accepted operation ends FAILED with a fixed code."""

    crashed = False

    def __init__(self, error_code: str) -> None:
        self._error_code = error_code

    def submit(self, _operation):

        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, _timeout: float):
        from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
            LifecycleResult,
            LifecycleState,
        )

        return LifecycleResult(
            operation_id=operation_id,
            state=LifecycleState.FAILED,
            completed_effects=(),
            compensated_effects=(),
            error=f"terminal failure: {self._error_code}",
            error_code=self._error_code,
        )

    def drain(self, _timeout: float) -> bool:
        return True


class _ProbeHarnesses:
    def status(self):
        return ()


def _wire_af_unix(app, method: str, params: dict) -> dict:
    import json as json_module
    import socket
    import threading

    server, client = socket.socketpair()
    client.settimeout(10)
    thread = threading.Thread(target=app._serve_client, args=(server,))
    thread.start()
    try:
        client.sendall(
            json_module.dumps(
                dict(version=1, id="inventory", method=method, params=params)
            ).encode()
            + b"\n"
        )
        received = bytearray()
        while b"\n" not in received:
            data = client.recv(65536)
            if not data:
                break
            received.extend(data)
        return json_module.loads(bytes(received))
    finally:
        client.close()
        thread.join(10)
        assert not thread.is_alive()


__all__ = [
    "SRC_ROOT",
    "REPO_ROOT",
    "_CodeFact",
    "_EXCLUSIONS",
    "_INVENTORY",
    "_STOP_METHOD",
    "_CREATE_METHOD",
    "_START_METHOD",
    "_START_ONLY_CODES",
    "_STOP_ONLY_CODES",
    "_TerminalManager",
    "_ProbeHarnesses",
    "_Unused",
    "_Harnesses",
    "_application",
    "_declared_codes",
    "_desired_harness",
    "_inventory_reachable",
    "_wire_af_unix",
    "scan_candidates",
]
