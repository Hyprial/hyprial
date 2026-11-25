"""Attach and detach an externally supervised harness as an agent (design §10.1 S8).

Decision notes (phase B, S8):

- attach registers an agent identity whose single cell member is a harness
  with custody=external: the daemon never launches, restores or supervises
  that harness.  Messages addressed to the agent land in its durable inbox
  and the external harness reads and answers them through the existing
  ``message.*`` methods.
- The identity is created through the agent directory's public ``create``
  port only.  It is not the ``agent.create`` path: that path binds the
  default ``agent-home-setup`` routine (a 15 m dispatch that would ask the
  daemon to drive the agent), prepares a daemon workspace and a P2 config
  root, none of which belong to an external harness.  The persona route is
  still declared so remote peers reach the agent, as ``agent.create`` does.
- Custody and attachment are facts on the local agent record
  (``capabilities.custody``/``capabilities.attached``), written through the
  directory's ``create`` and ``compare_and_swap`` ports.  The record is the
  authority and needs no OrgFS, so attach, detach and messaging all work
  while OrgFS is unavailable; the agent cell is reconciled from the record by
  the cell runtime's agent registrar when the runtime composes or changes.
- Idempotent by name: attaching an attached agent and detaching a detached
  one change nothing (``changed: false``); a concurrent create of the same
  name that loses the race reuses the winner when it is external.  A name held
  by a daemon-managed agent is never converted (``AGENT_EXISTS``), and detach
  refuses a managed agent (``INVALID_ARGUMENT``: detach is not stop/destroy).
- detach removes the harness member and keeps the identity, its ``a.``/``c.``
  ids, its inbox and history; messages to it keep queueing until a harness
  attaches again.  Retiring the cell tombstone is left to agent destroy.
- The cell is written by the cell runtime's agent registrar only
  (``registrar.settle``): it re-reads the record under its own lock, so a
  detach racing a re-attach can never leave the cell on a stale snapshot,
  and a write that fails is retried by the registrar without a restart.
- ``cell.state`` reports where the cell change stands: ``recorded`` (in the
  local cell cache, flushed to OrgFS by the runtime thread); ``deferred``
  (no cell runtime now, or a transient failure: the registrar reconciles it
  when the runtime returns or on its retry timer); ``skipped`` (the agent's
  owner is not the OrgFS author, so no agent cell is kept, phase A rule);
  ``refused`` (the cell runtime refused the cell, for example a retired
  cell; not retried until the record changes).
- ``AGENT_VERSION_CONFLICT`` only after ``CAS_ATTEMPTS`` swaps lost to a
  real change of this agent's record: the directory's compare-and-swap is
  fenced by a global version, so a lost swap whose re-read shows this
  record unchanged (another agent moved) is retried, up to
  ``CAS_TRIES_MAX`` in all.  A concurrent writer of the same record is
  reported, never overwritten.
- The record convention is enforced elsewhere too: ``agent.create`` cannot
  write the ``custody`` capability and no daemon launch path accepts an
  external-custody agent (``refuse_external_launch``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from hyprial.daemon.impl.cells.runtime.agents import (
    ATTACHED_CAPABILITY,
    CUSTODY_CAPABILITY,
    CUSTODY_EXTERNAL,
    agent_custody,
    is_attached,
)
from hyprial.daemon.impl.composition.cells.wiring import cell_runtime
from hyprial.kernel import DaemonRequestError, ipc_errors

AGENT_ERROR = "AGENT_ERROR"
CAS_ATTEMPTS = 2
CAS_TRIES_MAX = 16
DEFERRED_CELL = {"state": "deferred", "revision": None}

JsonObject = dict[str, Any]


def _domain_error(error: BaseException) -> DaemonRequestError:
    code = getattr(error, "code", None)
    if code in (
        ipc_errors.AGENT_EXISTS, ipc_errors.AGENT_VERSION_CONFLICT,
        ipc_errors.AGENT_NOT_FOUND,
    ):
        return DaemonRequestError(str(code), str(error))
    if code == ipc_errors.INVALID_AGENT_NAME:
        return DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error))
    return DaemonRequestError(AGENT_ERROR, str(error) or type(error).__name__)


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    logger = getattr(app, "_log", None)
    if logger is None:
        return
    try:
        logger(level, "agents", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks attach
        pass


def _native_name(app: Any, name: str) -> str:
    if ":" in name:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "attach takes a bare agent name; the canonical URI is derived from it",
        )
    try:
        return app.agents.native_actor(name)
    except DaemonRequestError:
        raise
    except Exception as error:  # noqa: BLE001 - mapped to declared codes
        raise _domain_error(error) from error


def _facts(attached: bool) -> dict[str, Any]:
    return {CUSTODY_CAPABILITY: CUSTODY_EXTERNAL, ATTACHED_CAPABILITY: attached}


def _refuse_managed(agent: Any) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.AGENT_EXISTS,
        f"the name {agent.actor!r} is held by a daemon-managed agent ({agent.uri}); "
        "attach never converts a managed agent to custody=external — choose another "
        "name, or destroy that agent first",
        {"actor": agent.uri, "agent": agent.actor},
    )


def _create(app: Any, name: str) -> tuple[Any, bool]:
    try:
        return app.agents.create(name, capabilities=_facts(True)), True
    except Exception as error:  # noqa: BLE001 - a lost create race is reused
        if getattr(error, "code", None) != ipc_errors.AGENT_EXISTS:
            raise _domain_error(error) from error
    existing = app.agents.get(name)
    if existing is None:
        raise DaemonRequestError(AGENT_ERROR, f"agent {name!r} vanished while attaching")
    return existing, False


def _set_attached(app: Any, name: str, attached: bool) -> tuple[Any, bool]:
    conflicts = 0
    current = app.agents.get(name)
    for _ in range(CAS_TRIES_MAX):
        if current is None:
            raise DaemonRequestError(
                ipc_errors.AGENT_NOT_FOUND, f"no agent named {name!r} on this machine"
            )
        if agent_custody(current) != CUSTODY_EXTERNAL:
            return current, False
        if is_attached(current) == attached:
            return current, False
        replacement = replace(
            current, capabilities={**dict(current.capabilities), **_facts(attached)}
        )
        try:
            swapped = app.agents.compare_and_swap(current, replacement)
        except Exception as error:  # noqa: BLE001 - mapped to declared codes
            raise _domain_error(error) from error
        if swapped:
            settled = app.agents.get(name)
            return (settled if settled is not None else replacement), True
        fresh = app.agents.get(name)
        if fresh != current:
            conflicts += 1
            if conflicts >= CAS_ATTEMPTS:
                break
        current = fresh
    raise DaemonRequestError(
        ipc_errors.AGENT_VERSION_CONFLICT,
        f"agent {name!r} changed concurrently while its attachment was being set; "
        "read it again and retry",
    )


def _cell_state(app: Any, agent: Any) -> JsonObject:
    runtime = cell_runtime(app)
    registrar = getattr(app, "_agent_cell_registrar", None)
    if runtime is None or registrar is None:
        return dict(DEFERRED_CELL)
    try:
        return registrar.settle(agent.actor)
    except Exception as error:  # noqa: BLE001 - cells never break attach
        _log(app, "warn", "agent.attach.cell_failed", actor=agent.uri,
             errorType=type(error).__name__, detail=str(error)[:300])
        return dict(DEFERRED_CELL)


def _declare_route(app: Any, agent: Any) -> None:
    declare = getattr(app, "_declare_persona_route", None)
    if not callable(declare):
        return
    try:
        declare(agent.uri)
    except Exception as error:  # noqa: BLE001 - the route is retried by later sessions
        _log(app, "warn", "agent.attach.route_failed", actor=agent.uri,
             errorType=type(error).__name__, detail=str(error)[:300])


def _result(agent: Any, *, changed: bool, cell: JsonObject) -> JsonObject:
    return {
        "ok": True,
        "actor": agent.uri,
        "name": agent.actor,
        "agentId": agent.agent_id,
        "cellId": agent.cell_id,
        "custody": CUSTODY_EXTERNAL,
        "attached": is_attached(agent),
        "changed": changed,
        "cell": cell,
    }


def attach_agent(app: Any, name: str) -> JsonObject:
    """Register (or reuse) the external-custody agent *name* and attach its harness."""

    native = _native_name(app, name)
    existing = app.agents.get(native)
    if existing is not None and agent_custody(existing) != CUSTODY_EXTERNAL:
        raise _refuse_managed(existing)
    created = False
    if existing is None:
        from hyprial.daemon.impl.bootstrap.names import require_agent_name_available

        require_agent_name_available(app, native)
        agent, created = _create(app, native)
        if agent_custody(agent) != CUSTODY_EXTERNAL:
            raise _refuse_managed(agent)
    agent, flipped = _set_attached(app, native, True)
    if agent_custody(agent) != CUSTODY_EXTERNAL:
        raise _refuse_managed(agent)
    _declare_route(app, agent)
    changed = created or flipped
    _log(app, "info", "agent.attached", actor=agent.uri, created=created, changed=changed)
    return {**_result(agent, changed=changed, cell=_cell_state(app, agent)),
            "created": created}


def detach_agent(app: Any, name: str) -> JsonObject:
    """Remove the external harness member of *name*; identity and inbox stay."""

    local = app.agents.local_actor(name)
    agent = None if local is None else app.agents.get(local)
    if agent is None:
        raise DaemonRequestError(
            ipc_errors.AGENT_NOT_FOUND, f"no agent named {name!r} on this machine"
        )
    if agent_custody(agent) != CUSTODY_EXTERNAL:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"agent {agent.actor!r} is daemon-managed; detach applies only to agents "
            "attached with custody=external (use 'hyprial agent stop' or "
            "'hyprial agent destroy' for a managed agent)",
        )
    agent, changed = _set_attached(app, agent.actor, False)
    _log(app, "info", "agent.detached", actor=agent.uri, changed=changed)
    return _result(agent, changed=changed, cell=_cell_state(app, agent))


__all__ = ["AGENT_ERROR", "attach_agent", "detach_agent"]
