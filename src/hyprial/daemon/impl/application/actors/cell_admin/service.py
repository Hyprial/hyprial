# Decision record (I6-3, plan §H.6):
# - ``get_agent_cell`` reads strictly from in-memory ``CellRuntime.lookup`` and
#   ``CellActorRegistry.snapshot`` without touching OrgFS or triggering lazy
#   runtime composition.
# - ``update_agent_cell`` enforces ``expectedRevision`` CAS, restricts edits to
#   the whitelist (``cellName``/``members``/``params``), rejects immutable fields
#   and ``retire``, validates through ``Cell``/``codec``/``parse_structure``,
#   and fails closed with ``ORG_SOURCE_UNREACHABLE`` when OrgFS is unavailable.
# Decision record (I7a, contract i7-0 v2 section 2.3, owner item A-1):
# - The user agent cell (current param ``agent.template`` is ``user``) is
#   read and written here like any agent cell, within one allow-list: the
#   candidate may differ from the current document only in its adapter
#   members, their ``cell.members.<ref>`` object with the single key
#   ``adapter``, and ``cell.connections`` rows ``{from, on: "user", to,
#   route?}`` between members.  Everything else must stay equal, so the
#   output of ``cell show`` round-trips through ``cell apply``.  A changed
#   or removed template is ``USER_AGENT_IMMUTABLE``; any other difference is
#   ``INVALID_ARGUMENT``.  The check runs on the merged candidate, after the
#   cell is loaded, so the value ``user`` is accepted exactly when the cell
#   already holds it and an ordinary cell can never take it.  The rule keys on
#   the agent record (template capability) as well as the cell param, so a
#   user agent cell whose template seed has not landed yet fails closed.
# - Only the local operator reads the user agent cell (it names the owner's
#   adapters and routes); any other caller is answered
#   ``USER_AGENT_IMMUTABLE``.  The caller kind is the router's identity
#   binding check, the same one that admits ``agent.cell.update``.
"""Cell editing core service for ``agent.cell.get`` and ``agent.cell.update``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.cells.codec import decode, encode
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellError, CellMember, thaw_json
from hyprial.daemon.impl.cells.structure import ADAPTER_ROLE, USER_LABEL, parse_structure
from hyprial.daemon.impl.user_agent.naming import (
    USER_AGENT_NAME,
    USER_AGENT_TEMPLATE,
    is_user_agent,
)
from hyprial.identity import AgentError
from hyprial.kernel import DaemonRequestError, ipc_errors, subject_cell_id

ALLOWED_UPDATE_PARAMS = frozenset({"name", "expectedRevision", "cellName", "members", "params"})
FORBIDDEN_CELL_FIELDS = frozenset(
    {
        "id",
        "cellId",
        "kind",
        "authority",
        "binding",
        "scope",
        "retired",
        "retire",
        "revision",
        "permissions",
        "schema",
        "updatedAtMs",
    }
)


def _require_agent_name(params: Mapping[str, Any]) -> str:
    raw = params.get("name")
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "name must be a non-empty trimmed string"
        )
    return raw


def _require_expected_revision(params: Mapping[str, Any]) -> int:
    if "expectedRevision" not in params:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "expectedRevision is required"
        )
    value = params["expectedRevision"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "expectedRevision must be a positive integer"
        )
    return value


def _resolve_agent(app: Any, name: str) -> Any:
    agents = getattr(app, "agents", None)
    if agents is None or not callable(getattr(agents, "get", None)):
        raise DaemonRequestError(
            ipc_errors.DAEMON_ERROR, "agent registry is unavailable"
        )
    try:
        agent = agents.get(name)
    except AgentError as error:
        raise DaemonRequestError(error.code, str(error)) from error
    if agent is None:
        raise DaemonRequestError(
            ipc_errors.AGENT_NOT_FOUND, f"no agent named {name!r} on this machine"
        )
    return agent


def _cell_snippet(cell: Cell) -> dict[str, Any]:
    return {
        "cellId": cell.cell_id,
        "revision": cell.revision,
        "members": [member.to_json() for member in cell.members],
        "params": thaw_json(cell.params),
    }


def _snapshot_json(snapshot: Any) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    active_work = (
        None
        if snapshot.active_work is None
        else {
            "workId": snapshot.active_work.work_id,
            "member": snapshot.active_work.member,
            "hopIndex": snapshot.active_work.hop_index,
            "startedAtMs": snapshot.active_work.started_at_ms,
        }
    )
    return {
        "cellId": snapshot.cell_id,
        "epoch": snapshot.epoch,
        "sequence": snapshot.sequence,
        "revision": snapshot.revision,
        "effectiveRevision": snapshot.effective_revision,
        "observedDocumentDigest": snapshot.observed_document_digest,
        "effectiveDocumentDigest": snapshot.effective_document_digest,
        "entryMember": snapshot.entry_member,
        "state": snapshot.state.value,
        "reason": snapshot.reason,
        "sourceStale": snapshot.source_stale,
        "updatedAtMs": snapshot.updated_at_ms,
        "members": [
            {
                "name": member.name,
                "role": member.role,
                "kind": member.kind,
                "phase": member.phase.value,
                "running": member.running,
                "workId": member.work_id,
                "lastError": member.last_error,
                "policyRevision": member.policy_revision,
                "policyDigest": member.policy_digest,
            }
            for member in snapshot.members
        ],
        "activeWork": active_work,
        "queuedWork": snapshot.queued_work,
        "completedWork": snapshot.completed_work,
        "lastTerminal": snapshot.last_terminal,
        "connections": [
            {
                "from": connection.from_member,
                "on": sorted(connection.on),
                "to": connection.to,
                "route": connection.route,
            }
            for connection in snapshot.connections
        ],
        "failOpen": snapshot.fail_open,
    }


def _ensure_orgfs_connected(app: Any, runtime: Any) -> None:
    if runtime is not None:
        health = runtime.health()
        if getattr(health, "source_stale", False):
            detail = getattr(health, "detail", None) or "source is stale"
            raise DaemonRequestError(
                ipc_errors.ORG_SOURCE_UNREACHABLE,
                f"OrgFS is unavailable ({detail}); cell editing requires a connected source",
            )
    require_orgfs = getattr(app, "_require_orgfs_runtime", None)
    if callable(require_orgfs):
        try:
            facade = require_orgfs().facade
            facade.spaces()
        except DaemonRequestError:
            raise
        except Exception as error:
            raise DaemonRequestError(
                ipc_errors.ORG_SOURCE_UNREACHABLE,
                f"OrgFS is unavailable ({error}); cell editing failed closed",
            ) from error


def _parse_members(raw: object) -> tuple[CellMember, ...]:
    if not isinstance(raw, list):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "members must be a JSON array"
        )
    parsed: list[CellMember] = []
    seen_refs: set[str] = set()
    for item in raw:
        try:
            member = CellMember.from_json(item)
        except CellError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        if member.ref in seen_refs:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, f"duplicate cell member ref {member.ref!r}"
            )
        seen_refs.add(member.ref)
        parsed.append(member)
    return tuple(parsed)


def _parse_params(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "params must be a JSON object"
        )
    for key in raw:
        if not isinstance(key, str) or not key:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "params keys must be non-empty strings"
            )
        if key.startswith("org."):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"agent cell params cannot contain org.* key {key!r}",
            )
    return dict(raw)


def _parse_cell_name(raw: object) -> str:
    if not isinstance(raw, str) or "\x00" in raw:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "cellName must be a string without NUL bytes"
        )
    return raw


def _validate_candidate_cell(cell: Cell, changes: Mapping[str, Any]) -> None:
    try:
        candidate = cell.revised(now_ms=cell.updated_at_ms, **changes)
        encoded = encode(candidate)
        decode(encoded)
    except CellError as error:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    structure = parse_structure(candidate)
    if structure.problems:
        problem = structure.problems[0]
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"invalid cell structure ({problem.code}): {problem.detail}",
        )


def _effective_state(app: Any, cell_id: str) -> tuple[int, str]:
    actor_registry = getattr(app, "_cell_actor_registry", None)
    if actor_registry is not None and callable(getattr(actor_registry, "snapshot", None)):
        snapshot = actor_registry.snapshot(cell_id)
        if snapshot is not None:
            return int(snapshot.effective_revision), str(snapshot.state.value)
    coordinator = getattr(app, "_cell_reconcile_coordinator", None)
    if coordinator is not None and callable(getattr(coordinator, "snapshot", None)):
        rsnap = coordinator.snapshot(cell_id)
        if rsnap is not None:
            return int(rsnap.effective_revision), str(rsnap.state.value)
    return 0, "unknown"


def _require_agent_cell(runtime: Any, agent: Any) -> Cell:
    if runtime is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_ERROR, "cell runtime is unavailable"
        )
    cell = runtime.lookup(agent.cell_id)
    if cell is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_ERROR, f"cell {agent.cell_id} is not loaded"
        )
    if cell.kind != "agent":
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cell {cell.cell_id} has kind {cell.kind!r}; expected 'agent'",
        )
    if cell.retired:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cell {cell.cell_id} is retired and cannot be accessed",
        )
    agent_id = getattr(agent, "agent_id", None)
    if not isinstance(agent_id, str) or not agent_id:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"agent {getattr(agent, 'actor', '')!r} is missing a valid agent_id",
        )
    expected_cell_id = subject_cell_id("agent", agent_id)
    if cell.cell_id != expected_cell_id:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cell {cell.cell_id} does not match subject cell {expected_cell_id} for agent {agent_id!r}",
        )
    expected_binding = CellBinding.subject(agent_id)
    if cell.binding != expected_binding:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cell {cell.cell_id} binding does not match subject({agent_id!r})",
        )
    local_authority = getattr(getattr(runtime, "identity", None), "authority", None)
    if isinstance(local_authority, str) and local_authority and cell.authority != local_authority:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cell {cell.cell_id} has authority {cell.authority!r}; expected {local_authority!r}",
        )
    return cell


_USER_CELL_PARAMS = frozenset({"cell.members", "cell.connections"})
_USER_CONNECTION_KEYS = frozenset({"from", "on", "to", "route"})
_ABSENT = object()


def _is_user_agent_cell(agent: Any, cell: Cell) -> bool:
    return is_user_agent(agent) or cell.param("agent.template") == USER_AGENT_TEMPLATE


def _not_user_cell(detail: str) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.INVALID_ARGUMENT,
        "the user agent cell may change only its adapter members, their "
        f"cell.members adapter and user connections: {detail}",
    )


def _refuse_user_cell_change(cell: Cell, changes: Mapping[str, Any]) -> None:
    current = thaw_json(cell.params)
    params = dict(changes["params"]) if "params" in changes else current
    if params.get("agent.template") != USER_AGENT_TEMPLATE:
        raise DaemonRequestError(
            ipc_errors.USER_AGENT_IMMUTABLE,
            f"cannot change agent.template of {cell.name or USER_AGENT_NAME!r}: it is "
            "the owner's user agent, which the daemon keeps as the owner's identity",
        )
    if changes.get("name", cell.name) != cell.name:
        raise _not_user_cell("cellName cannot change")
    for key in sorted(set(params) | set(current)):
        if key not in _USER_CELL_PARAMS and params.get(key, _ABSENT) != current.get(key, _ABSENT):
            raise _not_user_cell(f"param {key!r} cannot change")
    members = tuple(changes.get("members", cell.members))
    harness = [member.ref for member in members if member.role != ADAPTER_ROLE]
    if harness:
        raise _not_user_cell(f"member {harness[0]!r} is not an adapter member")
    refs = {member.ref for member in members}
    _check_user_cell_members(params.get("cell.members", _ABSENT), refs)
    _check_user_cell_connections(params.get("cell.connections", _ABSENT), refs)


def _check_user_cell_members(raw: object, refs: set[str]) -> None:
    if raw is _ABSENT:
        return
    if not isinstance(raw, Mapping):
        raise _not_user_cell("cell.members must be an object")
    for ref, data in raw.items():
        if ref not in refs or not isinstance(data, Mapping) or set(data) != {"adapter"}:
            raise _not_user_cell(f"cell.members.{ref} must hold only the adapter of a member")
        adapter = data["adapter"]
        if not isinstance(adapter, str) or not adapter:
            raise _not_user_cell(f"cell.members.{ref}.adapter must be a non-empty string")


def _check_user_cell_connections(raw: object, refs: set[str]) -> None:
    if raw is _ABSENT:
        return
    if not isinstance(raw, list):
        raise _not_user_cell("cell.connections must be an array")
    for row in raw:
        keys = set(row) if isinstance(row, Mapping) else set()
        if not {"from", "on", "to"} <= keys <= _USER_CONNECTION_KEYS:
            raise _not_user_cell("a connection has exactly from, on, to and an optional route")
        if not all(isinstance(value, str) for key, value in row.items() if key != "on"):
            raise _not_user_cell("a connection's from, to and route are strings")
        if row["on"] not in (USER_LABEL, [USER_LABEL]):
            raise _not_user_cell(f"a connection carries only the label {USER_LABEL!r}")
        if row["from"] not in refs or row["to"] not in refs:
            raise _not_user_cell("a connection joins two members of this cell")


def _refuse_user_template(changes: Mapping[str, Any]) -> None:
    params = changes.get("params")
    if isinstance(params, Mapping) and params.get("agent.template") == USER_AGENT_TEMPLATE:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"agent cell params cannot set agent.template={USER_AGENT_TEMPLATE!r}: "
            "reserved for the owner's user agent",
        )


def _local_operator(
    app: Any, params: Mapping[str, Any], trusted_message_origin: str | None,
) -> bool:
    bind_check = getattr(app, "_identity_binding_operator", None)
    if callable(bind_check):
        operator, _caller_pid = bind_check(
            dict(params), trusted_message_origin=trusted_message_origin,
        )
        return bool(operator)
    return trusted_message_origin in (None, "ipc")


def get_agent_cell(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    """Read-only lookup of one agent's cell fragment and ``CellActorSnapshot``."""

    if not isinstance(params, Mapping):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "agent.cell.get params must be an object"
        )
    extra = sorted(set(params) - {"name"})
    if extra:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"unsupported parameter(s): {', '.join(extra)}"
        )
    name = _require_agent_name(params)
    agent = _resolve_agent(app, name)
    runtime = getattr(app, "_cell_runtime", None)
    cell = _require_agent_cell(runtime, agent)
    if _is_user_agent_cell(agent, cell) and not _local_operator(app, params, trusted_message_origin):
        raise DaemonRequestError(
            ipc_errors.USER_AGENT_IMMUTABLE,
            f"only the host local operator may inspect the cell of {name!r}: it is "
            "the owner's user agent",
        )
    actor_registry = getattr(app, "_cell_actor_registry", None)
    snapshot = (
        actor_registry.snapshot(cell.cell_id)
        if actor_registry is not None and callable(getattr(actor_registry, "snapshot", None))
        else None
    )
    return {
        "ok": True,
        "cell": _cell_snippet(cell),
        "snapshot": _snapshot_json(snapshot),
    }


def update_agent_cell(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    """CAS-guarded cell document update over ``cellName``/``members``/``params``."""

    del trusted_message_origin
    if not isinstance(params, Mapping):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "agent.cell.update params must be an object"
        )
    forbidden = sorted(set(params) & FORBIDDEN_CELL_FIELDS)
    if forbidden:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"immutable or forbidden cell field(s): {', '.join(forbidden)}",
        )
    unknown = sorted(set(params) - ALLOWED_UPDATE_PARAMS)
    if unknown:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"unsupported parameter(s): {', '.join(unknown)}"
        )
    name = _require_agent_name(params)
    expected_revision = _require_expected_revision(params)
    has_mutation = any(key in params for key in ("cellName", "members", "params"))
    if not has_mutation:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "at least one of cellName, members, or params is required",
        )
    changes: dict[str, Any] = {}
    if "cellName" in params:
        changes["name"] = _parse_cell_name(params["cellName"])
    if "members" in params:
        changes["members"] = _parse_members(params["members"])
    if "params" in params:
        changes["params"] = _parse_params(params["params"])

    agent = _resolve_agent(app, name)
    runtime = getattr(app, "_cell_runtime", None)
    _ensure_orgfs_connected(app, runtime)
    cell = _require_agent_cell(runtime, agent)
    if cell.revision != expected_revision:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cell revision conflict: expected {expected_revision}, current {cell.revision}",
            data={"expected": expected_revision, "current": cell.revision},
        )
    if _is_user_agent_cell(agent, cell):
        _refuse_user_cell_change(cell, changes)
    else:
        _refuse_user_template(changes)
    _validate_candidate_cell(cell, changes)
    try:
        updated = runtime.update(
            cell.cell_id,
            name=changes.get("name"),
            members=changes.get("members"),
            params=changes.get("params"),
            expect_revision=expected_revision,
        )
    except CellError as error:
        if error.code == "stale-revision":
            latest = runtime.lookup(cell.cell_id)
            current_revision = latest.revision if latest is not None else expected_revision
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"cell revision conflict: expected {expected_revision}, current {current_revision}",
                data={"expected": expected_revision, "current": current_revision},
            ) from error
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    applied_revision, state = _effective_state(app, updated.cell_id)
    return {
        "ok": True,
        "cellId": updated.cell_id,
        "revision": updated.revision,
        "appliedRevision": applied_revision,
        "state": state,
    }


__all__ = [
    "ALLOWED_UPDATE_PARAMS",
    "FORBIDDEN_CELL_FIELDS",
    "get_agent_cell",
    "update_agent_cell",
]
