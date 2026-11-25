"""Agents as cells: one ``agent`` cell per local agent (design §2, S3).

Decision notes (phase A, S3):

- The agent cell is the subject cell of the agent's ``a.`` id, so its id is
  exactly ``Agent.cell_id`` (identity owns the derivation; this module checks
  it and refuses a drift rather than minting a second id).
- Today an agent is a single harness: the cell has one member, the agent
  URI, with role ``harness``.
- Scope is every live org directory space, like the agent's send-link file.
- Only agents whose owner is the runtime's writer are registered: phase A
  authors cells as the local owner only (plan Q10).  An agent's owner is a
  username, so it is compared with ``identity.writer`` (``user:<username>``),
  never with the uid authority (contract OC0 §1.6).  Each skipped agent is
  logged (``cell.agent_skipped``) when a logger is given, so a drift between
  the agent's owner form and the OrgFS author form is visible.
- Agents are read duck-typed (``agent_id``, ``cell_id``, ``uri``, ``actor``,
  ``owner``) so the daemon does not import the identity implementation.

Decision notes (phase B, S8 attach):

- An agent attached with ``hyprial agent attach`` carries two facts in its
  local record's ``capabilities``: ``custody: "external"`` (fixed for the
  incarnation) and ``attached`` (flipped by attach and detach).  The record
  is local and OrgFS-independent, so the cell is always re-derivable from it:
  a cell write missed while OrgFS was down is reconciled from the record.
- An external agent's cell has the ``agent.custody: "external"`` param and,
  while attached, its one ``harness`` member; detach removes the member (a
  new revision, not a tombstone), so identity, ids and history stay.
- ``register_agents`` reconciles an external agent's cell to the record:
  only the membership and the ``agent.custody`` key are rewritten, every
  other param an owner wrote is kept.  A daemon-managed agent keeps the
  phase A ensure-only path, byte for byte.
- ``registration_key`` folds the attachment state into the registrar's
  per-cell key, so an attach or detach is reconciled by the next trigger
  even when it happened while no runtime was composed; ``cell_matches`` lets
  the registrar notice a cell that drifted from the record after that.
- custody=external is enforced, not only described (S8 review):
  ``refuse_external_launch`` is called by every daemon path that would
  launch or bind a daemon-supervised harness (harness-bound ``agent.create``,
  ``agent.start.preflight``, the ``lifecycle.start`` funnel), and
  ``refuse_reserved_capabilities`` keeps ``agent.create`` from writing the
  ``custody`` fact, so only ``agent.attach`` creates an external agent.
  Both refuse with the existing ``INVALID_ARGUMENT`` code.
- The attach handler and the registrar's mutation listener can reconcile the
  same cell at once; the update is revision-checked and re-read up to
  ``RECONCILE_ATTEMPTS`` times, so neither overwrites the other's revision.

Decision notes (UA1b, 2026-11-18; UA4, 2026-11-19):

- Each machine's user agent is an independent agent with its own ``a.`` id,
  hence its own agent cell; merging the instances after an OrgFS link is
  future work.  Its cell spec has no member because no harness speaks for
  the owner.  Its members (the owner's client surfaces) are a later slice.
  The scope is the org's, like every agent cell.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellError, CellMember, CellScope
from hyprial.daemon.impl.cells.ports import CellRuntimePort, CellSpec
from hyprial.kernel import DaemonRequestError, canonical_user_uri, ipc_errors

CUSTODY_CAPABILITY = "custody"
ATTACHED_CAPABILITY = "attached"
CUSTODY_EXTERNAL = "external"
CUSTODY_DAEMON = "daemon"
CUSTODY_PARAM = "agent.custody"
HARNESS_ROLE = "harness"
RECONCILE_ATTEMPTS = 3


def _capabilities(agent: Any) -> Any:
    facts = getattr(agent, "capabilities", None)
    return facts if hasattr(facts, "get") else {}


def agent_custody(agent: Any) -> str:
    """``external`` for an attached-harness agent, else ``daemon``."""

    if _capabilities(agent).get(CUSTODY_CAPABILITY) == CUSTODY_EXTERNAL:
        return CUSTODY_EXTERNAL
    return CUSTODY_DAEMON


def is_attached(agent: Any) -> bool:
    """Whether an external-custody agent currently has its harness attached."""

    return (
        agent_custody(agent) == CUSTODY_EXTERNAL
        and _capabilities(agent).get(ATTACHED_CAPABILITY) is True
    )


def registration_key(agent: Any) -> str:
    """The registrar's per-cell state key: the cell id plus attachment state."""

    if agent_custody(agent) != CUSTODY_EXTERNAL:
        return str(agent.cell_id)
    state = "attached" if is_attached(agent) else "detached"
    return f"{agent.cell_id}:{CUSTODY_EXTERNAL}:{state}"


def agent_cell_spec(agent: Any) -> CellSpec:
    """The ``agent`` cell spec of one agent record."""

    from hyprial.daemon.impl.user_agent.naming import is_user_agent

    external = agent_custody(agent) == CUSTODY_EXTERNAL
    members = (
        ()
        if (external and not is_attached(agent)) or is_user_agent(agent)
        else (CellMember(agent.uri, HARNESS_ROLE),)
    )
    return CellSpec(
        kind="agent",
        binding=CellBinding.subject(agent.agent_id),
        scope=CellScope.of_org(),
        name=agent.actor,
        members=members,
        params={CUSTODY_PARAM: CUSTODY_EXTERNAL} if external else {},
    )


def cell_matches(cell: Cell | None, agent: Any) -> bool:
    """Whether a live, unretired *cell* has the membership and custody the record implies."""

    if cell is None or cell.retired:
        return False
    if cell.members != tuple(sorted(agent_cell_spec(agent).members)):
        return False
    external = agent_custody(agent) == CUSTODY_EXTERNAL
    return not external or cell.param(CUSTODY_PARAM) == CUSTODY_EXTERNAL


def external_member_matches(cell: Cell | None, agent: Any) -> bool:
    """Whether an external agent's own attachment member and custody match *agent*."""

    if cell is None or cell.retired:
        return False
    if agent_custody(agent) != CUSTODY_EXTERNAL:
        return True
    return (
        cell.has_member(agent.uri) == is_attached(agent)
        and cell.param(CUSTODY_PARAM) == CUSTODY_EXTERNAL
    )


def refuse_external_launch(agent: Any) -> None:
    """Refuse a daemon launch or harness binding for an external-custody agent."""

    if agent is None or agent_custody(agent) != CUSTODY_EXTERNAL:
        return
    raise DaemonRequestError(
        ipc_errors.INVALID_ARGUMENT,
        f"agent {agent.actor!r} is attached with custody=external: its harness runs "
        "outside the daemon, which never starts, restores or supervises it. Run the "
        "harness yourself, or 'hyprial agent detach' and 'hyprial agent destroy' it "
        "before starting a daemon-managed agent under this name.",
    )


def refuse_foreign_owner_launch(agent: Any, owner: str) -> None:
    """Refuse a daemon launch or harness binding for a foreign-owner agent."""

    if agent is None or canonical_user_uri(agent.owner) == canonical_user_uri(owner):
        return
    raise DaemonRequestError(
        ipc_errors.INVALID_ARGUMENT,
        f"agent {agent.actor!r} belongs to foreign owner {agent.owner!r}; "
        "use transfer receive before starting it on this daemon",
    )


def refuse_reserved_capabilities(capabilities: Any) -> None:
    """Refuse a caller-written ``custody`` capability (only ``agent.attach`` writes it).

    The ``template: user`` fact is refused the same way: only the daemon's
    user agent ensure writes it (UA1b).
    """

    from hyprial.daemon.impl.user_agent.naming import refuse_user_template_capability

    refuse_user_template_capability(capabilities)
    if hasattr(capabilities, "get") and CUSTODY_CAPABILITY in capabilities:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"capability {CUSTODY_CAPABILITY!r} is reserved: an external-custody agent "
            "is registered with 'hyprial agent attach', never by agent create",
        )


def refuse_custody_launch(
    agent: Any,
    method: str,
    params: Mapping[str, Any],
    *,
    owner: str | None = None,
) -> None:
    """The S8 custody guards of ``agent.create`` and ``agent.start.preflight``.

    *agent* is the record already registered under the requested name, or
    None.  A new agent may not carry the reserved ``custody`` capability; an
    existing external-custody agent is never preflighted for a launch nor
    reused by a harness-bound create (``hyprial agent start``'s first call).
    An existing agent created without ``existing=reuse`` is left to the
    create's ``AGENT_EXISTS`` refusal.
    """

    if agent is None:
        if method == "agent.create":
            refuse_reserved_capabilities(params.get("capabilities"))
        return
    if owner is not None:
        refuse_foreign_owner_launch(agent, owner)
    harness = params.get("harness")
    bound = isinstance(harness, str) and bool(harness)
    if method == "agent.start.preflight" or (
        method == "agent.create" and params.get("existing") == "reuse" and bound
    ):
        refuse_external_launch(agent)


def _reconcile_external(runtime: CellRuntimePort, agent: Any) -> Cell:
    spec = agent_cell_spec(agent)
    attached = is_attached(agent)
    for attempt in range(RECONCILE_ATTEMPTS):
        cell = runtime.ensure(spec)
        other_members = tuple(m for m in cell.members if m.ref != agent.uri)
        target_members = (
            tuple(sorted((*other_members, CellMember(agent.uri, HARNESS_ROLE))))
            if attached
            else other_members
        )
        custody_ok = cell.param(CUSTODY_PARAM) == CUSTODY_EXTERNAL
        if cell.members == target_members and custody_ok:
            return cell
        try:
            return runtime.update(
                cell.cell_id,
                members=target_members,
                params={**cell.params, CUSTODY_PARAM: CUSTODY_EXTERNAL},
                expect_revision=cell.revision,
            )
        except CellError as error:
            if error.code != "stale-revision" or attempt + 1 == RECONCILE_ATTEMPTS:
                raise
    raise CellError("stale-revision", f"{spec.binding.value} kept changing")


def register_agents(
    runtime: CellRuntimePort,
    agents: Iterable[Any],
    *,
    logger: Callable[..., None] | None = None,
) -> tuple[Cell, ...]:
    """Ensure the agent cell of every agent the runtime's writer owns."""

    writer = runtime.identity.writer
    registered: list[Cell] = []
    for agent in agents:
        if canonical_user_uri(agent.owner) != writer:
            if logger is not None:
                try:
                    logger("info", "cells", "cell.agent_skipped", agent=agent.uri,
                           reason="owner-mismatch")
                except Exception:  # noqa: BLE001 - logging never breaks registration
                    pass
            continue
        if agent_custody(agent) == CUSTODY_EXTERNAL:
            cell = _reconcile_external(runtime, agent)
        else:
            cell = runtime.ensure(agent_cell_spec(agent))
        if cell.cell_id != agent.cell_id:
            raise CellError("id-mismatch", f"agent {agent.uri} cell id drifted")
        registered.append(cell)
    return tuple(registered)


__all__ = [
    "ATTACHED_CAPABILITY",
    "CUSTODY_CAPABILITY",
    "CUSTODY_DAEMON",
    "CUSTODY_EXTERNAL",
    "CUSTODY_PARAM",
    "HARNESS_ROLE",
    "agent_cell_spec",
    "agent_custody",
    "cell_matches",
    "external_member_matches",
    "is_attached",
    "refuse_custody_launch",
    "refuse_external_launch",
    "refuse_foreign_owner_launch",
    "refuse_reserved_capabilities",
    "register_agents",
    "registration_key",
]
