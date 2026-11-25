"""Register every local agent as a cell once the cell runtime composes (S3).

Decision notes (phase A final integration, acceptance open issue for S3):

- ``register_agents`` had no production caller, so no agent cell existed
  unless an owner hand-wrote its document.  ``attach_agent_cells`` is called
  by ``cell_runtime(app)`` right after a runtime is published; it never
  composes the runtime itself, so a daemon boot still performs no cell write
  until a consumer asks for a cell.
- Three triggers, one reconcile: the attach itself (every agent existing at
  composition), the agent directory's ``on_mutation`` port (an agent created
  later, on the agent actor thread after its projection moved), and the
  runtime's ``on_index`` hook (a catch-all for a registry change that did not
  pass through the agent actor, such as a transfer landing row).
- The reconcile reads only the in-memory agent projection
  (``app.agents.list()``) and calls ``CellRuntime.ensure``, which commits to
  the local cell cache and kicks the runtime thread; OrgFS is reached only
  by that thread.  OrgFS being down therefore never slows or fails agent
  creation or messaging: the cell stays dirty and is flushed on return.
- Each agent is handled once per runtime and record state: registered,
  skipped (an owner the runtime's author is not, logged ``cell.agent_skipped``
  once by ``register_agents``) or refused.  A raising directory or ensure is
  logged ``cell.agent_registration_failed`` and retried; a refused agent (for
  example a retired cell) is not retried until its record changes.
- An app without an agent directory (test doubles) is left alone.
- Phase B (S8): the registrar remembers, per cell id, the last
  ``registration_key(agent)`` it handled and its outcome, replacing the
  entry on every change (review fix: a set of keys made the second attach
  or detach of an agent look handled forever).  An external agent's key
  carries its attachment state, so every flip is reconciled, including one
  recorded while no runtime was composed (OrgFS down).
- An external agent whose cell was recorded is also checked against the
  live cell on each trigger (``cell_matches``), so a membership written from
  a stale record snapshot, or edited by hand, is put back to the record.
  The record is the authority for an external agent cell's membership.
- Outcomes: ``recorded``; ``skipped`` (owner is not the runtime author,
  phase A rule); ``refused`` (a ``CellError`` other than stale-revision, for
  example a retired cell: not retried until the record changes);
  ``deferred`` (stale-revision or any other exception: retried).  A deferred
  outcome schedules a retry timer (``RETRY_BASE_S`` doubling to
  ``RETRY_MAX_S``), so healing never waits for a daemon restart or for an
  unrelated trigger; ``detach`` cancels it.
- ``settle(actor)`` is the attach/detach handlers' path: it reads the agent
  record fresh and reconciles it under the registrar's lock, so a handler
  and the triggers never interleave cell writes from different snapshots.
  The lock never waits on the agent actor: ``get``/``list`` read the
  in-memory projection.

Decision notes (phase B, S10; Allen 2026-11-03 "squire is an ordinary agent"):

- An agent's role is cell data.  After its agent cell is ensured, the
  registrar seeds the cell params of the agent's template
  (``agent_templates.template_cell_params``): only keys the cell does not
  hold yet, through ``CellRuntimePort.update``, so an owner edit of the role
  is never overwritten and a plain agent's cell is untouched.  The seed is a
  local cache commit like ``ensure``; OrgFS is reached only by the runtime
  thread.
- Merge note (S8 onto S10): the seed runs inside ``_register`` right after
  ``register_agents``, so ``reconcile`` and ``settle`` both seed, a seed
  failure takes the same recorded/refused/deferred outcome as the ensure,
  and ``settle`` answers the post-seed revision.

Decision notes (I6-5a last step, ruling §13.1.5, coordinator decision A):

- The registrar seeds the ``managed-worker`` template rules into
  ``cell.members.<m>.allow``/``deny`` of each harness member that declares
  neither and whose harness resolves to ``claude`` (the document's ``kind``,
  else the local launch facts, else the harness a launch names).  The write
  merges into ``cell.members`` (``kind``/``model`` and other members kept)
  under ``expect_revision``; a stale revision defers and retries like the
  ensure.  A document with structure problems is never written to.  Other
  harnesses are not seeded: their launch has no policy mapping.
- ``_current`` also asks whether such a member is still unseeded, so an
  agent registered before its harness was known is seeded once it is.
- ``seed_launch_policy`` is ``lifecycle.start``'s hook (``agent.start``,
  routine and teleport launches; boot restore resumes its operation without
  it), called after the record exists and before the launch.  Launching a
  claude member is a consumer asking for its cell, so it composes the runtime
  if needed like any other consumer, seeds under the registrar's lock and
  waits at most ``SEED_WAIT_S`` on a daemon thread; a timeout or failure is
  logged and the start proceeds with the document as it is.  It never raises.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.agent_templates import template_cell_params
from hyprial.daemon.impl.cells.model import Cell, CellError
from hyprial.daemon.impl.cells.ports import CellRuntimePort
from hyprial.daemon.impl.cells.structure import (
    CELL_MEMBERS_PARAM,
    HARNESS_ROLE,
    StructureMember,
    parse_structure,
)
from hyprial.daemon.impl.cells.runtime.agents import (
    CUSTODY_EXTERNAL,
    agent_custody,
    external_member_matches,
    register_agents,
    registration_key,
)
from hyprial.daemon.impl.composition.cells.reconcile import AppLocalMemberFacts
from hyprial.daemon.impl.harnesses.turn_delivery.channel.templates import (
    load_worker_channel_template,
)

REGISTRATION_FAILED_EVENT = "cell.agent_registration_failed"
RETRY_BASE_S = 1.0
RETRY_MAX_S = 60.0
RECORDED = "recorded"
DEFERRED = "deferred"
SKIPPED = "skipped"
REFUSED = "refused"
SEED_FAILED_EVENT = "cell.member_policy_seed_failed"
SEED_HARNESS = "claude"
SEED_TEMPLATE = "managed-worker"
SEED_WAIT_S = 2.0

HarnessOf = Callable[[Any, str], "str | None"]


class AgentCellRegistrar:
    """Keep the agent cell of each local agent in step with its record."""

    def __init__(
        self,
        runtime: CellRuntimePort,
        agents: Any,
        logger: Any,
        harness_of: HarnessOf | None = None,
    ) -> None:
        self._runtime = runtime
        self._agents = agents
        self._logger = logger
        self._harness_of = harness_of
        self._lock = threading.Lock()
        self._handled: dict[str, tuple[str, str]] = {}
        self._detachers: list[Callable[[], None]] = []
        self._retry: threading.Timer | None = None
        self._delay = RETRY_BASE_S
        self._closed = False

    def reconcile(self, _generation: int | None = None) -> int:
        """Reconcile every agent not in step yet; returns how many cells were registered."""

        with self._lock:
            try:
                agents = tuple(self._agents.list())
            except Exception as error:  # noqa: BLE001 - the directory never breaks cells
                self.failed(None, error)
                self._after(pending=True)
                return 0
            registered = 0
            pending = False
            for agent in agents:
                try:
                    if self._current(agent):
                        continue
                except Exception as error:  # noqa: BLE001 - a broken record is retried
                    self.failed(agent, error)
                    pending = True
                    continue
                outcome, cells = self._register(agent)
                registered += len(cells)
                pending = pending or outcome == DEFERRED
            self._after(pending=pending)
            return registered

    def settle(self, actor: str) -> dict[str, Any]:
        """Reconcile *actor*'s cell from a fresh read of its record; never raises."""

        with self._lock:
            try:
                agent = self._read(actor)
            except Exception as error:  # noqa: BLE001 - answered as deferred, retried
                self.failed(None, error)
                agent = None
            if agent is None:
                self._after(pending=True)
                return {"state": DEFERRED, "revision": None}
            outcome, cells = self._register(agent)
            self._after(pending=outcome == DEFERRED)
            return {"state": outcome, "revision": cells[0].revision if cells else None}

    def seed_for_launch(self, actor: str, harness: str) -> str:
        """Register *actor*'s cell and seed its member rules for a *harness* launch."""

        with self._lock:
            try:
                agent = self._read(actor)
            except Exception as error:  # noqa: BLE001 - the start proceeds unseeded
                self.failed(None, error)
                agent = None
            if agent is None:
                return DEFERRED
            outcome, _cells = self._register(agent, harness=harness)
            self._after(pending=outcome == DEFERRED)
            return outcome

    def track(self, undo: Callable[[], None]) -> None:
        self._detachers.append(undo)

    def detach(self) -> None:
        """Stop listening and cancel a pending retry; idempotent."""

        with self._lock:
            self._closed = True
            retry, self._retry = self._retry, None
            undoers, self._detachers = self._detachers, []
        if retry is not None:
            retry.cancel()
        for undo in undoers:
            try:
                undo()
            except Exception:  # noqa: BLE001 - detaching is best effort
                pass

    def _member_harness(
        self, agent: Any, member: StructureMember, harness: str | None
    ) -> str | None:
        if member.kind is not None:
            return member.kind
        if harness is not None and member.name == str(agent.uri):
            return harness
        if self._harness_of is None:
            return None
        return self._harness_of(agent, member.name)

    def _unseeded(
        self, cell: Cell, agent: Any, harness: str | None = None
    ) -> tuple[str, ...]:
        if cell.retired or agent_custody(agent) == CUSTODY_EXTERNAL:
            return ()
        structure = parse_structure(cell)
        if structure.problems:
            return ()
        return tuple(
            member.name
            for member in structure.members
            if member.role == HARNESS_ROLE
            and member.allow is None
            and member.deny is None
            and self._member_harness(agent, member, harness) == SEED_HARNESS
        )

    def _seed_policy(self, cell: Cell, agent: Any, harness: str | None = None) -> Cell:
        names = self._unseeded(cell, agent, harness)
        if not names:
            return cell
        allow, deny = load_worker_channel_template(SEED_TEMPLATE).member_rules()
        raw = cell.param(CELL_MEMBERS_PARAM.key)
        declared = dict(raw) if isinstance(raw, dict) else {}
        for name in names:
            item = declared.get(name)
            entry = dict(item) if isinstance(item, dict) else {}
            entry["allow"] = list(allow)
            entry["deny"] = list(deny)
            declared[name] = entry
        current = {key: cell.param(key) for key in cell.params}
        return self._runtime.update(
            cell.cell_id,
            params={**current, CELL_MEMBERS_PARAM.key: declared},
            expect_revision=cell.revision,
        )

    def _seed_template(self, cell: Cell, agent: Any) -> Cell:
        seeds = template_cell_params(agent)
        missing = {key: value for key, value in seeds.items() if key not in cell.params}
        if not missing:
            return cell
        current = {key: cell.param(key) for key in cell.params}
        return self._runtime.update(cell.cell_id, params={**current, **missing})

    def failed(self, agent: Any, error: BaseException) -> None:
        if self._logger is None:
            return
        try:
            self._logger("warn", "cells", REGISTRATION_FAILED_EVENT,
                         agent=None if agent is None else str(agent.uri),
                         errorType=type(error).__name__, detail=str(error)[:300])
        except Exception:  # noqa: BLE001 - logging never breaks registration
            pass

    def _read(self, actor: str) -> Any:
        get = getattr(self._agents, "get", None)
        if callable(get):
            return get(actor)
        return next((item for item in self._agents.list() if item.actor == actor), None)

    def _current(self, agent: Any) -> bool:
        handled = self._handled.get(agent.cell_id)
        if handled is None or handled[0] != registration_key(agent):
            return False
        if handled[1] != RECORDED:
            return True
        if agent_custody(agent) != CUSTODY_EXTERNAL:
            cell = self._runtime.index().get(agent.cell_id)
            return cell is None or not self._unseeded(cell, agent)
        return external_member_matches(self._runtime.index().get(agent.cell_id), agent)

    def _register(
        self, agent: Any, *, harness: str | None = None
    ) -> tuple[str, tuple[Cell, ...]]:
        try:
            key = registration_key(agent)
            cells = register_agents(self._runtime, (agent,), logger=self._logger)
            cells = tuple(self._seed_template(cell, agent) for cell in cells)
            cells = tuple(self._seed_policy(cell, agent, harness) for cell in cells)
        except CellError as error:
            self.failed(agent, error)
            if error.code == "stale-revision":
                return DEFERRED, ()
            self._handled[agent.cell_id] = (key, REFUSED)
            return REFUSED, ()
        except Exception as error:  # noqa: BLE001 - retried by the timer or the next trigger
            self.failed(agent, error)
            return DEFERRED, ()
        outcome = RECORDED if cells else SKIPPED
        self._handled[agent.cell_id] = (key, outcome)
        return outcome, cells

    def _after(self, *, pending: bool) -> None:
        if not pending:
            self._delay = RETRY_BASE_S
            return
        if self._closed or self._retry is not None:
            return
        delay = min(self._delay, RETRY_MAX_S)
        self._delay = min(delay * 2, RETRY_MAX_S)
        retry = threading.Timer(delay, self._retry_now)
        retry.daemon = True
        self._retry = retry
        retry.start()

    def _retry_now(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._retry = None
        self.reconcile()


def _launch_harness_of(app: Any) -> HarnessOf:
    facts = AppLocalMemberFacts(app)

    def harness_of(agent: Any, member: str) -> str | None:
        found = facts.facts_for(agent.agent_id, member)
        return None if found is None else found.harness

    return harness_of


def seed_launch_policy(
    app: Any, actor: str, harness: str, *, timeout_s: float = SEED_WAIT_S
) -> str:
    """Seed *actor*'s claude member rules before a launch; bounded, never raises."""

    if harness != SEED_HARNESS:
        return SKIPPED
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            from hyprial.daemon.impl.composition.cells.wiring import cell_runtime

            registrar = (
                getattr(app, "_agent_cell_registrar", None)
                if cell_runtime(app) is not None
                else None
            )
            if not isinstance(registrar, AgentCellRegistrar):
                result["state"] = "unavailable"
                return
            result["state"] = registrar.seed_for_launch(actor, harness)
        except Exception as error:  # noqa: BLE001 - a seed failure never fails the start
            result["state"] = "failed"
            result["errorType"] = type(error).__name__

    worker = threading.Thread(target=run, name="hyprial-cell-policy-seed", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout_s))
    state = str(result.get("state", "timeout"))
    if state not in {RECORDED, SKIPPED}:
        log = getattr(app, "_log", None)
        if callable(log):
            try:
                log("warn", "cells", SEED_FAILED_EVENT, actor=actor, harness=harness,
                    state=state, errorType=result.get("errorType"))
            except Exception:  # noqa: BLE001 - logging never breaks a start
                pass
    return state


def attach_agent_cells(app: Any, runtime: CellRuntimePort) -> AgentCellRegistrar | None:
    """Register the app's agents on *runtime* now and on every later change.

    Returns the registrar (``detach`` stops it), or None for an app without
    an agent directory; never raises.
    """

    agents = getattr(app, "agents", None)
    if agents is None or not callable(getattr(agents, "list", None)):
        return None
    registrar = AgentCellRegistrar(
        runtime, agents, getattr(app, "_log", None), harness_of=_launch_harness_of(app)
    )
    subscribe = getattr(agents, "on_mutation", None)
    try:
        if callable(subscribe):
            registrar.track(subscribe(registrar.reconcile))
        registrar.track(runtime.on_index(registrar.reconcile))
    except Exception as error:  # noqa: BLE001 - attaching never fails composition
        registrar.failed(None, error)
    registrar.reconcile()
    return registrar


__all__ = [
    "DEFERRED",
    "RECORDED",
    "REFUSED",
    "REGISTRATION_FAILED_EVENT",
    "SEED_FAILED_EVENT",
    "SEED_WAIT_S",
    "SKIPPED",
    "AgentCellRegistrar",
    "attach_agent_cells",
    "seed_launch_policy",
]
