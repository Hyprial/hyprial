"""Tool profiles and runtime git-author resolution for agent runtimes.

Owns the profile value types, the content-derived profile id, the mandated
git-author resolution order, and the one-shot legacy profile migration
notice.  ``runtime/context.py`` consumes this package; cross-domain callers
go through ``hyprial.identity``.
"""

from __future__ import annotations

from .errors import AgentRuntimeError, GitAuthorUnresolvedError
from .identity import (
    AgentEmailDomain,
    EXPLICIT_GIT_AUTHOR_KEYS,
    GitAuthor,
    SOURCE_AGENT,
    SOURCE_EXPLICIT,
    resolve_agent_tool_profile,
    resolve_git_author,
    retire_legacy_tool_profile,
)
from .profiles import AgentToolProfile, SshToolAuthorization, derived_profile_id

__all__ = [
    "AgentEmailDomain",
    "AgentRuntimeError",
    "AgentToolProfile",
    "EXPLICIT_GIT_AUTHOR_KEYS",
    "GitAuthor",
    "GitAuthorUnresolvedError",
    "SOURCE_AGENT",
    "SOURCE_EXPLICIT",
    "SshToolAuthorization",
    "derived_profile_id",
    "resolve_agent_tool_profile",
    "resolve_git_author",
    "retire_legacy_tool_profile",
]
