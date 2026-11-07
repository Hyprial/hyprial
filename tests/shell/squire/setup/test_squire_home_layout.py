"""Squire uses the same home and configuration layout as other agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.squire import SquireSetup
from hyprial.daemon.impl.squire.home_layout import SquireHomeLayout, create_private_file
from hyprial.identity import Agent

from .test_squire_setup_scaffold import IDENTITY, FakeAgentRegistry, new_setup


@pytest.fixture(autouse=True)
def isolated_squire_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))


def test_default_squire_home_uses_the_agent_home_layout(tmp_path: Path) -> None:
    registry = FakeAgentRegistry()
    setup = new_setup(tmp_path, registry)
    home = tmp_path / "hyprial-home" / "agents" / "squire"

    result = setup.run(IDENTITY, step="create-squire")

    assert result["squire"]["home"] == str(home)
    assert result["squire"]["cwd"] == str(home / "workspace")
    assert registry.creates[0]["cwd"] == str(home / "workspace")
    assert registry.creates[0]["config"]["sources"] == [
        {"path": str(home / "config"), "required": True}
    ]
    assert (home / "config" / "shared" / "skills" / "squire" / "SKILL.md").is_file()
    assert (home / "workspace" / "profile.md").is_file()
    assert (home / "workspace" / "backlog.md").is_file()


def test_explicit_legacy_home_is_only_read_and_never_used_as_agent_home(tmp_path: Path) -> None:
    registry = FakeAgentRegistry()
    setup = new_setup(tmp_path, registry)
    external = tmp_path / "outside" / "squire"

    result = setup.run(IDENTITY, squire_home=external, step="create-squire")

    assert not external.exists()
    assert result["squire"]["home"] == str(tmp_path / "hyprial-home" / "agents" / "squire")


def test_squire_setup_state_must_stay_inside_hyprial_home(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="state directory"):
        SquireSetup(
            hyprial_home=tmp_path / "hyprial-home",
            state_dir=tmp_path / "outside-state",
        )


def test_legacy_squire_files_are_copied_without_deleting_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    legacy = Path.home() / "squire"
    legacy_skill = legacy / "skills" / "squire" / "SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_text("custom rules\n", encoding="utf-8")
    (legacy / "profile.md").write_text("personal profile\n", encoding="utf-8")
    (legacy / "backlog.md").write_text("personal backlog\n", encoding="utf-8")
    setup = new_setup(tmp_path, FakeAgentRegistry())

    setup.run(IDENTITY, step="create-squire")

    home = tmp_path / "hyprial-home" / "agents" / "squire"
    assert (home / "config" / "shared" / "skills" / "squire" / "SKILL.md").read_text() == "custom rules\n"
    assert (home / "workspace" / "profile.md").read_text() == "personal profile\n"
    assert (home / "workspace" / "backlog.md").read_text() == "personal backlog\n"
    assert legacy_skill.read_text() == "custom rules\n"
    assert (legacy / "profile.md").read_text() == "personal profile\n"
    assert (legacy / "backlog.md").read_text() == "personal backlog\n"


def test_legacy_skill_rewrites_its_old_data_paths_in_the_new_copy(tmp_path: Path) -> None:
    legacy = Path.home() / "squire"
    skill = legacy / "skills" / "squire" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    old_instruction = "Write ~/squire/backlog.md and read ~/.hyprial/org-context.md\n"
    skill.write_text(old_instruction, encoding="utf-8")
    setup = new_setup(tmp_path, FakeAgentRegistry())

    setup.run(IDENTITY, step="create-squire")

    copied = (
        tmp_path / "hyprial-home" / "agents" / "squire" / "config"
        / "shared" / "skills" / "squire" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "$HYPRIAL_HOME/agents/squire/workspace/backlog.md" in copied
    assert "$HYPRIAL_HOME/org-context.md" in copied
    assert "~/squire" not in copied
    assert skill.read_text(encoding="utf-8") == old_instruction


def test_legacy_copy_refuses_conflicting_target_without_overwriting_it(
    tmp_path: Path,
) -> None:
    legacy = Path.home() / "squire"
    legacy.mkdir(parents=True)
    (legacy / "profile.md").write_text("legacy profile\n", encoding="utf-8")
    target = tmp_path / "hyprial-home" / "agents" / "squire" / "workspace"
    target.mkdir(parents=True)
    (target / "profile.md").write_text("new profile\n", encoding="utf-8")
    setup = new_setup(tmp_path, FakeAgentRegistry())

    with pytest.raises(ValueError, match="conflicts"):
        setup.run(IDENTITY, step="create-squire")

    assert (target / "profile.md").read_text() == "new profile\n"
    assert (legacy / "profile.md").read_text() == "legacy profile\n"


def test_migrated_files_remain_user_owned_on_later_setup(tmp_path: Path) -> None:
    legacy = Path.home() / "squire"
    legacy.mkdir(parents=True)
    (legacy / "profile.md").write_text("old profile\n", encoding="utf-8")
    setup = new_setup(tmp_path, FakeAgentRegistry())

    setup.run(IDENTITY, step="create-squire")
    target = tmp_path / "hyprial-home" / "agents" / "squire" / "workspace" / "profile.md"
    target.write_text("new profile\n", encoding="utf-8")
    setup.run(IDENTITY, step="create-squire")

    assert target.read_text(encoding="utf-8") == "new profile\n"
    assert (legacy / "profile.md").read_text(encoding="utf-8") == "old profile\n"


def test_squire_scaffold_rejects_workspace_symlink_outside_home(tmp_path: Path) -> None:
    home = tmp_path / "hyprial-home" / "agents" / "squire"
    home.mkdir(parents=True)
    external = tmp_path / "outside"
    external.mkdir()
    (home / "workspace").symlink_to(external, target_is_directory=True)
    setup = new_setup(tmp_path, FakeAgentRegistry())

    with pytest.raises(ValueError, match="agent home"):
        setup.run(IDENTITY, step="create-squire")

    assert list(external.iterdir()) == []


def test_file_creation_rejects_workspace_symlink_installed_after_precheck(tmp_path: Path) -> None:
    root = tmp_path / "hyprial-home"
    layout = SquireHomeLayout.within(root)
    layout.workspace.mkdir(parents=True)
    external = tmp_path / "outside"
    external.mkdir()
    layout.validate_write_targets()
    layout.workspace.rmdir()
    layout.workspace.symlink_to(external, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        create_private_file(layout, layout.workspace / "profile.md", b"private\n")

    assert list(external.iterdir()) == []


def test_existing_squire_agent_rebinds_to_ordinary_home_config(tmp_path: Path) -> None:
    legacy = Path.home() / "squire"
    legacy.mkdir(parents=True)
    (legacy / "profile.md").write_text("saved profile\n", encoding="utf-8")
    registry = FakeAgentRegistry(
        agent=Agent(
            uri=IDENTITY.actor,
            actor="squire",
            owner=IDENTITY.owner,
            machine=IDENTITY.machine,
            cwd=str(legacy),
            config=None,
        )
    )
    setup = new_setup(tmp_path, registry)

    setup.run(IDENTITY, step="create-squire")

    home = tmp_path / "hyprial-home" / "agents" / "squire"
    assert registry.agent.cwd == str(home / "workspace")
    assert registry.agent.config.source == str(home / "config")
    assert (home / "workspace" / "profile.md").read_text() == "saved profile\n"
    assert (legacy / "profile.md").read_text() == "saved profile\n"
