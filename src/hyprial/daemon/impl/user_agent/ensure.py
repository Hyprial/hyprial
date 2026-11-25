# Decision record (user agent identity, UA1b plan A, 2026-11-18; UA4,
# 2026-11-19):
# - Every machine ensures its own user agent.  Nothing replicated is read or
#   written: placement needs no record because the answer is always "here".
#   A second machine of the same owner therefore creates its own instance, an
#   independent agent with the same name and marker and its own id (see
#   ``naming``).
# - Creation goes through the registry's ordinary create (the agent actor's
#   ``CreateAgentCommand``, the same one ``agent.create`` uses) with no
#   harness, no model vendor and no cwd: no harness spec is written and
#   nothing is started.  The registry mints the id as for any agent.  The
#   default routine is not bound (as for ``wf-`` workers): a user agent has
#   no model to run it.  The persona route is declared like every other
#   registered agent, so the record is addressable.
# - Any marked record of the owner is the user agent, whatever its id: a row
#   an older build created with an id derived from the owner answers
#   ``present`` and is never rewritten.
# - A name already held by an agent without the marker answers ``name-taken``
#   and is never adopted; so does a create the registry refuses because the
#   name is another agent's display name (AGENT_EXISTS).  Retrying cannot
#   change it; it is reported on the daemon status
#   (``runtime.user_agent_status``).  Any other failed create answers
#   ``deferred``; the caller retries.
# - No owner answers ``no-owner`` and creates nothing.
# - An owner change needs no step of its own: the general owner rewrite moves
#   the owner segment of the user agent's address like every agent's, and
#   the id stays.
# - A row lost together with its registry database, or removed by a forward
#   destroy (only an internal registry destroy can: every public destroy
#   refuses the user agent), is simply ensured again with a new id: a
#   destroyed agent's name is free (owner decision 2026-11-19).
"""Idempotent ensure of this machine's user agent over explicit ports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from hyprial.kernel import ipc_errors

from .naming import USER_AGENT_NAME, is_user_agent, user_agent_capabilities

__all__ = [
    "CREATED",
    "DEFERRED",
    "NAME_TAKEN",
    "NO_OWNER",
    "PRESENT",
    "ensure_user_agent",
    "local_user_agent",
]

CREATED = "created"
PRESENT = "present"
DEFERRED = "deferred"
NAME_TAKEN = "name-taken"
NO_OWNER = "no-owner"
_TERMINAL_CODES = {ipc_errors.AGENT_EXISTS: NAME_TAKEN}


def local_user_agent(agents: Iterable[Any], owner: str) -> Any | None:
    """The local record marked as *owner*'s user agent, or None."""

    key = owner.removeprefix("user:")
    return next(
        (agent for agent in agents if is_user_agent(agent) and agent.owner == key),
        None,
    )


def ensure_user_agent(
    *,
    agents: Any,
    declare_route: Callable[[str], None] | None = None,
    log: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Create or confirm the user agent of *agents*' owner on this machine.

    *should_stop* is asked immediately before the create; when it answers
    true no create is submitted and the answer is ``deferred``.
    """

    owner = str(getattr(agents, "owner", "") or "").removeprefix("user:")

    def emit(level: str, event: str, **fields: Any) -> None:
        if log is not None:
            try:
                log(level, event, owner=owner, **fields)
            except Exception:  # noqa: BLE001 - logging never breaks the ensure
                pass

    if not owner:
        emit("info", "user_agent.no_owner")
        return NO_OWNER
    if local_user_agent(agents.list(), owner) is not None:
        return PRESENT
    holder = agents.get(USER_AGENT_NAME)
    if holder is not None:
        emit("warn", "user_agent.name_taken", holder=getattr(holder, "uri", None))
        return NAME_TAKEN
    if should_stop is not None and should_stop():
        emit("info", "user_agent.create_skipped_stopping")
        return DEFERRED
    try:
        agent = agents.create(USER_AGENT_NAME, capabilities=user_agent_capabilities())
    except Exception as error:  # noqa: BLE001 - retried by the caller
        terminal = _TERMINAL_CODES.get(getattr(error, "code", None))
        if terminal is not None:
            emit("warn", "user_agent.name_taken", code=error.code)
            return terminal
        emit("warn", "user_agent.create_failed", errorType=type(error).__name__,
             detail=str(error)[:300])
        return DEFERRED
    if declare_route is not None:
        declare_route(agent.uri)
    emit("info", "user_agent.created", agent=agent.uri)
    return CREATED
