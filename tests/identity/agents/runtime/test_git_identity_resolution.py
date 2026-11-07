"""Red-first tests for runtime git-author resolution (brief agent-git-identity).

Fix round 2 (review of 1b051f70, REQUIRED REWORK R1/R3/R4):

- R1: without an explicit per-agent pair the agent commits under its own
  identity.  The owner's global git config is never adopted, even when it is
  present, valid and reachable through HOME/GIT_CONFIG_GLOBAL.
- R3: the two tests that pinned the old owner-fallback behaviour
  (``test_owner_gitconfig_used_when_profile_fields_absent`` and the
  human-name expectation in ``test_git_identity_migration.py``) are rewritten
  in this same change instead of being bypassed by the new logic.
- R4: the assertions distinguish "fallback to the owner" from "unresolved".
  A blank actor is unresolvable, fails with the stable
  ``GIT_AUTHOR_UNRESOLVED`` code, and tells the operator to configure the
  explicit pair; a resolvable agent is always attributed to the agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.identity import GitAuthorUnresolvedError
from hyprial.identity import resolve_agent_tool_profile
from hyprial.identity.impl.agents.runtime.tool_profile import (
    SOURCE_AGENT,
    SOURCE_EXPLICIT,
    AgentToolProfile,
    resolve_git_author,
)

AGENT_NAME = "worker"
AGENT_EMAIL = "agent-worker@agents.hyprial.com"


class _FakeAgent:
    def __init__(
        self,
        actor: str = "worker",
        capabilities: dict[str, object] | None = None,
    ) -> None:
        self.actor = actor
        self.capabilities = {} if capabilities is None else capabilities


def _owner_home_with_gitconfig(
    tmp_path: Path, name: str, email: str
) -> Path:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    (owner_home / ".gitconfig").write_text(
        f"[user]\n\tname = {name}\n\temail = {email}\n",
        encoding="utf-8",
    )
    return owner_home


@pytest.mark.parametrize(
    "owner_name,owner_email",
    [
        ("Owner Person", "owner@example.com"),
        ("Allen Woods", "allenwoods@users.noreply.code.hyprial.com"),
    ],
)
def test_owner_global_gitconfig_is_never_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_name: str,
    owner_email: str,
) -> None:
    """R1: the reviewer's reproduction — an owner identity on this machine
    must not become the agent's committer, however it is reachable."""

    owner_home = _owner_home_with_gitconfig(tmp_path, owner_name, owner_email)
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(owner_home / ".gitconfig"))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    author = resolve_git_author(actor=AGENT_NAME)
    profile = resolve_agent_tool_profile(_FakeAgent(actor=AGENT_NAME))

    assert (author.name, author.email) == (AGENT_NAME, AGENT_EMAIL)
    assert author.source == SOURCE_AGENT
    assert (profile.git_author_name, profile.git_author_email) == (
        AGENT_NAME,
        AGENT_EMAIL,
    )
    assert owner_name not in profile.git_author_name
    assert owner_email not in profile.git_author_email


def test_explicit_human_name_is_the_only_way_to_opt_in(
    tmp_path: Path,
) -> None:
    """R1: a human-signed commit requires the explicit per-agent pair."""

    _owner_home_with_gitconfig(tmp_path, "Allen Woods", "owner@example.com")
    agent = _FakeAgent(
        capabilities={
            "gitAuthorName": "Ming Li",
            "gitAuthorEmail": "ming.li@example.com",
        }
    )
    author = resolve_git_author(
        actor=agent.actor, capabilities=agent.capabilities
    )
    profile = resolve_agent_tool_profile(agent)
    assert author.source == SOURCE_EXPLICIT
    assert (profile.git_author_name, profile.git_author_email) == (
        "Ming Li",
        "ming.li@example.com",
    )


def test_partial_explicit_pair_is_ignored_not_half_applied(
    tmp_path: Path,
) -> None:
    """A lone key must not be blended with another identity, and must not
    reopen the owner fallback either."""

    _owner_home_with_gitconfig(tmp_path, "Owner Person", "owner@example.com")
    profile = resolve_agent_tool_profile(
        _FakeAgent(capabilities={"gitAuthorName": "CI Bot"})
    )
    assert (profile.git_author_name, profile.git_author_email) == (
        AGENT_NAME,
        AGENT_EMAIL,
    )


def test_blank_actor_is_unresolved_and_guides_configuration(
    tmp_path: Path,
) -> None:
    """R4: unresolved is loud and actionable — not a silent owner fallback."""

    _owner_home_with_gitconfig(tmp_path, "Owner Person", "owner@example.com")
    with pytest.raises(GitAuthorUnresolvedError) as excinfo:
        resolve_agent_tool_profile(_FakeAgent(actor="  "))
    assert excinfo.value.code == "GIT_AUTHOR_UNRESOLVED"
    message = str(excinfo.value)
    assert "gitAuthorName" in message
    assert "gitAuthorEmail" in message
    assert "never used" in message


def test_owner_overrides_are_not_accepted_by_the_api(tmp_path: Path) -> None:
    """The removed owner knobs cannot be reintroduced without a type error."""

    with pytest.raises(TypeError):
        resolve_agent_tool_profile(
            _FakeAgent(), owner_home=tmp_path  # type: ignore[call-arg]
        )


def test_agent_identity_email_uses_agent_prefix_and_sanitizes_actor(
    tmp_path: Path,
) -> None:
    """R1: default email is agent-<name>@agents.hyprial.com."""

    profile = resolve_agent_tool_profile(
        _FakeAgent(actor="Team:Worker@node-7")
    )
    assert profile.git_author_name == "Team:Worker@node-7"
    local_part, domain = profile.git_author_email.rsplit("@", 1)
    assert domain == "agents.hyprial.com"
    assert local_part == "agent-team-worker-node-7"
    assert "@" not in local_part


def test_profile_id_is_content_derived_and_stable(tmp_path: Path) -> None:
    first = resolve_agent_tool_profile(
        _FakeAgent(capabilities={
            "gitAuthorName": "CI Bot",
            "gitAuthorEmail": "ci-bot@agents.hyprial.com",
        })
    )
    second = resolve_agent_tool_profile(
        _FakeAgent(capabilities={
            "gitAuthorName": "CI Bot",
            "gitAuthorEmail": "ci-bot@agents.hyprial.com",
        })
    )
    other = resolve_agent_tool_profile(_FakeAgent())
    assert first.profile_id == second.profile_id
    assert first.profile_id != other.profile_id
    assert first.profile_id.startswith("agent-git-v1-")


def test_resolved_profile_materializes_as_agent_tool_profile(
    tmp_path: Path,
) -> None:
    profile = resolve_agent_tool_profile(_FakeAgent())
    assert isinstance(profile, AgentToolProfile)
    assert profile.profile_id
