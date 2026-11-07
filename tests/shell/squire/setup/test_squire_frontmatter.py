"""Regression coverage for Codex skill loading and conservative upgrades."""
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from .test_squire_setup_scaffold import IDENTITY, FakeAgentRegistry, new_setup


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)


def template():
    return files("hyprial.daemon.impl.squire").joinpath("SKILL-template.md").read_text()


def old_rendering():
    text = template()
    if text.startswith("---\n"):
        text = text.split("---\n", 2)[2].lstrip("\n")
    for key, value in (("<用户显示名>", "allen"), ("<用户名>", "alicesmith"),
                       ("<用户>", "allen"), ("<owner>", "allen"),
                       ("<adapter>", "allen-squire"), ("<dm-route>", "dm")):
        text = text.replace(key, value)
    return text.encode()


def skill_path(tmp_path):
    return tmp_path / "hyprial-home/agents/squire/config/shared/skills/squire/SKILL.md"


def test_packaged_and_documented_templates_have_yaml_frontmatter():
    text = template()
    assert text.startswith("---\n")
    frontmatter = yaml.safe_load(text.split("---\n", 2)[1])
    assert frontmatter["name"] == "squire"
    assert isinstance(frontmatter["description"], str) and frontmatter["description"]
    assert "<" not in text.split("---\n", 2)[1]
    assert Path("docs/domains/squire/SKILL-template.md").read_text() == text


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_old_rendering_is_upgraded_and_idempotent(tmp_path, legacy):
    target = skill_path(tmp_path)
    source = Path.home() / "squire/skills/squire/SKILL.md" if legacy else target
    source.parent.mkdir(parents=True)
    original = old_rendering()
    source.write_bytes(original)
    setup = new_setup(tmp_path, FakeAgentRegistry())
    result = setup.run(IDENTITY, step="create-squire", dm_route="dm")
    assert target.read_bytes().startswith(b"---\nname: squire\n")
    assert target.read_bytes().endswith(original)
    assert "scaffold.skill" in result["changed"]
    before = target.read_bytes()
    assert setup.run(IDENTITY, step="create-squire", dm_route="dm")["changed"] == []
    assert target.read_bytes() == before
    if legacy:
        assert source.read_bytes() == original


@pytest.mark.parametrize("legacy", [False, True])
def test_edited_old_skill_is_preserved_with_frontmatter_repair_guidance(tmp_path, legacy):
    target = skill_path(tmp_path)
    source = Path.home() / "squire/skills/squire/SKILL.md" if legacy else target
    source.parent.mkdir(parents=True)
    original = old_rendering() + b"\nUser customization.\n"
    source.write_bytes(original)
    setup = new_setup(tmp_path, FakeAgentRegistry())
    result = setup.run(IDENTITY, step="create-squire", dm_route="dm")
    assert target.read_bytes() == original
    assert source.read_bytes() == original
    warning = " ".join(result.get("warnings", []))
    assert "frontmatter" in warning
    assert "move" in warning and "prepend" in warning


def test_legacy_rendering_with_old_paths_upgrades_only_the_copy(tmp_path):
    source = Path.home() / "squire/skills/squire/SKILL.md"
    source.parent.mkdir(parents=True)
    original = old_rendering().replace(
        b"$HYPRIAL_HOME/agents/squire/config/shared/skills/squire/SKILL.md",
        b"~/squire/skills/squire/SKILL.md",
    ).replace(b"$HYPRIAL_HOME/agents/squire/workspace", b"~/squire").replace(
        b"$HYPRIAL_HOME/org-context.md", b"~/.hyprial/org-context.md",
    )
    source.write_bytes(original)
    setup = new_setup(tmp_path, FakeAgentRegistry())
    setup.run(IDENTITY, step="create-squire", dm_route="dm")
    assert skill_path(tmp_path).read_bytes().startswith(b"---\n")
    assert skill_path(tmp_path).read_bytes().endswith(old_rendering())
    assert source.read_bytes() == original


def test_custom_non_utf8_skill_is_preserved_with_repair_warning(tmp_path):
    target = skill_path(tmp_path)
    target.parent.mkdir(parents=True)
    original = old_rendering() + b"\xff"
    target.write_bytes(original)
    setup = new_setup(tmp_path, FakeAgentRegistry())
    result = setup.run(IDENTITY, step="create-squire", dm_route="dm")
    assert target.read_bytes() == original
    assert any("UTF-8" in warning and "frontmatter" in warning for warning in result["warnings"])


def test_upgrade_refuses_linked_skill(tmp_path):
    target = skill_path(tmp_path)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside-skill"
    original = old_rendering()
    outside.write_bytes(original)
    target.symlink_to(outside)
    setup = new_setup(tmp_path, FakeAgentRegistry())
    with pytest.raises(ValueError, match="symlink"):
        setup.run(IDENTITY, step="create-squire", dm_route="dm")
    assert outside.read_bytes() == original


def test_rendering_does_not_interpolate_owner_into_yaml(tmp_path):
    setup = new_setup(tmp_path, FakeAgentRegistry())
    setup.run(IDENTITY, step="create-squire", display_name='A: "B"\n---')
    text = skill_path(tmp_path).read_text()
    assert text.startswith("---\n")
    assert yaml.safe_load(text.split("---\n", 2)[1])["name"] == "squire"
