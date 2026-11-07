"""Runtime git-author resolution for agent tool profiles.

Decision block (brief agent-git-identity; review of 1b051f70 REQUIRED REWORK
R1-R5, fix round 2, 2026-11-04; R1 direction confirmed by Allen via Feishu
7ddc2c58, 2026-11-04):
- The former built-in person tool profile hard-coded one person as every
  agent's commit author; that constant and both personal literals are removed
  from src/ and a guard test keeps them out.
- Resolution order, mandated by the review:
  (a) an explicit per-agent setting: the pair ``gitAuthorName`` and
      ``gitAuthorEmail`` in the agent record's ``capabilities``; the pair is
      atomic -- a lone key is ignored rather than half-applied;
  (b) the agent's own identity -- the default: ``name = agent.actor``
      verbatim, ``email = agent-<sanitized actor>@agents.hyprial.com``.
      Sanitization lowercases the actor, maps every character outside
      ``[a-z0-9._-]`` to ``-``, collapses repeats, strips edge ``-.``, and
      falls back to ``agent`` when nothing survives.  This is the format
      Allen confirmed (Feishu 7ddc2c58): a human name only appears when the
      agent record opts in through the explicit pair in step (a), never
      through an inherited owner setting;
  (c) otherwise GitAuthorUnresolvedError with the stable code
      ``GIT_AUTHOR_UNRESOLVED`` and instructions for configuring the explicit
      pair.  There is no built-in person fallback and no owner fallback.
- Fix round 2 (review of 1b051f70, R1/R4): an agent without an explicit pair
  must never commit as the owner.  Reading the owner's global ``user.name``/
  ``user.email`` silently attributed every agent commit on a shared machine to
  that human, which is exactly the defect the review reproduced.  The owner
  reader is therefore gone: a blank actor is unresolvable and fails loudly
  instead of degrading to somebody else's identity.
- The profile id is content-derived (``agent-git-v1-<sha256[:16]>``) so an
  identity change materializes a fresh tool-profile root instead of tripping
  the write-or-verify check; homes recorded with the retired person profile
  are detected by directory shape (see ``retire_legacy_tool_profile``), re-
  resolved at the next start, reported once via one AgentRuntimeNotice with
  the legacy directory renamed aside, and git history is never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Any

from .errors import GitAuthorUnresolvedError
from hyprial.identity.impl.agents.runtime.notices import AgentRuntimeNotice
from .profiles import AgentToolProfile, SshToolAuthorization, derived_profile_id

if TYPE_CHECKING:
    from hyprial.identity.impl.agents.registry._base import Agent

__all__ = [
    "AgentEmailDomain",
    "EXPLICIT_GIT_AUTHOR_KEYS",
    "GitAuthor",
    "GitAuthorUnresolvedError",
    "SOURCE_AGENT",
    "SOURCE_EXPLICIT",
    "resolve_agent_tool_profile",
]

AgentEmailDomain = "agents.hyprial.com"
EXPLICIT_GIT_AUTHOR_KEYS = ("gitAuthorName", "gitAuthorEmail")
SOURCE_EXPLICIT = "explicit"
SOURCE_AGENT = "agent"

_MIGRATED_NOTICE_CODE = "AGENT_GIT_PROFILE_MIGRATED"
_DERIVED_PROFILE_PREFIX = "agent-git-v1-"


@dataclass(frozen=True, slots=True)
class GitAuthor:
    """One resolved git author plus the resolution source that produced it."""

    name: str
    email: str
    source: str


def _email_local_part(actor: str) -> str:
    sanitized = actor.lower()
    sanitized = "".join(
        character if character in "abcdefghijklmnopqrstuvwxyz0123456789._-"
        else "-"
        for character in sanitized
    )
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    sanitized = sanitized.strip("-.")
    return sanitized or "agent"


def agent_git_identity(actor: str) -> GitAuthor:
    """The final fallback: the agent commits under its own identity."""

    if not actor or not actor.strip():
        raise GitAuthorUnresolvedError(
            "agent has no resolvable git author: actor is blank; set the "
            "agent's actor or an explicit gitAuthorName/gitAuthorEmail pair "
            "on the agent record (capabilities); the owner's git identity is "
            "never used",
            code="GIT_AUTHOR_UNRESOLVED",
        )
    return GitAuthor(
        name=actor,
        email=f"agent-{_email_local_part(actor)}@{AgentEmailDomain}",
        source=SOURCE_AGENT,
    )


def resolve_git_author(
    *,
    actor: str,
    capabilities: dict[str, Any] | None = None,
) -> GitAuthor:
    """Resolve one git author under the mandated order; see module decision."""

    if capabilities is not None:
        explicit_name = capabilities.get(EXPLICIT_GIT_AUTHOR_KEYS[0])
        explicit_email = capabilities.get(EXPLICIT_GIT_AUTHOR_KEYS[1])
        if (
            isinstance(explicit_name, str)
            and explicit_name.strip()
            and isinstance(explicit_email, str)
            and explicit_email.strip()
        ):
            return GitAuthor(
                name=explicit_name,
                email=explicit_email,
                source=SOURCE_EXPLICIT,
            )
    return agent_git_identity(actor)


def resolve_agent_tool_profile(
    agent: Agent,
    *,
    ssh: SshToolAuthorization | None = None,
) -> AgentToolProfile:
    """Build the launch tool profile for one agent record at runtime."""

    author = resolve_git_author(
        actor=agent.actor,
        capabilities=dict(agent.capabilities),
    )
    return AgentToolProfile(
        profile_id=derived_profile_id(author.name, author.email),
        git_author_name=author.name,
        git_author_email=author.email,
        ssh=ssh,
    )


def retire_legacy_tool_profile(
    *,
    config_root: Path,
    actor: str,
    resolved_profile_id: str,
) -> AgentRuntimeNotice | None:
    """Report and set aside every legacy tool profile under this config root.

    The scan covers every recorded config revision of this entity token, not
    only the current one: a home can hold the retired profile under a previous
    revision, and a normal config edit between launches changes the revision
    without touching the old directory (review of f98c63e248, R2).  Any tools
    directory that is neither the resolved profile, a content-derived
    profile, nor an already-migrated ``.migrated`` tombstone predates runtime
    resolution.  When a tombstone already exists — an older binary re-
    materialized the retired profile after a rollback — the stale tombstone
    is replaced and the source is genuinely set aside, so migration stays
    idempotent and the notice is emitted exactly once per real migration
    (review of f98c63e248, R3).  Returns the single migration notice, or
    None when there is nothing to migrate.
    """

    if not config_root.is_dir():
        return None
    migrated: list[str] = []
    first_set_aside: Path | None = None
    for revision in sorted(config_root.iterdir()):
        tools_root = revision / "tools"
        if not tools_root.is_dir():
            continue
        for child in sorted(tools_root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name == resolved_profile_id:
                continue
            if child.name.startswith(_DERIVED_PROFILE_PREFIX):
                continue
            if child.name.endswith(".migrated"):
                continue
            set_aside = child.with_name(child.name + ".migrated")
            try:
                if set_aside.is_symlink() or (
                    set_aside.exists() and not set_aside.is_dir()
 ):
                    set_aside.unlink()
                elif set_aside.is_dir():
                    shutil.rmtree(set_aside)
                child.rename(set_aside)
            except FileNotFoundError:
                continue
            migrated.append(f"{revision.name}/{child.name}")
            if first_set_aside is None:
                first_set_aside = set_aside
    if not migrated or first_set_aside is None:
        return None
    listed = ", ".join(migrated[:4]) + (" …" if len(migrated) > 4 else "")
    return AgentRuntimeNotice(
        code=_MIGRATED_NOTICE_CODE,
        actor=actor,
        message=(
            f"tool profile(s) {listed} predate runtime git-identity "
            f"resolution and were re-resolved to {resolved_profile_id!r}; "
            "each legacy directory was set aside as *.migrated "
            "(git history untouched)"
        ),
        path=str(first_set_aside),
    )
