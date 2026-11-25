# Decision record (user agent identity, UA1b plan A, 2026-11-18):
# - A daemon resolves its own owner's user agent to its own local instance,
#   so the lookup reads only this node's agent directory: no replicated
#   record, no peer, no other owner.  Another machine's instance is never
#   looked up; only its name-form address is recognised, by
#   ``naming.is_user_agent_address`` (UA4).
"""Answer "the local user agent address of this node's owner" from the registry."""

from __future__ import annotations

from typing import Any

from .ensure import local_user_agent

__all__ = ["local_user_agent_address"]


def local_user_agent_address(agents: Any) -> str | None:
    """The address of this node's owner's user agent, or None when there is none."""

    owner = str(getattr(agents, "owner", "") or "")
    if not owner:
        return None
    agent = local_user_agent(agents.list(), owner)
    return None if agent is None else str(agent.uri)
