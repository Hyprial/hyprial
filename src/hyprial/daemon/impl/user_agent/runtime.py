# Decision record (user agent identity, UA1b plan A, 2026-11-18; UA1c):
# - Triggers.  The daemon is the only writer of the agent registry, and its
#   owner is fixed when the application is constructed.  ``hyprial daemon
#   run`` is one same-PID loop: a home with no owner runs the bootstrap host
#   (no application, so never this code); a login, from the CLI or from the
#   GUI (which is only a client of the daemon since #2933), ends bootstrap
#   and the loop constructs a new application for that owner; a logout stops
#   the application and returns the loop to bootstrap (#2927).  So "at
#   login", "at onboarding" and "at upgrade" are the same hook: every
#   application start, after ``daemon.ready``.  The startup window itself
#   submits no agent operation.
# - The ensure runs on its own daemon thread, never on the restore thread or
#   a request thread.  A ``deferred`` (failed create) is retried with doubling
#   delays, at most ``MAX_ATTEMPTS`` times per trigger; the loop ends right
#   after the last attempt (no back-off follows it) and leaves on daemon stop.
#   After the last failed attempt there is no user agent until the next
#   daemon start.
# - Teardown (UA1g): the thread is a daemon thread and is never joined.  The
#   application's ``stop_event`` is set by every stop, logout included (a
#   logged-out daemon stays in the process and returns to bootstrap, #2927).
#   The ensure re-checks it immediately before it submits the create, so no
#   create is submitted once the stop is visible.  A create submitted just
#   before the stop goes to the agent actor, the registry's single writer:
#   the actor commits it before it stops or the submit is refused
#   (``PORT_*`` admission), and the registry is closed only after the actor,
#   so no commit lands in a closed registry.  Such a create can still commit
#   during the drain; logout removes identity files only, so the record is
#   the same one the start-time ensure made for that owner.
# - One loop per application: a trigger that arrives while a loop runs makes
#   it run once more instead of starting a second thread.
# - Visibility (UA1c item 4): the loop keeps its last outcome and attempt
#   count, and ``user_agent_status`` projects them, with the live registry
#   read, into the ``userAgent`` block of ``ps`` (the daemon status that
#   ``hyprial doctor`` already reads): ``present``, ``pending``,
#   ``no-owner``, ``name-taken`` or ``unavailable``
#   (attempts exhausted).  Any marked record of the owner is ``present``,
#   whatever its id (UA4).
# - UA2: an ensure that found or created the user agent binds user:<owner>
#   to this node's local delivery (``local_target``).
# - Listings (UA1f, 2026-11-18): agent listings and counts hide the user agent
#   by default (``agent.list``, ``ps.agents``, the whole-node
#   ``agent.home-census``, ``top.snapshot``, ``targets``), so merging it
#   changes nothing a user sees.  It stays visible through the ``userAgent``
#   block of ``ps``, ``agent.list`` with ``includeUserAgent: true``, and
#   explicit lookups by name (``agent.get``, a census ``actor`` selector).
#   An actor status row is hidden when it is any machine's name-form address
#   or this machine's marked record (UA4); another machine's id form is an
#   ordinary agent's.
"""Run the user agent ensure for a daemon application and report its state."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

from .ensure import (
    CREATED,
    DEFERRED,
    NAME_TAKEN,
    NO_OWNER,
    PRESENT,
    ensure_user_agent,
    local_user_agent,
)
from .local_target import bind_local_user_target
from .naming import USER_AGENT_NAME, is_user_agent, is_user_agent_address

__all__ = [
    "MAX_ATTEMPTS",
    "census_agents",
    "listed_agents",
    "run_user_agent_ensure",
    "schedule_user_agent_ensure",
    "user_agent_status",
    "without_user_agents",
]

MAX_ATTEMPTS = 6
RETRY_BASE_S = 2.0
_ATTRIBUTE = "_user_agent_ensure_state"
_STATE_LOCK = threading.Lock()
_UNAVAILABLE = "unavailable"


def run_user_agent_ensure(app: Any) -> str:
    """One ensure attempt for *app*'s owner on this node."""

    declare = getattr(app, "_declare_persona_route", None)
    stop = getattr(app, "stop_event", None)
    return ensure_user_agent(
        agents=app.agents,
        declare_route=declare if callable(declare) else None,
        log=lambda level, event, **fields: app._log(level, "agents", event, **fields),
        should_stop=stop.is_set if stop is not None else None,
    )


def _record(state: dict[str, Any], **fields: Any) -> None:
    with _STATE_LOCK:
        state.update(fields)


def _loop(app: Any, state: dict[str, Any]) -> None:
    stop = getattr(app, "stop_event", None)
    while True:
        with _STATE_LOCK:
            state["again"] = False
        for attempt in range(MAX_ATTEMPTS):
            if stop is not None and stop.is_set():
                break
            try:
                outcome = run_user_agent_ensure(app)
                detail = None
            except Exception as error:  # noqa: BLE001 - the daemon outlives the ensure
                app._log("warn", "agents", "user_agent.ensure_failed",
                         errorType=type(error).__name__)
                outcome, detail = DEFERRED, type(error).__name__
            exhausted = outcome == DEFERRED and attempt + 1 == MAX_ATTEMPTS
            _record(state, outcome=_UNAVAILABLE if exhausted else outcome,
                    attempts=attempt + 1, detail=detail)
            if outcome in (CREATED, PRESENT):
                bind_local_user_target(app)
            if outcome != DEFERRED or exhausted:
                break
            if stop is not None and stop.wait(RETRY_BASE_S * (2 ** attempt)):
                break
        with _STATE_LOCK:
            if not state["again"] or (stop is not None and stop.is_set()):
                state["thread"] = None
                return


def schedule_user_agent_ensure(app: Any, reason: str) -> None:
    """Run the ensure for *app* off the caller's thread; coalesces triggers."""

    with _STATE_LOCK:
        state = getattr(app, _ATTRIBUTE, None)
        if state is None:
            state = {"thread": None, "again": False}
            setattr(app, _ATTRIBUTE, state)
        if state["thread"] is not None:
            state["again"] = True
            return
        thread = threading.Thread(
            target=_loop, args=(app, state),
            name=f"hyprial-user-agent-ensure-{reason}", daemon=True,
        )
        state["thread"] = thread
    thread.start()


_REASONS = {
    NAME_TAKEN: (
        f"the name {USER_AGENT_NAME!r} is held by an ordinary agent on this machine, "
        "so the owner's user agent cannot be created"
    ),
    _UNAVAILABLE: (
        "every create attempt failed; the daemon tries again at its next start"
    ),
}


def user_agent_status(app: Any, records: Iterable[Any] | None = None) -> dict[str, Any]:
    """The ``userAgent`` block of the daemon status for *app*.

    *records* is the ``agents.list()`` snapshot the caller already read, if
    any, so ``ps`` reads the registry projection once.
    """

    with _STATE_LOCK:
        state = dict(getattr(app, _ATTRIBUTE, None) or {})
    outcome, attempts = state.get("outcome"), int(state.get("attempts") or 0)
    agents = getattr(app, "agents", None)
    owner = str(getattr(agents, "owner", "") or "")
    agent = None
    if owner:
        try:
            agent = local_user_agent(agents.list() if records is None else records, owner)
        except Exception:  # noqa: BLE001 - the status read never fails the status
            agent = None
    if agent is not None:
        return {"state": "present", "address": agent.uri, "attempts": attempts}
    if not owner or outcome == NO_OWNER:
        return {"state": NO_OWNER, "address": None, "attempts": attempts}
    if outcome in _REASONS:
        reason = _REASONS[outcome]
        if state.get("detail"):
            reason = f"{reason} (last error: {state['detail']})"
        return {"state": outcome, "address": None, "attempts": attempts, "reason": reason}
    return {"state": "pending", "address": None, "attempts": attempts}


def without_user_agents(actor_statuses: list[Any], records: Iterable[Any]) -> list[Any]:
    """*actor_statuses* without a user agent: it is never observed running.

    *records* is this machine's ``agents.list()``; its marked record is
    hidden by address, and any machine's name form by its text.
    """

    local = {str(agent.uri) for agent in records if is_user_agent(agent)}
    return [
        status for status in actor_statuses
        if not (str(status.get("actor", "")) in local
                or is_user_agent_address(str(status.get("actor", ""))))
    ]


def listed_agents(agents: list[Any]) -> list[Any]:
    """The agent records an agent listing or count shows: the user agent is hidden."""

    return [agent for agent in agents if not is_user_agent(agent)]


def census_agents(agents: list[Any], selector: object) -> list[Any]:
    """The records ``agent.home-census`` measures for its ``actor`` *selector*.

    The whole-node census (no selector) is a count and hides the user agent;
    a selector is an explicit lookup that the catalog binding narrows to the
    one named agent afterwards, so every record stays in.
    """

    return list(agents) if selector is not None else listed_agents(agents)
