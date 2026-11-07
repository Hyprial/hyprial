"""Migration test: homes materialized with the retired built-in person profile
are re-resolved once at the next start, with one notice, and git history is
never touched (brief agent-git-identity).

The legacy profile directory is seeded through an explicit test-only profile
carrying the retired id; production code detects legacy homes by directory
shape alone and never stores the retired literal.

Fix round 1 (review of f98c63e248): R2 covers a legacy profile recorded under
a previous config revision — a normal config edit between launches changes
the revision and must not strand the old profile without a migration notice;
R3 covers rollback/re-upgrade idempotence — an older binary re-materializing
the retired profile beside its ``.migrated`` tombstone must still be set
aside, so the notice is emitted exactly once per real migration.

Fix round 2 (review of 1b051f70, REQUIRED REWORK R1-R4): both runtime cases
are asserted here against the reviewer's construction — the owner's global
git name is the retired person's name.  A newly materialized agent home and a
home migrated away from the retired profile must each produce a ``gitconfig``
whose ``user.name``/``user.email`` and all ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
values are in the agent domain, never the owner's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.runtime.context import (
    AgentToolProfile,
    resolve_agent_runtime_context,
)

RETIRED_PROFILE = AgentToolProfile(
    profile_id="agent-home-p2-allen-v1",
    git_author_name="Allen Woods",
    git_author_email="allenwoods@users.noreply.code.hyprial.com",
)

AGENT_NAME = "worker"
AGENT_EMAIL = "agent-worker@agents.hyprial.com"
OWNER_NAME = "Allen Woods"
OWNER_EMAIL = "allenwoods@users.noreply.code.hyprial.com"
IDENTITY_ENV_KEYS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)


@pytest.fixture(autouse=True)
def _isolated_git_config_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


def _config(root: Path) -> Path:
    config = root / "personality"
    native = config / "native" / "pi"
    native.mkdir(parents=True, exist_ok=True)
    (native / "settings.json").write_text("{}\n", encoding="utf-8")
    skill = config / "shared" / "skills" / "p22" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# P22\n", encoding="utf-8")
    return config


def _registry(tmp_path: Path) -> tuple[AgentRegistry, str, str]:
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir(parents=True)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="migration-owner",
        machine="migration-node",
        hyprial_home=hyprial_home,
    )
    config = _config(tmp_path)
    registry.create(
        "worker",
        cwd=str(tmp_path / "work"),
        config={
            "sources": [{"path": str(config), "required": True}],
            "discovery": "explicit-only",
        },
    )
    registry.ensure_home("worker")
    return registry, str(tmp_path / "work"), str(hyprial_home)


def _owner_home(tmp_path: Path) -> Path:
    """The reviewer's construction: the owner is the retired person."""

    owner_home = tmp_path / "owner-home"
    owner_home.mkdir(parents=True)
    (owner_home / ".gitconfig").write_text(
        f"[user]\n\tname = {OWNER_NAME}\n\temail = {OWNER_EMAIL}\n",
        encoding="utf-8",
    )
    return owner_home


def _git_config_value(environment: dict[str, str], key: str) -> str:
    """Read one profile value back through git, not through our own parser."""

    env = dict(environment)
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    completed = subprocess.run(
        ["git", "config", "--global", "--get", key],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _assert_agent_domain_identity(
    environment: dict[str, str], gitconfig: bytes
) -> None:
    """R2/R4: every exported identity field and the materialized file agree."""

    for key in IDENTITY_ENV_KEYS:
        expected = AGENT_EMAIL if key.endswith("EMAIL") else AGENT_NAME
        assert environment[key] == expected, key
        assert OWNER_NAME not in environment[key], key
        assert OWNER_EMAIL not in environment[key], key
    assert f"\tname = {AGENT_NAME}\n".encode() in gitconfig
    assert f"\temail = {AGENT_EMAIL}\n".encode() in gitconfig
    assert OWNER_NAME.encode() not in gitconfig
    assert OWNER_EMAIL.encode() not in gitconfig
    assert _git_config_value(environment, "user.name") == AGENT_NAME
    assert _git_config_value(environment, "user.email") == AGENT_EMAIL


def _tools_dirs(home: Path) -> tuple[set[str], dict[str, bytes]]:
    tools_roots = sorted((home / "state" / "config").glob("*/*/tools/*"))
    names = {path.name for path in tools_roots}
    bodies = {
        path.name: (path / "gitconfig").read_bytes()
        for path in tools_roots
        if (path / "gitconfig").exists()
    }
    return names, bodies


def test_legacy_profile_home_re_resolves_once_with_one_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, cwd, home = _registry(tmp_path)
    owner_home = _owner_home(tmp_path)
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    try:
        legacy = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=cwd,
            tool_profile=RETIRED_PROFILE,
        )
        assert legacy is not None
        assert legacy.tool_profile_id == "agent-home-p2-allen-v1"
        agent_home = Path(registry.home_receipt("worker").path)
        names, bodies = _tools_dirs(agent_home)
        assert names == {"agent-home-p2-allen-v1"}
        assert b"Allen Woods" in bodies["agent-home-p2-allen-v1"]

        migrated = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=cwd,
            tool_profile=None,
        )
        assert migrated is not None
        assert migrated.tool_profile_id.startswith("agent-git-v1-")
        assert migrated.tool_profile_id != "agent-home-p2-allen-v1"
        assert len(migrated.notices) == 1
        notice = migrated.notices[0].to_json()
        assert notice["code"] == "AGENT_GIT_PROFILE_MIGRATED"
        assert notice["actor"] == "worker"
        assert migrated.environment()["GIT_AUTHOR_NAME"] == AGENT_NAME
        assert migrated.environment()["GIT_AUTHOR_EMAIL"] == AGENT_EMAIL

        names, bodies = _tools_dirs(agent_home)
        migrated_id = migrated.tool_profile_id
        assert "agent-home-p2-allen-v1" not in names
        assert "agent-home-p2-allen-v1.migrated" in names
        assert migrated_id in names
        _assert_agent_domain_identity(migrated.environment(), bodies[migrated_id])

        again = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=cwd,
            tool_profile=None,
        )
        assert again is not None
        assert again.tool_profile_id == migrated_id
        assert again.notices == ()

        projection = migrated.public_projection()
        assert projection["toolProfileId"] == migrated_id
        assert json.dumps(projection)
    finally:
        registry.close()


def test_new_agent_materializes_agent_git_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 case 1: a fresh materialize never adopts the owner's identity."""

    registry, cwd, home = _registry(tmp_path)
    owner_home = _owner_home(tmp_path)
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    try:
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=cwd,
            tool_profile=None,
        )
        assert context is not None
        assert context.tool_profile_id.startswith("agent-git-v1-")
        names, bodies = _tools_dirs(
            Path(registry.home_receipt("worker").path)
        )
        assert context.tool_profile_id in names
        _assert_agent_domain_identity(
            context.environment(), bodies[context.tool_profile_id]
        )
    finally:
        registry.close()


def test_legacy_profile_under_previous_config_revision_migrates_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, cwd, home = _registry(tmp_path)
    owner_home = _owner_home(tmp_path)
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    try:
        legacy = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=cwd,
            tool_profile=RETIRED_PROFILE,
        )
        assert legacy is not None
        legacy_root = legacy.roots.tool_profile_root

        skill = tmp_path / "personality" / "shared" / "skills" / "p22" / "SKILL.md"
        skill.write_text("# Updated configuration\n", encoding="utf-8")

        current = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=cwd,
            tool_profile=None,
        )
        assert current is not None
        assert current.config_revision != legacy.config_revision
        assert len(current.notices) == 1
        assert current.notices[0].code == "AGENT_GIT_PROFILE_MIGRATED"
        assert current.environment()["GIT_AUTHOR_NAME"] == AGENT_NAME
        _names, bodies = _tools_dirs(
            Path(registry.home_receipt("worker").path)
        )
        _assert_agent_domain_identity(
            current.environment(), bodies[current.tool_profile_id]
        )

        assert not legacy_root.exists()
        assert Path(str(legacy_root) + ".migrated").is_dir()
    finally:
        registry.close()


def test_rollback_re_upgrade_migration_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, cwd, home = _registry(tmp_path)
    owner_home = _owner_home(tmp_path)
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    try:
        contexts = []
        for selected in (RETIRED_PROFILE, None, RETIRED_PROFILE, None, None):
            context = resolve_agent_runtime_context(
                registry=registry,
                agent_name="worker",
                harness="pi",
                cwd=cwd,
                tool_profile=selected,
            )
            assert context is not None
            contexts.append(context)

        counts = [len(context.notices) for context in contexts]
        assert counts == [0, 1, 0, 1, 0]

        first_legacy_root = contexts[0].roots.tool_profile_root
        assert not first_legacy_root.exists()
        assert Path(str(first_legacy_root) + ".migrated").is_dir()
    finally:
        registry.close()
