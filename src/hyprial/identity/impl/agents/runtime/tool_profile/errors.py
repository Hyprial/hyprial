"""Errors for agent tool profiles and their git-author resolution.

AgentRuntimeError moved here from runtime/context.py (decision
agent-git-identity 2026-11-03): the profile value types and the resolver both
need it, and context.py consumes this package, so the error lives below the
consumer to keep the import graph acyclic.
"""

from __future__ import annotations

__all__ = ["AgentRuntimeError", "GitAuthorUnresolvedError"]


class AgentRuntimeError(ValueError):
    """A P2 root/profile combination cannot be represented safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        data: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class GitAuthorUnresolvedError(AgentRuntimeError):
    """No git author could be resolved for an agent under the mandated order.

    Raised only when even the agent's own identity cannot produce a name,
    which means the agent record itself carried no usable actor.  The product
    never falls back to a built-in person.
    """

    code = "GIT_AUTHOR_UNRESOLVED"
