"""Who calls a group method, and which member a member reference names (slice GC2).

Decision notes (group chat design draft §2.4, §3; owner decisions §10;
brief GC2):

- The caller is the conversation reads' caller (``_history_caller``): the
  owner as ``user:<owner>``, an agent by its session, or with
  ``onBehalfOf`` the agent the owner acts for.  Its member id is
  ``member_id_for`` of that subject after this machine's user agent, in any
  spelling, is read as the owner by its registry marker (``member_id_of``,
  UA4); a subject with no stable member id (a name-form agent, an adapter)
  is no member of any group.
- A member reference resolves in this order: a ``user:`` URI (this owner's
  is the owner, another owner's is ``UNSUPPORTED_TARGET``); an agent
  address of another owner (``UNSUPPORTED_TARGET``, before any registry
  read); any machine's name-form user agent address (the owner); then the
  local registry (name, display name, ``a.<id>`` or this machine's address),
  where no agent is ``AGENT_NOT_FOUND`` and the user agent record is the
  owner (UA4: each machine's user agent has its own id, so another machine's
  id form is ``AGENT_NOT_FOUND`` like any agent of another machine);
  only then ``member_id_for`` of the registered address, where a name-form
  address is ``INVALID_ARGUMENT``.  A bare ``a.<id>`` therefore reaches the
  user agent only through the registry record, never by id comparison.
- The user agent named as a member is the owner (``user:<owner>``), who is
  always a member: there is no second member for it.  ``create`` refuses it
  as a duplicate, ``members.add`` answers it as a no-op.  That is the
  ordinary duplicate and no-op rule, with no special case.
- Single machine, single owner this round: an agent of this owner on
  another machine is not in the local registry and is ``AGENT_NOT_FOUND``.
- ``displayName`` is a snapshot at add time: the registry's display name,
  else the agent's name; the owner's is the owner key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.conversations.groups import MEMBER_AGENT, MEMBER_USER, member_id_for
from hyprial.daemon.impl.user_agent.local_target import canonical_user_end
from hyprial.daemon.impl.user_agent.naming import is_user_agent, is_user_agent_address
from hyprial.kernel import (
    DaemonRequestError,
    canonical_user_uri,
    ipc_errors,
    parse_agent_uri,
    parse_user_uri,
)


@dataclass(frozen=True)
class Candidate:
    """A resolved member reference, before it becomes a member record."""

    member_id: str
    kind: str
    display_name: str
    address: str | None = None


def owner_member_id(app: Any) -> str:
    return canonical_user_uri(str(app.owner))


def owner_candidate(app: Any) -> Candidate:
    return Candidate(owner_member_id(app), MEMBER_USER, str(app.owner))


def caller_member_id(app: Any, method: str, params: dict[str, Any],
                     trusted_origin: str | None) -> str | None:
    """The caller's member id, or None when the caller has none."""

    return member_id_of(app, str(app._history_caller(method, params, trusted_origin).subject))


def member_id_of(app: Any, address: str) -> str | None:
    """The member id of *address* on this machine, or None when it has none."""

    try:
        return member_id_for(canonical_user_end(address, app.agents))[0]
    except ValueError:
        return None


def _refused(code: str, message: str, reference: str | None = None) -> DaemonRequestError:
    data = None if reference is None else {"member": reference}
    return DaemonRequestError(code, message, data)


def resolve_member(app: Any, reference: str) -> Candidate:
    """The member *reference* names; refuses what cannot be a member."""

    owner = owner_member_id(app)
    if parse_user_uri(reference) is not None:
        if reference == owner:
            return owner_candidate(app)
        raise _refused(ipc_errors.UNSUPPORTED_TARGET,
                       "a member of another owner cannot join this round", reference)
    parsed = parse_agent_uri(reference)
    if parsed is not None and canonical_user_uri(parsed[0]) != owner:
        raise _refused(ipc_errors.UNSUPPORTED_TARGET,
                       "a member of another owner cannot join this round", reference)
    if is_user_agent_address(reference):
        return owner_candidate(app)
    try:
        agent = app.agents.get(reference)
    except Exception:  # noqa: BLE001 - an unparseable reference names no agent
        agent = None
    if agent is None:
        raise _refused(ipc_errors.AGENT_NOT_FOUND,
                       f"no agent {reference!r} of this owner on this machine", reference)
    if is_user_agent(agent):
        return owner_candidate(app)
    try:
        member_id, kind = member_id_for(str(agent.uri))
    except ValueError:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"{reference!r} has a name-form address; a group member needs an id-form agent",
        ) from None
    if kind != MEMBER_AGENT:
        return owner_candidate(app)
    return Candidate(member_id, kind, str(agent.display_name or agent.actor), str(agent.uri))


__all__ = [
    "Candidate",
    "caller_member_id",
    "member_id_of",
    "owner_candidate",
    "owner_member_id",
    "resolve_member",
]
