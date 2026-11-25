# Decision record (user agent identity, UA1b plan A, 2026-11-18; rulings R20,
# R21; revised by UA4 after the owner decision of 2026-11-19):
# - Every machine the owner is logged in on holds its own user agent, and
#   each is an independent agent: the registry mints its id like any other
#   agent's, so the instances on different machines have different ids.
#   Merging them into one participant after an OrgFS link is future work.
#   What every instance shares is the owner's end ``user:<owner>``, which
#   conversations, group membership and envelopes key on, and the name and
#   role marker below.
# - Name ``hyprial-user`` on every machine.  ``user`` alone is a name people
#   already give their own agents; the owner is not part of the name because
#   owners may contain ``@``, which an actor name may not.
# - Marker: the record's capability fact ``template: user`` (packaged ``user``
#   template), whose cell params seed ``agent.role: user`` like squire's.  Code
#   never infers the role of a local record from its name alone.
# - Recognition.  This machine's instance is recognised by its registry
#   marker (``is_user_agent``; ``is_user_agent_target`` for a name or address
#   the local registry resolves).  ``is_user_agent_address`` decides from the
#   address text alone, on any node and without a registry, and recognises
#   the name form only: a four-segment agent URI whose last segment is the
#   reserved name.  An ordinary agent cannot take the name: it is refused at
#   every public creation and rename entrance.  An id-form address of another
#   machine's instance is an ordinary agent address until the merge step;
#   nothing looks another machine's instance up.
# - Known limits (UA1c review, documented, not fixed; registered as KP-106
#   and KP-107 in docs/reference/known-problems.md): the name-form test
#   matches ``agent:<any owner>:<any node>:hyprial-user`` whether or not such
#   a record exists anywhere; a match only hides the address from listings
#   and refuses work and administration, it grants nothing.  A role-carrying
#   message sent by an older peer that does not know the user agent is not
#   gated at this node's ingress; only this node's own dispatch and work
#   admission refuse it.
# - The reserved name is compared exactly, like the registry's reserved
#   internal names and the adapter collision check (``bootstrap.names``);
#   on a case-insensitive filesystem an agent named ``Hyprial-User`` collides
#   with the user agent's home instead (KP-109).
# - The name is refused at every public creation entrance (create, start
#   preflight, attach and the lifecycle start funnel all pass
#   ``require_agent_name_available``) and as a rename target.  It is NOT added
#   to ``RESERVED_AGENT_ACTOR_NAMES``: that set exempts daemon-internal actors
#   from link governance.  A pre-existing agent holding the name is never
#   adopted (it lacks the template fact); the ensure reports it.
# - A caller may not write the ``template: user`` fact through agent.create,
#   so the marker cannot be forged onto an ordinary agent.
# - Destroy and rename of the user agent are refused: it is the owner's
#   identity on this machine and the daemon recreates nothing it was told to
#   remove.
# - No session binds to the user agent (PR 2941, owner approval 2026-11-18):
#   ``session.register``/``register_guarded`` refuse it by name, name-form
#   URI (any machine's), and this machine's id-form URI and ``a.`` id with
#   USER_AGENT_IMMUTABLE, after the process-claim check and before anything is written.  Until the
#   ensure has created the record the id-form URI and the bare ``a.`` id are
#   refused earlier by the stale-address check with SESSION_SUPERSEDED (still
#   nothing written); the name and name-form URI keep USER_AGENT_IMMUTABLE.  Refresh,
#   heartbeat, turns and fenced message calls all need the session only
#   register persists.  A later slice opens exactly one path, for the
#   owner's client surfaces; nothing here anticipates it.
"""The user agent's name, marker and the refusals that protect them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from hyprial.daemon.impl.agent_templates import TEMPLATE_CAPABILITY, load_template
from hyprial.kernel import DaemonRequestError, ipc_errors, parse_agent_uri

USER_AGENT_NAME = "hyprial-user"
USER_AGENT_TEMPLATE = "user"

__all__ = [
    "USER_AGENT_NAME",
    "USER_AGENT_TEMPLATE",
    "is_user_agent",
    "is_user_agent_address",
    "is_user_agent_target",
    "refuse_user_agent_change",
    "refuse_user_agent_name",
    "refuse_user_agent_session",
    "refuse_user_agent_target",
    "refuse_user_agent_work",
    "refuse_user_template_capability",
    "user_agent_capabilities",
    "user_agent_work_message",
]


def _owner_key(owner: str) -> str:
    return owner.removeprefix("user:")


def is_user_agent(agent: Any) -> bool:
    """Whether *agent*'s record carries the ``user`` template fact."""

    facts = getattr(agent, "capabilities", None)
    return hasattr(facts, "get") and facts.get(TEMPLATE_CAPABILITY) == USER_AGENT_TEMPLATE


def is_user_agent_address(address: object, owner: str | None = None) -> bool:
    """Whether *address* is a user agent's name-form address (of *owner* when given)."""

    if not isinstance(address, str):
        return False
    parsed = parse_agent_uri(address)
    if parsed is None:
        return False
    address_owner, _machine, segment = parsed
    if owner is not None and address_owner != _owner_key(owner):
        return False
    return segment == USER_AGENT_NAME


def user_agent_capabilities() -> dict[str, Any]:
    """The capability facts a new user agent record is created with."""

    return dict(load_template(USER_AGENT_TEMPLATE).capabilities)


def refuse_user_agent_name(name: str) -> None:
    """Refuse a public creation, start or rename that names the user agent."""

    if name == USER_AGENT_NAME:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"the agent name {USER_AGENT_NAME!r} is reserved for the owner's user "
            "agent, which the daemon creates itself; choose another name",
        )


def refuse_user_agent_change(agent: Any, verb: str) -> None:
    """Refuse *verb* (destroy, rename, start, ...) on the owner's user agent."""

    if agent is not None and is_user_agent(agent):
        raise DaemonRequestError(
            ipc_errors.USER_AGENT_IMMUTABLE,
            f"cannot {verb} {getattr(agent, 'actor', USER_AGENT_NAME)!r}: it is the "
            "owner's user agent, which the daemon keeps as the owner's identity",
        )


def refuse_user_agent_session(agents: Any, actor: str) -> None:
    """Refuse binding an interactive session to *actor* when it is a user agent."""

    if is_user_agent_target(agents, actor):
        raise DaemonRequestError(
            ipc_errors.USER_AGENT_IMMUTABLE,
            f"cannot register a session as {actor!r}: it is the owner's user agent, "
            "which no harness or carrier session speaks for",
        )


def refuse_user_agent_target(agents: Any, name: str, verb: str) -> None:
    """Refuse *verb* when *name* resolves to the user agent; other lookups pass."""

    try:
        agent = agents.get(name)
    except Exception:  # noqa: BLE001 - the verb's own path reports a bad name
        return
    refuse_user_agent_change(agent, verb)


def is_user_agent_target(agents: Any, target: object) -> bool:
    """Whether *target* is a user agent: any machine's name form, or the local record."""

    if is_user_agent_address(target):
        return True
    if not isinstance(target, str) or not target or agents is None:
        return False
    try:
        return is_user_agent(agents.get(target))
    except Exception:  # noqa: BLE001 - the caller's own path reports a bad name
        return False


def user_agent_work_message(target: object) -> str:
    """The refusal text for work (routine, workflow node) aimed at a user agent."""

    return (
        f"{target!r} is the owner's user agent: it has no harness and never runs "
        "work; give the work to a model agent"
    )


def refuse_user_agent_work(
    agents: Any, target: object, *, error: Callable[[str, str], Exception] = DaemonRequestError
) -> None:
    """Raise USER_AGENT_IMMUTABLE (as *error*) when scheduled or workflow work names a user agent."""

    if is_user_agent_target(agents, target):
        raise error(ipc_errors.USER_AGENT_IMMUTABLE, user_agent_work_message(target))


def refuse_user_template_capability(capabilities: Any) -> None:
    """Refuse a caller-written ``template: user`` capability fact."""

    if (
        isinstance(capabilities, Mapping)
        and capabilities.get(TEMPLATE_CAPABILITY) == USER_AGENT_TEMPLATE
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"capability {TEMPLATE_CAPABILITY}={USER_AGENT_TEMPLATE!r} is reserved: "
            "only the daemon creates the owner's user agent",
        )
